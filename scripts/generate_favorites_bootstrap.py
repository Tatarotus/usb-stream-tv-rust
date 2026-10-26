#!/usr/bin/env python3
"""Generate the finite synthetic MPEG-TS bootstrap used by favorites files.

The asset is a short slate, not a copy of any live channel. It is generated
offline once and embedded in the Rust driver; the runtime never invokes ffmpeg
and never depends on the network. Regenerate with:

    python3 scripts/generate_favorites_bootstrap.py

The resulting stream is 188-byte aligned, H.264 Constrained Baseline with
bframes=0/slices=1/GOP 30 (1.0 s at 30 fps), AC3 384 kbit/s stereo 48 kHz and a
PCR interval of at most 20 ms. A 6 Mbit/s constant transport mux emits PCRs
between video frames; a bounded filter replaces existing NULL packets when
FFmpeg's scheduler overshoots the requested 20 ms. Packet count and bitrate
stay unchanged. The format and zero mux delay match the live source bridge.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

from live_favorites_pcr import PcrBounder

TS_PKT_SZ = 188
PCR_HZ = 27_000_000
PCR_MAX_STEP = PCR_HZ * 20 // 1000  # 20 ms
ASSET = pathlib.Path(__file__).resolve().parents[1] / (
    "crates/fuse_ntfs/assets/live_favorites_bootstrap.ts"
)


def ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        sys.exit("ffmpeg not found; install it to regenerate the bootstrap asset")
    return exe


def generate_raw(path: pathlib.Path) -> None:
    cmd = [
        ffmpeg(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=1280x720:r=30:d=2",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-shortest",
        "-c:v",
        "libx264",
        "-profile:v",
        "baseline",
        "-level",
        "3.1",
        "-pix_fmt",
        "yuv420p",
        "-threads:v",
        "1",
        "-threads:a",
        "1",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-x264-params",
        "bframes=0:slices=1:sliced-threads=0:keyint=30:min-keyint=30:scenecut=0:repeat-headers=1",
        "-g",
        "30",
        "-keyint_min",
        "30",
        "-sc_threshold",
        "0",
        "-b:v",
        "2500k",
        "-maxrate",
        "3000k",
        "-bufsize",
        "100k",
        "-c:a",
        "ac3",
        "-b:a",
        "384k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-pcr_period",
        "20",
        "-muxrate",
        "6000000",
        "-muxdelay",
        "0",
        "-muxpreload",
        "0",
        "-f",
        "mpegts",
        str(path),
    ]
    subprocess.run(cmd, check=True)


def pcr_of(packet: bytes) -> int | None:
    if len(packet) != TS_PKT_SZ or packet[0] != 0x47:
        return None
    afc = (packet[3] >> 4) & 3
    if not (afc & 2) or packet[4] == 0 or not (packet[5] & 0x10):
        return None
    b = packet[6:12]
    base = (b[0] << 25) | (b[1] << 17) | (b[2] << 9) | (b[3] << 1) | (b[4] >> 7)
    ext = ((b[4] & 1) << 8) | b[5]
    return base * 300 + ext


def validate(data: bytes) -> None:
    if not data or len(data) % TS_PKT_SZ:
        sys.exit("bootstrap is not a non-empty multiple of 188 bytes")
    pids: set[int] = set()
    pcrs: list[int] = []
    for i in range(0, len(data), TS_PKT_SZ):
        packet = data[i : i + TS_PKT_SZ]
        if packet[0] != 0x47:
            sys.exit(f"missing sync byte at packet {i // TS_PKT_SZ}")
        pids.add(((packet[1] & 0x1F) << 8) | packet[2])
        value = pcr_of(packet)
        if value is not None:
            pcrs.append(value)
    if 0 not in pids:
        sys.exit("bootstrap has no PAT (PID 0)")
    if not pcrs:
        sys.exit("bootstrap has no PCR")
    gaps = [b - a for a, b in zip(pcrs, pcrs[1:])]
    if any(gap <= 0 or gap > PCR_MAX_STEP for gap in gaps):
        sys.exit(f"PCR interval out of bounds: {min(gaps)}..{max(gaps)} ticks")


def main() -> None:
    ASSET.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tv-fav-bootstrap-") as tmp:
        raw = pathlib.Path(tmp) / "raw.ts"
        generate_raw(raw)
        bounder = PcrBounder()
        data = bounder.feed(raw.read_bytes()) + bounder.finish()
    validate(data)
    ASSET.write_bytes(data)
    print(f"wrote {ASSET} ({len(data)} bytes, {len(data) // TS_PKT_SZ} packets)")


if __name__ == "__main__":
    main()
