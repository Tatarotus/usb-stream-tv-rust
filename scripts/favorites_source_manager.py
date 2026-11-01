#!/usr/bin/env python3
"""Source manager + private media link for the Live Favorites runtime.

This module is intentionally self-contained and import-safe: importing it does
not start threads, spawn ffmpeg or open sockets. Production wiring must call
`FavoritesSourceManager` from the existing control API and run
`FavoritesMediaServer` on a private loopback/private-network address.

Contract highlights:

* control auth is `Authorization: Bearer <token>` plus explicit
  `X-Mount-Session` and `X-Manifest-Id` headers; no PIN/query/fallback;
* one preparation per session, monotonic generation/epoch;
* a media link is exclusive per epoch and carries the exact framed identity;
* frames are HMAC-SHA256 authenticated with `transport_key(token)`, matching
  `crates/fuse_ntfs/src/favorites/transport.rs`;
* the media server only moves TS bytes to the feeder; it never changes the
  global HUB or another session's TV/VOD.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

TS_PKT_SZ = 188
FRAME_MAGIC = b"TVFT"
FRAME_VERSION = 1
FRAME_HEADER = struct.Struct(">4sBBHIQQQIHHH")
FRAME_TAG_LEN = 32
MAX_FRAME_PAYLOAD = 256 * 1024
MAX_IDENTITY = 512
TOKEN_DOMAIN = b"tv-live-favorites/transport/v1\0"


class TransportError(ValueError):
    pass


class ControlError(ValueError):
    pass


def transport_key(token: str) -> bytes:
    if not token or not token.strip():
        raise TransportError("empty token")
    return hashlib.sha256(TOKEN_DOMAIN + token.encode("utf-8")).digest()


def frame_bytes(request: dict, sequence: int, payload: bytes, token: str) -> bytes:
    if not payload or len(payload) % TS_PKT_SZ:
        raise TransportError("payload is not whole TS packets")
    if len(payload) > MAX_FRAME_PAYLOAD:
        raise TransportError("frame too large")
    identities = [
        request["session_id"].encode("utf-8"),
        request["manifest_id"].encode("utf-8"),
        request["channel_id"].encode("utf-8"),
    ]
    if any(len(value) > MAX_IDENTITY for value in identities):
        raise TransportError("identity too long")
    header = FRAME_HEADER.pack(
        FRAME_MAGIC,
        FRAME_VERSION,
        0,
        FRAME_HEADER.size,
        int(request["file_id"]),
        int(request["request_generation"]),
        int(request["source_epoch"]),
        int(sequence),
        len(payload),
        len(identities[0]),
        len(identities[1]),
        len(identities[2]),
    )
    body = header + b"".join(identities) + payload
    tag = hmac.new(transport_key(token), body, hashlib.sha256).digest()
    return body + tag


def decode_frame(wire: bytes, token: str) -> tuple[dict, int]:
    if len(wire) < FRAME_HEADER.size + FRAME_TAG_LEN:
        raise TransportError("short frame")
    (
        magic,
        version,
        _flags,
        header_len,
        file_id,
        generation,
        epoch,
        sequence,
        payload_len,
        session_len,
        manifest_len,
        channel_len,
    ) = FRAME_HEADER.unpack(wire[: FRAME_HEADER.size])
    if magic != FRAME_MAGIC or version != FRAME_VERSION or header_len != FRAME_HEADER.size:
        raise TransportError("bad header")
    identity_len = session_len + manifest_len + channel_len
    total = FRAME_HEADER.size + identity_len + payload_len + FRAME_TAG_LEN
    if total != len(wire) or payload_len == 0 or payload_len % TS_PKT_SZ:
        raise TransportError("bad length")
    body, tag = wire[:-FRAME_TAG_LEN], wire[-FRAME_TAG_LEN:]
    if not hmac.compare_digest(hmac.new(transport_key(token), body, hashlib.sha256).digest(), tag):
        raise TransportError("bad tag")
    cursor = FRAME_HEADER.size
    session = wire[cursor : cursor + session_len].decode("utf-8")
    cursor += session_len
    manifest = wire[cursor : cursor + manifest_len].decode("utf-8")
    cursor += manifest_len
    channel = wire[cursor : cursor + channel_len].decode("utf-8")
    cursor += channel_len
    payload = wire[cursor : cursor + payload_len]
    request = {
        "file_id": file_id,
        "request_generation": generation,
        "source_epoch": epoch,
        "session_id": session,
        "manifest_id": manifest,
        "channel_id": channel,
    }
    return request, sequence, payload


def authenticate_headers(token: str, headers: str) -> tuple[str, str]:
    if not token or not token.strip():
        raise ControlError("empty configured token")
    values: dict[str, str] = {}
    for line in headers.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip().lower()] = value.strip()
    authorization = values.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise ControlError("unauthorized")
    supplied = authorization[len("Bearer ") :]
    if not hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
        raise ControlError("unauthorized")
    session = values.get("x-mount-session", "").strip()
    manifest = values.get("x-manifest-id", "").strip()
    if not session:
        raise ControlError("missing session")
    if not manifest:
        raise ControlError("missing manifest")
    return session, manifest


@dataclass
class Grant:
    session_id: str
    manifest_id: str
    file_id: int
    channel_id: str
    request_generation: int
    source_epoch: int
    created_at: float = field(default_factory=time.monotonic)

    def as_request(self) -> dict:
        return {
            "session_id": self.session_id,
            "manifest_id": self.manifest_id,
            "file_id": self.file_id,
            "channel_id": self.channel_id,
            "request_generation": self.request_generation,
            "source_epoch": self.source_epoch,
        }


class FavoritesSourceManager:
    """Owns the single preparation allowed for one mount session."""

    def __init__(self, token: str, manifest_id: str):
        if not token or not token.strip():
            raise ControlError("empty token")
        self.token = token
        self.manifest_id = manifest_id
        self.session_id: Optional[str] = None
        self._generation = 0
        self._epoch = 0
        self._grant: Optional[Grant] = None
        self._lock = threading.Lock()

    def grant(self) -> Optional[Grant]:
        with self._lock:
            return self._grant

    def handle_control(self, headers: str, body: dict) -> dict:
        session, manifest = authenticate_headers(self.token, headers)
        if manifest != self.manifest_id:
            raise ControlError("wrong manifest")
        operation = str(body.get("operation", "prepare")).lower()
        with self._lock:
            if self.session_id is None:
                self.session_id = session
            elif self.session_id != session:
                raise ControlError("wrong session")
            if operation == "prepare":
                file_id = int(body.get("file_id", -1))
                channel_id = str(body.get("channel_id", "")).strip()
                if file_id < 0 or not channel_id:
                    raise ControlError("invalid identity")
                if (
                    self._grant is not None
                    and self._grant.file_id == file_id
                    and self._grant.channel_id == channel_id
                ):
                    return self._grant.as_request()
                self._generation += 1
                self._epoch += 1
                self._grant = Grant(
                    session,
                    manifest,
                    file_id,
                    channel_id,
                    self._generation,
                    self._epoch,
                )
                return self._grant.as_request()
            if operation == "cancel":
                if self._grant is None:
                    raise ControlError("no grant")
                if int(body.get("generation", -1)) != self._grant.request_generation:
                    raise ControlError("stale generation")
                self._grant = None
                return {"cancelled": True}
            if operation == "confirm":
                if self._grant is None:
                    raise ControlError("no grant")
                if int(body.get("generation", -1)) != self._grant.request_generation:
                    raise ControlError("stale generation")
                if int(body.get("epoch", -1)) != self._grant.source_epoch:
                    raise ControlError("stale epoch")
                return self._grant.as_request()
            raise ControlError("unknown operation")

    def authorize_media(
        self,
        generation: int,
        epoch: int,
        file_id: int,
        channel_id: str,
    ) -> Optional[Grant]:
        with self._lock:
            grant = self._grant
            if grant is None:
                return None
            if grant.request_generation != generation or grant.source_epoch != epoch:
                return None
            if grant.file_id != file_id or grant.channel_id != channel_id:
                return None
            return grant


OPEN_RE = re.compile(r"^OPEN\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)$")


def parse_open(line: str, identity: bytes, token: str) -> dict:
    match = OPEN_RE.match(line)
    if not match:
        raise TransportError("bad open line")
    generation, epoch, file_id, session_len, manifest_len, channel_len, token_len = map(
        int, match.groups()
    )
    expected = session_len + manifest_len + channel_len + token_len
    if expected != len(identity):
        raise TransportError("identity length mismatch")
    cursor = 0
    session = identity[cursor : cursor + session_len].decode("utf-8")
    cursor += session_len
    manifest = identity[cursor : cursor + manifest_len].decode("utf-8")
    cursor += manifest_len
    channel = identity[cursor : cursor + channel_len].decode("utf-8")
    cursor += channel_len
    supplied = identity[cursor:]
    if not hmac.compare_digest(supplied, token.encode("utf-8")):
        raise TransportError("unauthorized media link")
    return {
        "session_id": session,
        "manifest_id": manifest,
        "channel_id": channel,
        "file_id": file_id,
        "request_generation": generation,
        "source_epoch": epoch,
    }


def align_chunks(chunks: Iterable[bytes], max_payload: int = 64 * 1024) -> Iterable[bytes]:
    """Buffers arbitrary source output into whole TS packets <= max_payload."""
    limit = max(TS_PKT_SZ, (max_payload // TS_PKT_SZ) * TS_PKT_SZ)
    buffer = bytearray()
    for chunk in chunks:
        if not chunk:
            continue
        buffer.extend(chunk)
        while len(buffer) >= limit:
            yield bytes(buffer[:limit])
            del buffer[:limit]
    whole = (len(buffer) // TS_PKT_SZ) * TS_PKT_SZ
    if whole:
        yield bytes(buffer[:whole])


@dataclass(frozen=True)
class OutputProfile:
    """Encoder/mux profile approved for the Samsung 2013 decoder."""

    name: str
    width: int
    height: int
    fps: int
    level: str
    video_kbps: int
    maxrate_kbps: int
    bufsize_kbps: int
    muxrate: int
    pcr_period_ms: int = 20
    x264_profile: str = "baseline"

    @property
    def scale_filter(self) -> str:
        return (
            f"scale={self.width}:{self.height}:force_original_aspect_ratio=decrease,"
            f"pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={self.fps}"
        )


PROFILES = {
    # 19 ms keeps the effective PCR interval below the 20 ms ceiling after the
    # muxer's rounding, so the fail-closed PcrBounder does not need to split a
    # 1080p interval that has no NULL packet available (Astra review).
    "1080p30": OutputProfile("1080p30", 1920, 1080, 30, "4.0", 3500, 4000, 500,
                             6_000_000, pcr_period_ms=19),
    # Historical physical baseline (two-channel MVP), kept for rollback tests.
    "720p30": OutputProfile("720p30", 1280, 720, 30, "3.1", 2500, 3000, 100,
                            6_000_000, pcr_period_ms=20),
}
DEFAULT_OUTPUT_PROFILE = "1080p30"


class FakeProvider:
    """Deterministic provider for host tests; no ffmpeg or network."""
    def __init__(self, chunks: list[bytes], fail: bool = False):
        self.chunks = chunks
        self.fail = fail
        self.requests: list[dict] = []

    def open(self, request: dict) -> Iterable[bytes]:
        self.requests.append(dict(request))
        if self.fail:
            raise RuntimeError("provider failed")
        return iter(self.chunks)

    def close(self) -> None:
        pass


class FFmpegProvider:
    """Reference provider for one live channel per epoch.

    `resolve_url(channel_id)` must use the existing catalog/credentials; this
    class never guesses IDs or reads secrets from the request. It starts at
    most one process per active link and is not the global HUB process.
    """

    def __init__(self, resolve_url: Callable[[str], Optional[str]], ffmpeg: Optional[str] = None,
                 profile: "OutputProfile" = PROFILES[DEFAULT_OUTPUT_PROFILE]):
        self.resolve_url = resolve_url
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg") or "ffmpeg"
        self.profile = profile
        self.proc: Optional[subprocess.Popen] = None

    def command(self, url: str) -> list[str]:
        profile = self.profile
        # Live HTTP endpoints may close a chunk cleanly as well as disconnect
        # mid-stream. Keep the encoder/transport epoch alive across either case.
        # These are HTTP protocol options and must not be passed to file inputs
        # used by offline probes (nor to other FFmpeg protocols).
        http_reconnect = []
        if re.match(r"^https?://", url, re.IGNORECASE):
            http_reconnect = [
                "-rw_timeout", "10000000",
                "-reconnect", "1",
                "-reconnect_at_eof", "1",
                "-reconnect_streamed", "1",
                "-reconnect_on_network_error", "1",
                "-reconnect_delay_max", "5",
            ]
        return [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            *http_reconnect,
            "-fflags",
            "+genpts+discardcorrupt",
            "-probesize",
            "1000000",
            "-analyzeduration",
            "1500000",
            "-i",
            url,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            profile.scale_filter,
            "-pix_fmt",
            "yuv420p",
            "-g",
            "30",
            "-keyint_min",
            "30",
            "-sc_threshold",
            "0",
            "-c:v",
            "libx264",
            "-profile:v",
            profile.x264_profile,
            "-level:v",
            profile.level,
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-x264-params",
            "bframes=0:slices=1:sliced-threads=0:keyint=30:min-keyint=30:scenecut=0:repeat-headers=1",
            "-b:v",
            f"{profile.video_kbps}k",
            "-maxrate",
            f"{profile.maxrate_kbps}k",
            "-bufsize",
            f"{profile.bufsize_kbps}k",
            "-threads:v",
            "1",
            "-threads:a",
            "1",
            "-c:a",
            "ac3",
            "-b:a",
            "384k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-pcr_period",
            str(profile.pcr_period_ms),
            "-muxrate",
            str(profile.muxrate),
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-f",
            "mpegts",
            "pipe:1",
        ]

    def open(self, request: dict) -> Iterable[bytes]:
        url = self.resolve_url(request["channel_id"])
        if not url:
            raise RuntimeError("channel is not a resolvable Live source")
        self.proc = subprocess.Popen(
            self.command(url),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        assert self.proc.stdout is not None
        return iter(lambda: self.proc.stdout.read(65536), b"")

    def close(self) -> None:
        if self.proc is not None:
            try:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None


class FavoritesMediaServer:
    """Private loopback media link. One authenticated connection per epoch."""

    def __init__(self, manager: FavoritesSourceManager, provider, host: str = "127.0.0.1", port: int = 0):
        self.manager = manager
        self.provider = provider
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((host, port))
        self._socket.listen(2)
        self._socket.settimeout(0.2)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._client_lock = threading.Lock()

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._socket.getsockname()
        return host, port

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        try:
            self._socket.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _accept_loop(self) -> None:
        while self._running:
            try:
                client, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._client_lock:
                self._serve_client(client)

    def _read_line(self, client: socket.socket) -> bytes:
        data = bytearray()
        while len(data) < 512:
            byte = client.recv(1)
            if not byte:
                raise TransportError("client closed")
            if byte == b"\n":
                return bytes(data)
            data.extend(byte)
        raise TransportError("line too long")

    def _serve_client(self, client: socket.socket) -> None:
        grant = None
        try:
            client.settimeout(5.0)
            line = self._read_line(client).decode("ascii", "strict")
            match = OPEN_RE.match(line)
            if not match:
                client.sendall(b"ERR bad open\n")
                return
            identity_len = sum(int(value) for value in match.groups()[3:])
            identity = bytearray()
            while len(identity) < identity_len:
                chunk = client.recv(identity_len - len(identity))
                if not chunk:
                    raise TransportError("identity truncated")
                identity.extend(chunk)
            request = parse_open(line, bytes(identity), self.manager.token)
            grant = self.manager.authorize_media(
                request["request_generation"],
                request["source_epoch"],
                request["file_id"],
                request["channel_id"],
            )
            if grant is None or grant.session_id != request["session_id"] or grant.manifest_id != request["manifest_id"]:
                client.sendall(b"ERR stale epoch\n")
                return
            source = self.provider.open(request)
            client.sendall(
                f"OK {request['request_generation']} {request['source_epoch']}\n".encode()
            )
            client.settimeout(5.0)
            sequence = 0
            for payload in align_chunks(source):
                sequence += 1
                client.sendall(frame_bytes(request, sequence, payload, self.manager.token))
        except (TransportError, OSError, RuntimeError, ValueError):
            try:
                client.sendall(b"ERR source failed\n")
            except OSError:
                pass
        finally:
            self.provider.close()
            try:
                client.close()
            except OSError:
                pass


def main() -> int:
    print(
        "favorites_source_manager is a library; wire FavoritesSourceManager to the "
        "control API and run FavoritesMediaServer on a private address."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
