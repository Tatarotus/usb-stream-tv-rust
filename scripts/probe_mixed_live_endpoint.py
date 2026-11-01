#!/usr/bin/env python3
"""Prove that the Live path really delivers MPEG-TS bytes (not just a listener).

Performs the real manager handshake (``OPEN`` with session, manifest, channel
and token identities) and then authenticates and decodes the HMAC-framed
transport frames, asserting:

* the manager accepted the identities (``OK <generation> <epoch>``);
* every frame passes the transport HMAC;
* every payload is a whole number of 188-byte TS packets;
* the TS sync byte ``0x47`` is present at every packet boundary.

Run it on the VPS against the loopback port the tablet tunnel forwards to (for
example ``127.0.0.1:18190``). It never prints the token, the identities or any
raw media byte; only counts and the sanitized verdict.
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from favorites_source_manager import (  # noqa: E402
    FRAME_HEADER,
    FRAME_TAG_LEN,
    TS_PKT_SZ,
    TransportError,
    decode_frame,
)

MAX_FRAME = 256 * 1024 + 4096
TS_SYNC = 0x47


def _read_exactly(sock: socket.socket, count: int) -> bytes:
    buffer = bytearray()
    while len(buffer) < count:
        chunk = sock.recv(count - len(buffer))
        if not chunk:
            raise TransportError("connection closed mid-frame")
        buffer.extend(chunk)
    return bytes(buffer)


def probe_endpoint(*, host: str, port: int, session: str, manifest_id: str,
                   channel_id: str, file_id: int, generation: int, epoch: int,
                   token: str, frames: int = 2, timeout: float = 10.0) -> dict:
    """Return a sanitized verdict for the Live byte path."""
    result = {
        "ok": False,
        "host": host,
        "port": int(port),
        "handshake": None,
        "frames": 0,
        "ts_bytes": 0,
        "payloads_are_ts": False,
        "error": None,
    }
    identities = [session.encode(), manifest_id.encode(), channel_id.encode()]
    token_bytes = token.encode()
    line = (f"OPEN {int(generation)} {int(epoch)} {int(file_id)} "
            f"{len(identities[0])} {len(identities[1])} {len(identities[2])} "
            f"{len(token_bytes)}\n").encode()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(line + b"".join(identities) + token_bytes)
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = sock.recv(1)
                if not chunk:
                    raise TransportError("manager closed during handshake")
                reply += chunk
            text = reply.decode("utf-8", "replace").strip()
            result["handshake"] = "ok" if text.startswith("OK ") else "denied"
            if not text.startswith("OK "):
                result["error"] = "manager denied the media link"
                return result
            for _ in range(max(1, frames)):
                header = _read_exactly(sock, FRAME_HEADER.size)
                (
                    magic, version, _flags, header_len, _file_id, _generation,
                    _epoch, _sequence, payload_len, session_len, manifest_len,
                    channel_len,
                ) = FRAME_HEADER.unpack(header)
                identity_len = session_len + manifest_len + channel_len
                if header_len != FRAME_HEADER.size or identity_len > 4096:
                    raise TransportError("bad frame header")
                rest = _read_exactly(sock, identity_len + payload_len + FRAME_TAG_LEN)
                if len(header) + len(rest) > MAX_FRAME + FRAME_TAG_LEN:
                    raise TransportError("frame too large")
                request, sequence, payload = decode_frame(header + rest, token)
                if magic != b"TVFT" or version != 1 or not payload:
                    raise TransportError("invalid frame")
                if len(payload) % TS_PKT_SZ or any(
                        payload[index] != TS_SYNC
                        for index in range(0, len(payload), TS_PKT_SZ)):
                    result["payloads_are_ts"] = False
                    result["error"] = "payload is not aligned MPEG-TS"
                    return result
                result["frames"] += 1
                result["ts_bytes"] += len(payload)
        result["payloads_are_ts"] = result["frames"] > 0
        result["ok"] = result["payloads_are_ts"]
        return result
    except (OSError, TransportError, ValueError, struct.error) as exc:
        result["error"] = f"{type(exc).__name__}"
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--manifest-id", required=True)
    parser.add_argument("--channel-id", required=True)
    parser.add_argument("--file-id", type=int, required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--epoch", type=int, default=1)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--frames", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    try:
        token = args.token_file.read_text().strip()
    except OSError:
        parser.exit(2, "token file unreadable\n")
    if not token:
        parser.exit(2, "empty transport token\n")
    verdict = probe_endpoint(
        host=args.host, port=args.port, session=args.session,
        manifest_id=args.manifest_id, channel_id=args.channel_id,
        file_id=args.file_id, generation=args.generation, epoch=args.epoch,
        token=token, frames=args.frames, timeout=args.timeout)
    print(json.dumps(verdict, indent=2))
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
