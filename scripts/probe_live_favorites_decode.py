#!/usr/bin/env python3
"""Decode-check the chosen Live source (sanitized evidence).

Capture alone (``-c copy``) never proves the bitstream decodes cleanly: ffmpeg
can copy corrupt packets without complaint. This probe captures a short sample
and then runs a full decode pass (``-threads 1`` + ``rawvideo``) so decode
errors are measured
explicitly. URLs, credentials and raw stderr never leave the harness.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import probe_live_favorites_sources as probe  # noqa: E402

TS = 188
MIN_COVERAGE = 0.97


def decode_verdict(capture: dict, media: dict, decode: dict, requested: float) -> tuple[str, dict]:
    """Never infer a successful full decode from a truncated capture or missing data."""
    def covered(value: object) -> bool:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= requested * MIN_COVERAGE)

    valid_request = (isinstance(requested, (int, float)) and not isinstance(requested, bool)
                     and math.isfinite(requested) and requested > 0)
    checks = {
        "valid_duration": valid_request,
        "capture_ok": capture.get("returncode") == 0,
        "capture_has_media": (capture.get("bytes") or 0) > TS * 100,
        # media_duration_s may fall back to the requested duration when ffprobe
        # found no timestamps. Require independently measured stream spans.
        "video_coverage": (valid_request and covered(media.get("video_packet_span_s"))
                           and covered(media.get("video_packet_covered_s"))),
        "video_timestamps_valid": (
            isinstance(media.get("video_packets"), int)
            and not isinstance(media.get("video_packets"), bool)
            and media["video_packets"] >= 2
            and isinstance(media.get("video_packets_timestamped"), int)
            and not isinstance(media.get("video_packets_timestamped"), bool)
            and 0 < media["video_packets_timestamped"] <= media["video_packets"]
            and media["video_packets_timestamped"] >= media["video_packets"] * MIN_COVERAGE),
        "audio_coverage": valid_request and covered((media.get("audio") or {}).get("audio_duration_s")),
        "video_frames": (media.get("frames_decoded") or 0) > 0,
        "decode_process_ok": decode.get("returncode") == 0,
        "decode_clean": decode.get("lines") == 0,
    }
    failed = [name for name, passed in checks.items() if not passed]
    return ("decode_ok" if not failed else "decode_errors"), {"checks": checks, "failed": failed}


def decode_pass(source: Path, seconds: float, ffmpeg: str) -> dict:
    # Reference rule for this project (see AGENTS.md §2.4):
    #   * one decode thread, so the *command* is deterministic and a re-run of the
    #     same capture gives the same verdict (measured: 2/2/2 and 3/3);
    #   * raw outputs instead of `-f null`: the null muxer's own
    #     "non monotonically increasing dts" lines come from the muxing stage, not
    #     from the stream (measured: the line disappears without the null muxer);
    #   * any error line rejects the SAMPLE as a precaution while it is
    #     investigated. Stage and origin are NOT established by this check, and
    #     nothing here says why the payload is the way it is.
    # `-threads 1` + raw outputs. Note: `-f rawvideo` cannot carry the audio track
    # (ffmpeg then fails with "Encoder not found"), so video and audio go to
    # /dev/null through their own raw formats: the decode is what is measured, and
    # the muxer that used to emit its own DTS warnings is out of the picture.
    command = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-threads", "1",
               "-i", str(source), "-t", str(seconds),
               "-map", "0:v:0", "-f", "rawvideo", "-y", "/dev/null",
               "-map", "0:a:0", "-f", "s16le", "-y", "/dev/null"]
    started = time.monotonic()
    try:
        done = subprocess.run(command, capture_output=True, timeout=900)
        returncode = done.returncode
        stderr = done.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as error:
        returncode = -1
        stderr = (error.stderr or b"").decode("utf-8", "replace")
    lines = [line for line in stderr.splitlines() if line.strip()]
    return {
        "returncode": returncode,
        "wall_seconds": round(time.monotonic() - started, 3),
        "lines": len(lines),
        "categories": probe.classify_stderr(stderr),
        "checked_seconds": seconds,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path,
                        default=ROOT / "scratch" / "grade-1080p" / "decode-evidence")
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--only", default="")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration <= 0:
        parser.error("--duration must be a finite positive number")
    config = json.loads(args.export.read_text())
    only = {value for value in args.only.split(",") if value}
    environment = probe.load_env()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in config["identities"]:
        chosen = item.get("chosen") or {}
        channel = str(chosen.get("channel_id", ""))
        if not channel or (only and channel not in only):
            continue
        record = {
            "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "identity": item["identity"],
            "channel_id": channel,
            "catalog_name": chosen.get("catalog_name"),
            "status": item.get("status"),
            "duration_s": args.duration,
        }
        directory = Path(tempfile.mkdtemp(prefix="fav-decode-", dir="/tmp/opencode"))
        sample = directory / "sample.ts"
        try:
            print(f"[decode] {item['identity']} id={channel} ...", flush=True)
            url = probe.source_url(environment, channel)
            capture = probe.run_probe_ffmpeg(url, args.duration, sample, args.ffmpeg)
            capture["requested_duration"] = args.duration
            record["capture"] = capture
            if sample.exists() and capture["bytes"] > TS * 100:
                media = probe.analyse(sample, capture)
                record["media"] = {
                    "video_decoded_frames": media.get("frames_decoded"),
                    "video_duration_s": media.get("media_duration_s"),
                    "video_span_s": media.get("video_span_s"),
                    "video_packet_span_s": media.get("video_packet_span_s"),
                    "video_packet_covered_s": media.get("video_packet_covered_s"),
                    "video_packets": media.get("video_packets"),
                    "video_packets_timestamped": media.get("video_packets_timestamped"),
                    "audio_duration_s": (media.get("audio") or {}).get("audio_duration_s"),
                    "audio_packets": (media.get("audio") or {}).get("audio_packets"),
                    "width": media.get("width"),
                    "height": media.get("height"),
                    "audio_codec": media.get("audio_codec"),
                }
                record["decode"] = decode_pass(sample, args.duration, args.ffmpeg)
                record["verdict"], record["validation"] = decode_verdict(
                    capture, media, record["decode"], args.duration)
            else:
                record["verdict"] = probe.verdict(record)
        except Exception as error:  # noqa: BLE001 - never approve silently
            record["analysis_error"] = type(error).__name__
            record["verdict"] = "probe_error"
        finally:
            shutil.rmtree(directory, ignore_errors=True)
        path = args.out_dir / f"decode-{item['identity']}-{channel}.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        rows.append(record)
        print(f"    -> {record.get('verdict')}", flush=True)
    csv_path = args.out_dir / "decode-summary.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["identity", "channel_id", "status", "captured_bytes",
                         "decode_returncode", "decode_lines", "checked_seconds",
                         "categories", "verdict"])
        for row in rows:
            decode = row.get("decode") or {}
            writer.writerow([
                row["identity"], row["channel_id"], row.get("status"),
                (row.get("capture") or {}).get("bytes"), decode.get("returncode"),
                decode.get("lines"), decode.get("checked_seconds"),
                json.dumps(decode.get("categories") or {}, sort_keys=True),
                row.get("verdict"),
            ])
    print(json.dumps({"records": len(rows), "csv": str(csv_path)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
