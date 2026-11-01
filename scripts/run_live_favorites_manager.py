#!/usr/bin/env python3
"""Private operational bridge: authenticated Rust OPEN -> one Live ffmpeg.

Run beside favorites_source_manager.py. Bind is deliberately loopback only;
connect the tablet through a private loopback tunnel. Credentials come from the
process environment, never from the manifest or command-line parameters.
This independent process does not import server.py or change the global HUB.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import socketserver
import struct
import subprocess
import threading
import time
from collections import Counter
from urllib.parse import urlsplit

from favorites_source_manager import (
    DEFAULT_OUTPUT_PROFILE,
    FFmpegProvider,
    MAX_IDENTITY,
    OPEN_RE,
    PROFILES,
    TS_PKT_SZ,
    TransportError,
    frame_bytes,
    parse_open,
)
from live_favorites_pcr import PcrBounder
import build_mixed_media_package

LOG = logging.getLogger("tv_live_favorites")
MAX_MANIFEST_ENTRIES = 256


def log_event(event: str, **fields):
    # Call sites supply static categories and numeric values only. Never pass
    # exception text, request identities, commands, source URLs or raw stderr.
    LOG.info(json.dumps({"event": event, **fields}, sort_keys=True))


def stderr_category(line: bytes) -> str | None:
    """Classify known ffmpeg errors; discard all provider text and secrets."""
    text = line.decode("utf-8", "replace").lower()
    http = re.search(r"(?:http error|server returned|http/[12](?:\.\d)?)[ :]+([45]\d\d)\b", text)
    if http:
        return "http_" + http.group(1)
    for needle, category in (
        ("connection refused", "connection_refused"), ("connection timed out", "connection_timeout"),
        ("connection reset", "connection_reset"), ("network is unreachable", "network_unreachable"),
        ("failed to resolve", "dns_failure"), ("invalid data found", "invalid_input"),
        ("unknown encoder", "unknown_encoder"), ("unrecognized option", "unknown_option"),
        ("option not found", "unknown_option"), ("error while opening encoder", "encoder_open_failed"),
        ("no such file or directory", "missing_input"), ("conversion failed", "conversion_failed"),
        ("broken pipe", "broken_pipe"), ("invalid argument", "invalid_argument"),
        ("error opening input", "input_open_failed"), ("error opening output", "output_open_failed"),
    ):
        if needle in text:
            return category
    return "other_ffmpeg_error" if text.strip() else None


def collect_stderr(process):
    counts = Counter()
    lock = threading.Lock()

    def drain():
        try:
            while line := process.stderr.readline(4096):
                category = stderr_category(line)
                if category:
                    with lock:
                        counts[category] += 1
        except (OSError, ValueError):
            pass

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    process._favorites_stderr = (counts, lock, thread)


PCR_REASONS = {
    "PCR clock does not match configured muxrate": "pcr_muxrate_mismatch",
    "no NULL packet to bound PCR interval": "pcr_no_null_slot",
    "PCR lookahead exceeded": "pcr_lookahead_exceeded",
    "PCR discontinuity or excessive original interval": "pcr_bad_interval",
    "PCR discontinuity indicator": "pcr_discontinuity",
    "multiple PCR PIDs are unsupported": "pcr_multiple_pids",
    "invalid MPEG-TS framing": "ts_bad_framing",
    "invalid MPEG-TS adaptation field": "ts_bad_adaptation",
}


def manifest_digest(manifest: dict) -> str:
    """Exact Manifest::canonical_bytes format; no JSON canonicalization guess."""
    out = bytearray(b"tv-live-favorites-manifest\0")

    def u32(value):
        out.extend(struct.pack("<I", value))

    def u64(value):
        out.extend(struct.pack("<Q", value))

    def string(value):
        encoded = value.encode("utf-8")
        u32(len(encoded))
        out.extend(encoded)

    u32(manifest["schema_version"])
    u32(manifest["layout_version"])
    u64(manifest["generation"])
    entries = sorted(manifest["entries"], key=lambda e: (
        e["file_id"], e["namespace"], e["channel_id"], e["folder"], e["filename"],
    ))
    u32(len(entries))
    for entry in entries:
        if entry["kind"] not in ("live", "vod"):
            raise ValueError("manifest contains unsupported entry")
        u32(entry["file_id"])
        string(entry["namespace"])
        string(entry["channel_id"])
        out.append(1 if entry["kind"] == "live" else 2)
        u32(len(entry["folder"]))
        for component in entry["folder"]:
            string(component)
        string(entry["filename"])
        u64(entry["logical_size"])
        string(entry["bootstrap_id"])
    return hashlib.sha256(out).hexdigest()


def live_favorite_channels(live_cache_path: Path, favorites_path: Path) -> dict:
    """Resolve only IDs present in both real Live cache and saved favorites."""
    if live_cache_path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Live cache exceeds size limit")
    if favorites_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("favorites exceed size limit")
    live_rows = json.loads(live_cache_path.read_text())
    favorites = json.loads(favorites_path.read_text())
    if not isinstance(live_rows, list) or not isinstance(favorites, list):
        raise ValueError("Live cache and favorites must be lists")
    if any(not isinstance(value, (int, str)) or isinstance(value, bool) for value in favorites):
        raise ValueError("invalid favorite identity")
    saved_ids = {str(value) for value in favorites}
    channels = {}
    for row in live_rows:
        if not isinstance(row, dict) or row.get("stream_type") != "live":
            continue
        channel_id = str(row.get("stream_id", ""))
        if not re.fullmatch(r"[1-9][0-9]{0,19}", channel_id) or channel_id not in saved_ids:
            continue
        if channel_id in channels:
            raise ValueError("duplicate Live stream identity")
        channels[channel_id] = {
            "id": channel_id,
            "url": "{XTREAM_UPSTREAM}/live/{XTREAM_USER}/{XTREAM_PASS}/" + channel_id + ".ts",
        }
    return channels


def file_id_for(channel_id: str) -> int:
    value = 0x811C9DC5
    for byte in b"live\0" + channel_id.encode("utf-8"):
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    return value


def _expected_live_entries(config: dict) -> set:
    """The exact Live entries a manifest must carry for this export.

    Derived from the SAME builder the package is produced with, so the two can
    never disagree about ``live_root``, category defaults, the ``.trp`` suffix
    or filename validation: one source of truth, checked instead of duplicated.
    """
    entries = build_mixed_media_package.process_live_identities(config)
    return {(tuple(entry["folder"]), entry["filename"], entry["channel_id"])
            for entry in entries}


def manifest_matches_export(manifest: dict, export_config_path: Path) -> None:
    """The mounted manifest must be exactly the reviewed export identity set."""
    config = json.loads(export_config_path.read_text())
    # Pending/unavailable identities are intentionally absent from the mounted
    # manifest; the snapshot reports them separately.
    expected = _expected_live_entries(config)
    actual = set()
    for entry in manifest["entries"]:
        if entry["kind"] == "vod":
            continue
        folder = tuple(entry["folder"])
        # A Live entry lives under its category, optionally nested one level
        # under ``live_root`` (the TV/ folder). Anything deeper is not a shape
        # this manager builds, so it stays a hard error rather than a guess.
        depth_ok = (len(folder) == 1
                    or (len(folder) == 2 and folder[0] == str(config.get("live_root") or "")))
        if not depth_ok:
            raise ValueError("manifest entry is not a category path")
        actual.add((folder, entry["filename"], entry["channel_id"]))
        if entry["file_id"] != file_id_for(entry["channel_id"]):
            raise ValueError("manifest file_id does not match the chosen candidate")
        if entry["bootstrap_id"] != f"bootstrap-{entry['file_id']:08x}":
            raise ValueError("manifest bootstrap id mismatch")
    live_entries = sum(1 for entry in manifest["entries"] if entry["kind"] != "vod")
    if len(actual) != live_entries:
        raise ValueError("manifest carries duplicate Live entries")
    if actual != expected:
        raise ValueError("manifest does not match the export configuration")


def export_allowlist_channels(export_config_path: Path, live_cache_path: Path) -> dict:
    """Build the exact allowlist from the reviewed export configuration.

    Only identities whose status is `approved` enter the mount. The chosen
    candidate must exist in the Live catalog; personal favorites are not used
    as a allowlist, so a better tested variant outside the favorites list is
    permitted without editing the user's saved list.
    """
    if export_config_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("export config exceeds size limit")
    config = json.loads(export_config_path.read_text())
    if config.get("schema_version") != 1:
        raise ValueError("unsupported export config schema")
    if config.get("provisional"):
        raise ValueError("provisional export is not mountable")
    identities = config.get("identities")
    if not isinstance(identities, list) or not 1 <= len(identities) <= MAX_MANIFEST_ENTRIES:
        raise ValueError("export identity count outside the supported limit")
    allowed = {}
    for item in identities:
        if not isinstance(item, dict):
            raise ValueError("export identity is malformed")
        status = item.get("status")
        if status in ("pending", "pending_measurement", "unavailable"):
            # Not mounted; only approved (native 1080p) identities enter the
            # transport allowlist.
            continue
        if status != "approved":
            raise ValueError("unsupported export status")
        chosen = item.get("chosen")
        if not isinstance(chosen, dict):
            raise ValueError("export identity has no chosen candidate")
        channel_id = str(chosen.get("channel_id", ""))
        if not re.fullmatch(r"[1-9][0-9]{0,19}", channel_id):
            raise ValueError("invalid chosen channel identity")
        if channel_id in allowed:
            raise ValueError("duplicate chosen candidate")
        allowed[channel_id] = None
    if not allowed:
        raise ValueError("export config has no approved candidate")
    if live_cache_path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Live cache exceeds size limit")
    for row in json.loads(live_cache_path.read_text()):
        if not isinstance(row, dict):
            continue
        channel_id = str(row.get("stream_id", ""))
        if channel_id not in allowed or allowed[channel_id] is not None:
            continue
        if row.get("stream_type") != "live":
            raise ValueError("chosen candidate is not a Live stream")
        allowed[channel_id] = {
            "id": channel_id,
            "url": "{XTREAM_UPSTREAM}/live/{XTREAM_USER}/{XTREAM_PASS}/" + channel_id + ".ts",
        }
    missing = sorted(channel_id for channel_id, value in allowed.items() if value is None)
    if missing:
        raise ValueError("chosen candidate missing from Live catalog")
    return allowed


def read_config(manifest_path: Path, channels_path: Path | None = None, *,
                live_cache_path: Path | None = None,
                favorites_path: Path | None = None,
                export_config_path: Path | None = None) -> tuple[dict, dict]:
    if manifest_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("manifest exceeds size limit")
    manifest = json.loads(manifest_path.read_text())
    if manifest["schema_version"] != 1 or manifest["layout_version"] != 1:
        raise ValueError("unsupported manifest schema/layout")
    if not hmac.compare_digest(manifest_digest(manifest), manifest["manifest_id"]):
        raise ValueError("manifest digest mismatch")
    entries = manifest["entries"]
    if not 1 <= len(entries) <= MAX_MANIFEST_ENTRIES:
        raise ValueError("manifest entry count outside the supported limit")
    if len({e["file_id"] for e in entries}) != len(entries):
        raise ValueError("duplicate file_id")
    if export_config_path is not None:
        if channels_path is not None or favorites_path is not None:
            raise ValueError("export mode excludes channels and favorites files")
        if live_cache_path is None:
            raise ValueError("export mode requires the Live catalog")
        channels = export_allowlist_channels(export_config_path, live_cache_path)
    elif live_cache_path is not None:
        if channels_path is not None or favorites_path is None:
            raise ValueError("Live cache mode requires favorites and no channels file")
        channels = live_favorite_channels(live_cache_path, favorites_path)
    elif channels_path is not None:
        channels = json.loads(channels_path.read_text())
    else:
        raise ValueError("missing source catalog")
    if export_config_path is not None:
        manifest_matches_export(manifest, export_config_path)
    if not isinstance(channels, dict):
        raise ValueError("channels catalog must be keyed by channel ID")
    sources = {}
    for entry in entries:
        if entry["kind"] == "vod":
            continue
        channel_id = entry["channel_id"]
        channel = channels.get(channel_id)
        if not isinstance(channel, dict) or channel.get("id", channel_id) != channel_id:
            raise ValueError("manifest channel is missing from catalog")
        raw = str(channel.get("url", ""))
        for key in ("XTREAM_UPSTREAM", "XTREAM_USER", "XTREAM_PASS"):
            if "{" + key + "}" in raw:
                value = os.environ.get(key, "")
                if not value:
                    raise ValueError("missing credential environment: " + key)
                raw = raw.replace("{" + key + "}", value)
        if "{" in raw or "}" in raw:
            raise ValueError("unresolved provider placeholder")
        if raw.startswith("/"):
            base = os.environ.get("XTREAM_UPSTREAM", "").rstrip("/")
            raw = base + raw
        parsed = urlsplit(raw)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("Live source must use HTTP(S)")
        if re.search(r"/(?:movie|series)/", parsed.path, re.I):
            raise ValueError("VOD source forbidden in Live manifest")
        sources[(entry["file_id"], channel_id)] = raw
    return manifest, sources


def ffmpeg_command(url: str, executable: str = "ffmpeg",
                   profile_name: str = DEFAULT_OUTPUT_PROFILE) -> list[str]:
    # Keep all frames compatible with the Samsung 2013 decoder. The approved
    # profile is 1080p30 baseline, zero B-frames, one slice, AC3 384k/48k/2ch
    # and -pcr_period 20; the bootstrap asset matches this geometry.
    try:
        profile = PROFILES[profile_name]
    except KeyError as error:
        raise ValueError("unknown output profile") from error
    cmd = FFmpegProvider(lambda _: None, executable, profile).command(url)
    cmd.insert(1, "-nostdin")
    return cmd


class LiveBridge(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, *, token: str, session: str, manifest: dict,
                 sources: dict, ffmpeg: str = "ffmpeg",
                 profile_name: str = DEFAULT_OUTPUT_PROFILE):
        if address[0] != "127.0.0.1":
            raise ValueError("bind must be IPv4 loopback; use a private tunnel")
        if profile_name not in PROFILES:
            raise ValueError("unknown output profile")
        self.profile_name = profile_name
        self.token = token
        self.session = session
        self.manifest_id = manifest["manifest_id"]
        self.sources = sources
        self.ffmpeg = ffmpeg
        self._state_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(2)
        self._generation = self._epoch = 0
        self._active = None
        super().__init__(address, LiveHandler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    @staticmethod
    def terminate(process):
        if process is None:
            return
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        if process.stdout:
            process.stdout.close()

    def open_source(self, request: dict):
        if request["session_id"] != self.session or request["manifest_id"] != self.manifest_id:
            raise TransportError("wrong session/manifest")
        identity = request["file_id"], request["channel_id"]
        if identity not in self.sources:
            raise TransportError("channel outside mount allowlist")
        with self._state_lock:
            generation, epoch = request["request_generation"], request["source_epoch"]
            if generation <= self._generation or epoch <= self._epoch:
                raise TransportError("stale generation/epoch")
            # Retire the old process before spawning the next, bounded to one
            # ffmpeg and no global control API changes.
            old, self._active = self._active, None
            if old:
                self.terminate(old[1])
            self._generation, self._epoch = generation, epoch
            process = subprocess.Popen(
                ffmpeg_command(self.sources[identity], self.ffmpeg, self.profile_name),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            )
            collect_stderr(process)
            self._active = (generation, process)
            log_event("source_started", file_id=request["file_id"], generation=generation,
                      epoch=epoch, pid=process.pid)
            return process

    def close_source(self, process):
        with self._state_lock:
            if self._active and self._active[1] is process:
                self._active = None
            self.terminate(process)

    def server_close(self):
        with self._state_lock:
            active, self._active = self._active, None
            if active:
                self.terminate(active[1])
        super().server_close()


class LiveHandler(socketserver.BaseRequestHandler):
    def handle(self):
        process = None
        client = self.request
        request = None
        sequence = bytes_read = bytes_sent = 0
        started = time.monotonic()
        reason = "handshake_rejected"
        stage = "handshake"
        try:
            client.settimeout(2)
            line = bytearray()
            while len(line) < 512:
                byte = client.recv(1)
                if not byte:
                    raise TransportError("closed handshake")
                if byte == b"\n":
                    break
                line.extend(byte)
            else:
                raise TransportError("open line too long")
            text = line.decode("ascii")
            match = OPEN_RE.fullmatch(text)
            if not match:
                raise TransportError("invalid OPEN")
            lengths = list(map(int, match.groups()[3:]))
            if any(length <= 0 or length > MAX_IDENTITY for length in lengths):
                raise TransportError("identity exceeds limit")
            identity_len = sum(lengths)
            identity = bytearray()
            while len(identity) < identity_len:
                chunk = client.recv(identity_len - len(identity))
                if not chunk:
                    raise TransportError("closed identity")
                identity.extend(chunk)
            request = parse_open(text, bytes(identity), self.server.token)
            stage = "source_open"
            process = self.server.open_source(request)
            client.sendall(f"OK {request['request_generation']} {request['source_epoch']}\n".encode())
            log_event("source_ack", file_id=request["file_id"],
                      generation=request["request_generation"], epoch=request["source_epoch"])
            stage = "stream"
            reason = "ffmpeg_exit"
            # Unbuffered stdout plus socket readiness makes a closed tablet
            # connection cancel ffmpeg even while the provider has stalled.
            with selectors.DefaultSelector() as selector:
                selector.register(client, selectors.EVENT_READ, "client")
                selector.register(process.stdout, selectors.EVENT_READ, "media")
                buffer = bytearray()
                pcr_bounder = PcrBounder(PROFILES[self.server.profile_name].muxrate)
                last_media = time.monotonic()
                while process.poll() is None:
                    if time.monotonic() - last_media > 20:
                        raise TransportError("source timed out")
                    for key, _ in selector.select(timeout=0.2):
                        if key.data == "client":
                            # Protocol permits no further client data.
                            reason = "unexpected_client_data" if client.recv(1) else "client_eof"
                            return
                        chunk = os.read(process.stdout.fileno(), 64 * 1024)
                        if not chunk:
                            reason = "ffmpeg_stdout_eof"
                            return
                        bytes_read += len(chunk)
                        last_media = time.monotonic()
                        buffer.extend(chunk)
                        # Forward every available whole packet immediately;
                        # never await 64KiB or throttle the feeder.
                        whole = len(buffer) // TS_PKT_SZ * TS_PKT_SZ
                        if whole:
                            sequence += 1
                            stage = "pcr_filter"
                            payload = pcr_bounder.feed(bytes(buffer[:whole]))
                            stage = "stream"
                            del buffer[:whole]
                            if payload:
                                client.sendall(frame_bytes(request, sequence, payload, self.server.token))
                                bytes_sent += len(payload)
                                if sequence == 1:
                                    log_event("first_frame", file_id=request["file_id"],
                                              generation=request["request_generation"],
                                              payload_bytes=len(payload),
                                              elapsed_ms=round((time.monotonic() - started) * 1000))
                            else:
                                sequence -= 1
        except (OSError, ValueError, TransportError, subprocess.SubprocessError) as error:
            if stage == "pcr_filter":
                reason = PCR_REASONS.get(str(error), "pcr_filter_rejected")
            elif isinstance(error, OSError):
                reason = "socket_or_process_io_error"
            elif stage == "stream":
                reason = "stream_rejected"
            elif stage == "source_open":
                reason = "source_open_rejected"
            try:
                client.sendall(b"ERR source unavailable\n")
            except OSError:
                pass
        finally:
            returncode = process.poll() if process else None
            if process:
                self.server.close_source(process)
                counts, lock, thread = process._favorites_stderr
                thread.join(timeout=0.2)
                with lock:
                    stderr_summary = dict(counts)
                if process.stderr:
                    process.stderr.close()
            else:
                stderr_summary = {}
            log_event("source_closed", reason=reason, stage=stage, frames=sequence,
                      bytes_read=bytes_read, bytes_sent=bytes_sent,
                      elapsed_ms=round((time.monotonic() - started) * 1000),
                      generation=request["request_generation"] if request else None,
                      file_id=request["file_id"] if request else None,
                      ffmpeg_returncode=returncode, ffmpeg_errors=stderr_summary)


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    catalog = parser.add_mutually_exclusive_group(required=True)
    catalog.add_argument("--channels", type=Path)
    catalog.add_argument("--live-cache", type=Path)
    parser.add_argument("--favorites", type=Path,
                        help="required with --live-cache; exact saved Live IDs")
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--session", required=True)
    parser.add_argument("--port", default=9123, type=int)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--profile", default=DEFAULT_OUTPUT_PROFILE, choices=sorted(PROFILES))
    parser.add_argument("--export-config", type=Path)
    args = parser.parse_args()
    if args.live_cache and not (args.favorites or args.export_config):
        parser.error("--live-cache requires --favorites or --export-config")
    if args.export_config and not args.live_cache:
        parser.error("--export-config requires --live-cache")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", args.session):
        parser.error("invalid mount session")
    if args.token_file.stat().st_size > 4096:
        parser.error("token file exceeds limit")
    token = args.token_file.read_text().strip()
    if not 32 <= len(token.encode()) <= 512:
        parser.error("token must contain 32..512 bytes")
    try:
        manifest, sources = read_config(args.manifest, args.channels,
                                        live_cache_path=args.live_cache,
                                        favorites_path=args.favorites,
                                        export_config_path=args.export_config)
        bridge = LiveBridge(("127.0.0.1", args.port), token=token, session=args.session,
                            manifest=manifest, sources=sources, ffmpeg=args.ffmpeg,
                            profile_name=args.profile)
    except (OSError, ValueError, KeyError):
        parser.error("invalid manifest/catalog/credentials; no source was started")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    worker = threading.Thread(target=bridge.serve_forever, daemon=True)
    worker.start()
    print(f"Live Favorites private bridge ready: 127.0.0.1:{args.port}; {len(sources)} Live entries", flush=True)
    try:
        stop.wait()
    finally:
        bridge.shutdown()
        bridge.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
