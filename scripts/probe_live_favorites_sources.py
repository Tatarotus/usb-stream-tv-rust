#!/usr/bin/env python3
"""Measure Live Favorites source candidates (sanitized evidence only).

Sequential probes with explicit timeouts. For every candidate this writes a
JSON record with resolution/codec/bitrate/PCR/audio-gap/decode-error metrics
and a short sanitized CSV row. Provider URLs, credentials and raw stderr are
never written: stderr is reduced to fixed categories and any URL is dropped.

Usage:
    python3 scripts/probe_live_favorites_sources.py plan.json --phase triage
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TS = 188
PCR_HZ = 27_000_000
URL_RE = re.compile(r"https?://\S+", re.I)

STDERR_CATEGORIES = (
    ("http error 4", "http_4xx"),
    ("http error 5", "http_5xx"),
    ("server returned 4", "http_4xx"),
    ("server returned 5", "http_5xx"),
    ("connection refused", "connection_refused"),
    ("connection timed out", "connection_timeout"),
    ("connection reset", "connection_reset"),
    ("network is unreachable", "network_unreachable"),
    ("failed to resolve", "dns_failure"),
    ("invalid data found", "invalid_input"),
    ("error while decoding", "decode_error"),
    ("decode_slice_header", "decode_error"),
    ("non-existing pps", "decode_error"),
    ("non-existing sps", "decode_error"),
    ("no frame!", "decode_error"),
    ("error opening input", "input_open_failed"),
    ("options specifier", "bad_option"),
    ("unrecognized option", "bad_option"),
    ("conversion failed", "conversion_failed"),
    ("packet too large", "packet_too_large"),
    ("timed out", "timeout"),
    ("stream ends prematurely", "source_reconnect"),
)


def load_env(path: Path = ROOT / ".env") -> dict:
    values = {}
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def source_url(env: dict, channel_id: str) -> str:
    upstream = env.get("XTREAM_UPSTREAM", "").rstrip("/")
    user = env.get("XTREAM_USER", "")
    password = env.get("XTREAM_PASS", "")
    if not (upstream and user and password):
        raise SystemExit("missing XTREAM_* credentials in .env")
    return f"{upstream}/live/{user}/{password}/{channel_id}.ts"


def classify_stderr(text: str) -> dict:
    counts = {}
    for line in text.splitlines():
        low = line.lower()
        for needle, category in STDERR_CATEGORIES:
            if needle in low:
                counts[category] = counts.get(category, 0) + 1
                break
    return counts


def run_probe_ffmpeg(url: str, duration: float, target: Path, ffmpeg: str) -> dict:
    started = time.monotonic()
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-rw_timeout", "10000000", "-reconnect", "1",
        "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-fflags", "+genpts+discardcorrupt",
        "-i", url, "-t", str(duration), "-map", "0", "-c", "copy",
        "-f", "mpegts", "-y", str(target),
    ]
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=duration + 20)
        returncode = done.returncode
        stderr = done.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as error:
        returncode = -1
        stderr = (error.stderr or b"").decode("utf-8", "replace")
    elapsed = round(time.monotonic() - started, 3)
    bytes_written = target.stat().st_size if target.exists() else 0
    return {
        "returncode": returncode,
        "wall_seconds": elapsed,
        "bytes": bytes_written,
        "stderr_categories": classify_stderr(stderr),
    }


def ffprobe_json(args: list, ffprobe: str, timeout: float = 120) -> dict:
    done = subprocess.run([ffprobe, "-v", "error", "-of", "json"] + args,
                          capture_output=True, timeout=timeout)
    if done.returncode != 0 or not done.stdout:
        return {}
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:
        return {}


def pcr_metrics(path: Path) -> dict:
    per_pid: dict[int, list[float]] = {}
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(TS * 4096)
            if not chunk:
                break
            for offset in range(0, len(chunk) - TS + 1, TS):
                packet = chunk[offset:offset + TS]
                if packet[0] != 0x47:
                    continue
                afc = (packet[3] >> 4) & 3
                if not afc & 2 or packet[4] < 7 or not packet[5] & 0x10:
                    continue
                pid = ((packet[1] & 0x1F) << 8) | packet[2]
                b = packet[6:12]
                base = (b[0] << 25) | (b[1] << 17) | (b[2] << 9) | (b[3] << 1) | (b[4] >> 7)
                per_pid.setdefault(pid, []).append(
                    (base * 300 + ((b[4] & 1) << 8) + b[5]) / 27_000)
    if not per_pid:
        return {"pcr_count": 0, "pcr_max_gap_ms": None, "pcr_gaps_over_40ms": 0}
    pid, pcrs = max(per_pid.items(), key=lambda item: len(item[1]))
    gaps = [b - a for a, b in zip(pcrs, pcrs[1:]) if 0 <= b - a < 5000]
    per_pid_gaps = {}
    for item_pid, values in per_pid.items():
        item_gaps = [b - a for a, b in zip(values, values[1:]) if 0 <= b - a < 5000]
        per_pid_gaps[hex(item_pid)] = round(max(item_gaps), 2) if item_gaps else None
    return {
        "pcr_pid": hex(pid),
        "pcr_count": len(pcrs),
        "pcr_pids": len(per_pid),
        "pcr_max_gap_ms": round(max(gaps), 2) if gaps else None,
        "pcr_median_gap_ms": round(statistics.median(gaps), 2) if gaps else None,
        "pcr_gaps_over_40ms": sum(1 for gap in gaps if gap > 40),
        "pcr_gaps_over_100ms": sum(1 for gap in gaps if gap > 100),
        "pcr_max_gap_by_pid": per_pid_gaps,
    }


def _remove_emulation(data: bytes) -> bytes:
    return data.replace(b"\x00\x00\x03", b"\x00\x00")


def _ue(data: bytes, bitpos: int) -> tuple[int, int]:
    zeros = 0
    while bitpos < len(data) * 8 and not (data[bitpos // 8] >> (7 - bitpos % 8)) & 1:
        zeros += 1
        bitpos += 1
    bitpos += 1
    value = 0
    for _ in range(zeros):
        value = (value << 1) | ((data[bitpos // 8] >> (7 - bitpos % 8)) & 1)
        bitpos += 1
    return (1 << zeros) - 1 + value, bitpos


def video_slices(path: Path, video_pid: int) -> dict:
    """Count slices per access unit and classify slice types from the ES."""
    es = bytearray()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(TS * 4096)
            if not chunk:
                break
            for offset in range(0, len(chunk) - TS + 1, TS):
                packet = chunk[offset:offset + TS]
                if packet[0] != 0x47:
                    continue
                pid = ((packet[1] & 0x1F) << 8) | packet[2]
                if pid != video_pid:
                    continue
                afc = (packet[3] >> 4) & 3
                if not afc & 1:
                    continue
                pos = 4
                if afc & 2:
                    pos = 5 + packet[4]
                if pos >= TS:
                    continue
                es.extend(packet[pos:])
    data = _remove_emulation(bytes(es))
    positions = []
    index = data.find(b"\x00\x00\x01")
    while index >= 0 and index + 4 <= len(data):
        nal_type = data[index + 3] & 0x1F
        positions.append((index, nal_type))
        index = data.find(b"\x00\x00\x01", index + 3)
    access_units = []
    current = None
    for position, nal_type in positions:
        if nal_type in (6, 7, 8, 9):
            if current is not None and nal_type in (6, 7, 9):
                access_units.append(current)
                current = None
            continue
        if nal_type not in (1, 2, 5):
            continue
        payload = data[position + 4:position + 4 + 64]
        try:
            first_mb, pos = _ue(payload, 0)
            slice_type, _ = _ue(payload, pos)
        except (IndexError, ValueError):
            continue
        if current is None or first_mb == 0:
            if current is not None:
                access_units.append(current)
            current = {"slices": 0, "type": slice_type % 5}
        current["slices"] += 1
    if current is not None:
        access_units.append(current)
    if not access_units:
        return {"frames": 0}
    counts = [unit["slices"] for unit in access_units]
    types = [unit["type"] for unit in access_units]
    return {
        "frames": len(access_units),
        "slices_max": max(counts),
        "slices_median": statistics.median(counts),
        "multislice_frames": sum(1 for value in counts if value > 1),
        "i_frames": types.count(2),
        "p_frames": types.count(0),
        "b_frames": types.count(1),
    }


def audio_metrics(path: Path, audio_pid: int) -> dict:
    packets = ffprobe_json([
        "-select_streams", "a:0", "-show_packets",
        "-show_entries", "packet=pts_time,duration_time,size", str(path),
    ], "ffprobe").get("packets", [])
    stamps = []
    for packet in packets:
        if packet.get("pts_time") is None:
            continue
        stamps.append((float(packet["pts_time"]),
                       float(packet.get("duration_time") or 0)))
    if len(stamps) < 2:
        return {"audio_packets": len(packets), "audio_pts_gaps_over_500ms": None}
    # Real continuity: the next PTS must start when the previous packet ends.
    # A raw PTS delta wrongly counts the packet duration as a gap.
    gaps = [b_pts - (a_pts + a_dur)
            for (a_pts, a_dur), (b_pts, _) in zip(stamps, stamps[1:])]
    positive = [gap for gap in gaps if gap > 0.001]
    duration = stamps[-1][0] + stamps[-1][1] - stamps[0][0]
    return {
        "audio_packets": len(packets),
        "audio_pts_delta_max_ms": round(max(b[0] - a[0] for a, b in zip(stamps, stamps[1:])) * 1000, 1),
        "audio_gap_max_ms": round(max(positive, default=0.0) * 1000, 1),
        "audio_pts_max_gap_ms": round(max(positive, default=0.0) * 1000, 1),
        "audio_pts_gaps_over_500ms": sum(1 for gap in positive if gap > 0.5),
        "audio_pts_gaps_over_1000ms": sum(1 for gap in positive if gap > 1.0),
        "audio_pts_gaps_over_2000ms": sum(1 for gap in positive if gap > 2.0),
        "audio_overlaps": sum(1 for gap in gaps if gap < -0.001),
        "audio_overlap_max_ms": round(max((max(0.0, -gap) for gap in gaps), default=0.0) * 1000, 2),
        "audio_duration_s": round(duration, 2),
    }


def video_packet_metrics(packets: list[dict]) -> dict:
    """Use presentation intervals, including the tail after the last keyframe.

    Sorting PTS handles B-frame packet order. Union coverage excludes missing
    intervals, so a timestamp jump cannot turn a short capture into a long one.
    Missing/invalid timestamps and durations are reported, never fabricated.
    """
    intervals = []
    for packet in packets:
        try:
            start = float(packet["pts_time"])
            duration = float(packet["duration_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(duration) or not 0 < duration <= 1:
            continue
        intervals.append((start, start + duration))
    intervals.sort()
    if not intervals:
        return {"video_packet_span_s": None, "video_packet_covered_s": None,
                "video_packets": len(packets), "video_packets_timestamped": 0}
    first = intervals[0][0]
    last = first
    covered = 0.0
    for start, end in intervals:
        covered += max(0.0, end - max(start, last))
        last = max(last, end)
    return {"video_packet_span_s": round(last - first, 6),
            "video_packet_covered_s": round(covered, 6),
            "video_packets": len(packets),
            "video_packets_timestamped": len(intervals)}


def analyse(path: Path, probe: dict) -> dict:
    streams = ffprobe_json(["-show_streams", str(path)], "ffprobe").get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    # Explicit analysis limits: the full per-frame listing is CPU-bound, so it
    # is restricted to a fixed window of the sample; resolution stability over
    # the whole sample comes from a cheap keyframe-only scan.
    frames = ffprobe_json([
        "-select_streams", "v:0", "-show_frames", "-show_entries",
        "frame=width,height,pict_type,key_frame,best_effort_timestamp_time",
        "-read_intervals", "%+150", str(path),
    ], "ffprobe", timeout=600).get("frames", [])
    keyframes = ffprobe_json([
        "-select_streams", "v:0", "-skip_frame", "nokey", "-show_frames",
        "-show_entries", "frame=width,height,best_effort_timestamp_time", str(path),
    ], "ffprobe", timeout=600).get("frames", [])
    widths = sorted({f.get("width") for f in frames + keyframes if f.get("width")})
    heights = sorted({f.get("height") for f in frames + keyframes if f.get("height")})
    pict = [f.get("pict_type") for f in frames]
    key_times = [float(f["best_effort_timestamp_time"]) for f in frames
                 if f.get("key_frame") and f.get("best_effort_timestamp_time")]
    idr_gaps = [round(b - a, 3) for a, b in zip(key_times, key_times[1:])]
    frame_times = [float(f["best_effort_timestamp_time"]) for f in frames
                   if f.get("best_effort_timestamp_time")]
    internal_gaps = sum(1 for a, b in zip(frame_times, frame_times[1:]) if b - a > 1.5)
    video_pid = None
    audio_pid = None

    def pid_of(value):
        if isinstance(value, int):
            return value
        try:
            return int(str(value), 16)
        except (TypeError, ValueError):
            return None

    for stream in streams:
        if stream.get("codec_type") == "video" and video_pid is None:
            video_pid = pid_of(stream.get("id"))
        elif stream.get("codec_type") == "audio" and audio_pid is None:
            audio_pid = pid_of(stream.get("id"))
    slices = video_slices(path, video_pid) if video_pid is not None else {}
    audio_stats = audio_metrics(path, audio_pid) if audio_pid is not None else {}
    spans = [value for value in (
        audio_stats.get("audio_duration_s"),
        (max(key_times) - min(key_times))
        if len(key_times) > 1 and not audio_stats.get("audio_duration_s") else None,
    ) if value]
    media_duration = round(max(spans), 2) if spans else round(
        probe.get("requested_duration") or probe["wall_seconds"], 2)
    total_bps = None
    if media_duration > 0:
        total_bps = round(probe["bytes"] * 8 / media_duration / 1000)
    video_bps = None
    video_span = None
    packets = ffprobe_json([
        "-select_streams", "v:0", "-show_packets",
        "-show_entries", "packet=pts_time,duration_time,size", str(path),
    ], "ffprobe", timeout=600).get("packets", [])
    packet_metrics = video_packet_metrics(packets)
    if frames:
        # The frame listing is windowed; the keyframe scan covers the whole
        # sample and supplies the real span for the video bitrate.
        key_stamps = [float(f["best_effort_timestamp_time"]) for f in keyframes
                      if f.get("best_effort_timestamp_time")]
        if len(key_stamps) > 1:
            video_span = max(key_stamps) - min(key_stamps)
        else:
            first, last = frames[0], frames[-1]
            if first.get("best_effort_timestamp_time") and last.get("best_effort_timestamp_time"):
                video_span = float(last["best_effort_timestamp_time"]) - float(
                    first["best_effort_timestamp_time"])
        if video_span and video_span > 0:
            total = sum(int(p.get("size", 0)) for p in packets)
            bitrate_span = packet_metrics["video_packet_covered_s"] or video_span
            video_bps = round(total * 8 / bitrate_span / 1000)
    return {
        "video_codec": video.get("codec_name"),
        "profile": video.get("profile"),
        "level": video.get("level"),
        "width": video.get("width"),
        "height": video.get("height"),
        "widths_seen": widths,
        "heights_seen": heights,
        "fps": video.get("avg_frame_rate"),
        "pix_fmt": video.get("pix_fmt"),
        "has_b_frames": video.get("has_b_frames"),
        "b_frames_seen": pict.count("B"),
        "i_frames_seen": pict.count("I"),
        "p_frames_seen": pict.count("P"),
        "idr_gap_median_s": round(statistics.median(idr_gaps), 3) if idr_gaps else None,
        "idr_gap_max_s": max(idr_gaps) if idr_gaps else None,
        "slices": slices,
        "audio_codec": audio.get("codec_name"),
        "audio_sample_rate": audio.get("sample_rate"),
        "audio_channels": audio.get("channels"),
        "audio_bit_rate": audio.get("bit_rate"),
        "audio": audio_stats,
        "pcr": pcr_metrics(path),
        "video_kbps": video_bps,
        "video_span_s": round(video_span, 3) if video_span else None,
        **packet_metrics,
        "ts_kbps_total": total_bps,
        "media_duration_s": media_duration,
        "capture_coverage": (round(media_duration / probe["requested_duration"], 4)
                             if probe.get("requested_duration") else None),
        "video_internal_gaps_over_1s": internal_gaps,
        "frames_decoded": len(frames),
        "frame_window_s": 150,
        "keyframes_scanned": len(keyframes),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--phase", choices=("triage", "final", "winner"), default="triage")
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "scratch" / "grade-1080p" / "evidence")
    parser.add_argument("--keep-media", action="store_true")
    parser.add_argument("--resume", action="store_true",
                        help="skip candidates whose phase record already exists")
    parser.add_argument("--only", default="", help="comma separated channel ids")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()
    duration = args.duration or {"triage": 18, "final": 120, "winner": 300}[args.phase]
    only = {value for value in args.only.split(",") if value}
    env = load_env()
    plan = json.loads(args.plan.read_text())
    args.out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for identity in plan["identities"]:
        for candidate in identity["candidates"]:
            channel_id = str(candidate["channel_id"])
            if only and channel_id not in only:
                continue
            suffix = f"{identity['identity']}-{channel_id}"
            path = args.out_dir / f"{args.phase}-{suffix}.json"
            if args.resume and path.exists():
                try:
                    results.append(json.loads(path.read_text()))
                except json.JSONDecodeError:
                    pass
                continue
            record = {
                "identity": identity["identity"],
                "category": identity["category"],
                "filename": identity["filename"],
                "phase": args.phase,
                "duration_s": duration,
                "channel_id": channel_id,
                "catalog_name": candidate["catalog_name"],
                "region": candidate["region"],
                "quality_label": candidate["quality_label"],
            }
            target = Path(tempfile.mkdtemp(prefix="fav-probe-", dir="/tmp/opencode")) / "sample.ts"
            try:
                url = source_url(env, channel_id)
                print(f"[{args.phase}] {identity['identity']} id={channel_id} "
                      f"({candidate['quality_label']}, {duration:.0f}s) ...", flush=True)
                probe = run_probe_ffmpeg(url, duration, target, args.ffmpeg)
                probe["requested_duration"] = duration
                record["capture"] = probe
                if target.exists() and probe["bytes"] > TS * 100:
                    try:
                        record["media"] = analyse(target, probe)
                    except Exception as error:  # noqa: BLE001 - one sample must not abort the phase
                        record["analysis_error"] = type(error).__name__
                record["verdict"] = verdict(record)
            except Exception as error:  # noqa: BLE001 - keep measuring the remaining identities
                record.setdefault("capture", {})
                record["analysis_error"] = type(error).__name__
                record["verdict"] = "probe_error"
            finally:
                if not args.keep_media:
                    shutil.rmtree(target.parent, ignore_errors=True)
            path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
            results.append(record)
            print(f"    -> {record.get('verdict')}", flush=True)
    csv_path = args.out_dir / f"{args.phase}-summary.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "identity", "category", "filename", "channel_id", "catalog_name", "region",
            "quality_label", "phase", "duration_s", "media_duration_s", "width", "height", "fps", "profile",
            "codec", "b_frames", "slices_max", "idr_gap_s", "video_kbps", "ts_kbps",
            "audio_codec", "audio_gaps_500ms", "pcr_max_gap_ms", "errors", "verdict",
        ])
        for row in results:
            media = row.get("media", {})
            writer.writerow([
                row["identity"], row["category"], row["filename"], row["channel_id"],
                row["catalog_name"], row["region"], row["quality_label"], row["phase"],
                row["duration_s"], media.get("media_duration_s"), media.get("width"), media.get("height"), media.get("fps"),
                media.get("profile"), media.get("video_codec"), media.get("b_frames_seen"),
                (media.get("slices") or {}).get("slices_max"),
                media.get("idr_gap_median_s"), media.get("video_kbps"),
                media.get("ts_kbps_total"), media.get("audio_codec"),
                (media.get("audio") or {}).get("audio_pts_gaps_over_500ms"),
                (media.get("pcr") or {}).get("pcr_max_gap_ms"),
                json.dumps(row["capture"].get("stderr_categories", {}), sort_keys=True),
                row.get("verdict"),
            ])
    print(f"wrote {len(results)} records to {args.out_dir}", flush=True)
    return 0


def verdict(record: dict) -> str:
    capture = record.get("capture", {})
    media = record.get("media")
    if not media and record.get("analysis_error"):
        # Sample captured but local analysis failed: never silently downgrade
        # this to no_media, the source itself may be fine.
        return "probe_error"
    if not media or capture.get("bytes", 0) < TS * 1000:
        return "no_media"
    errors = capture.get("stderr_categories", {})
    if any(count for name, count in errors.items() if name.startswith("http_") or name in (
            "connection_refused", "dns_failure", "input_open_failed")):
        return "unavailable"
    if media.get("width") != 1920 or media.get("height") != 1080:
        return "not_1080p"
    if len(media.get("widths_seen") or []) > 1 or len(media.get("heights_seen") or []) > 1:
        return "resolution_unstable"
    if not media.get("audio_codec"):
        return "no_audio"
    if errors.get("decode_error", 0) > 3:
        return "decode_errors"
    if (capture.get("returncode") or 0) != 0:
        return "input_error"
    return "candidate_ok"


if __name__ == "__main__":
    sys.exit(main())
