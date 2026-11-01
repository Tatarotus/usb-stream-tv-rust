#!/usr/bin/env python3
"""Measure the prepared TV output for each exported identity (sanitized).

For every identity in the reviewed export configuration this probe:

1. captures a short source sample from the Live catalog (``-c copy``);
2. feeds that sample through the exact manager pipeline (``FFmpegProvider``);
3. measures the prepared output against the Samsung 2013 requirements.

Source characteristics and prepared-output characteristics are stored in
separate sections so an input limitation (for example a 100 ms PCR interval in
the provider stream) is never confused with an output defect. No URL,
credential or raw stderr text is written.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import probe_live_favorites_sources as probe  # noqa: E402
from favorites_source_manager import FFmpegProvider, PROFILES  # noqa: E402
from live_favorites_pcr import PcrBounder  # noqa: E402

TS = 188
PCR_LIMIT_MS = 20.0
AC3_BITRATE = 384_000


def capture_source(url: str, duration: float, target: Path, ffmpeg: str) -> dict:
    started = time.monotonic()
    result = probe.run_probe_ffmpeg(url, duration, target, ffmpeg)
    result["capture_seconds"] = round(time.monotonic() - started, 3)
    return result


def prepare_output(source: Path, target: Path, profile: str, ffmpeg: str) -> dict:
    command = FFmpegProvider(lambda _: None, ffmpeg, PROFILES[profile]).command(str(source))
    started = time.monotonic()
    done = subprocess.run(command, capture_output=True, timeout=1800)
    elapsed = round(time.monotonic() - started, 3)
    raw = done.stdout
    bounder = PcrBounder(PROFILES[profile].muxrate)
    bound_error = None
    try:
        final = bounder.feed(raw) + bounder.finish()
    except ValueError as error:
        final = raw
        bound_error = type(error).__name__
    target.write_bytes(final)
    version = subprocess.run([ffmpeg, "-version"], capture_output=True,
                             text=True, timeout=30).stdout.splitlines()
    return {
        "returncode": done.returncode,
        "wall_seconds": elapsed,
        "raw_bytes": len(raw),
        "bytes": len(final),
        "stderr_categories": probe.classify_stderr(
            done.stderr.decode("utf-8", "replace")),
        "bounder": {
            "replaced_null_packets": bounder.replaced_null_packets,
            "error": bound_error,
        },
        "artifact_sha256": hashlib.sha256(final).hexdigest(),
        "ffmpeg_version": version[0] if version else None,
        "profile": profile,
    }


def output_verdict(media: dict, capture: dict, profile) -> tuple[str, dict]:
    slices = media.get("slices") or {}
    audio = media.get("audio") or {}
    pcr = media.get("pcr") or {}
    checks = {
        "process_ok": capture.get("returncode") == 0,
        "pcr_filter_ok": (isinstance(capture.get("bounder"), dict)
                          and "error" in capture["bounder"]
                          and capture["bounder"]["error"] is None),
        "stderr_clean": (isinstance(capture.get("stderr_categories"), dict)
                         and not capture["stderr_categories"]),
        "geometry": (media.get("width"), media.get("height")) == (profile.width, profile.height),
        "resolution_stable": (media.get("widths_seen") == [profile.width]
                              and media.get("heights_seen") == [profile.height]),
        "no_b_frames": media.get("b_frames_seen") == 0,
        "single_slice": slices.get("slices_max") == 1,
        "idr_one_second": (media.get("idr_gap_median_s") is not None
                           and 0.9 <= float(media["idr_gap_median_s"]) <= 1.1
                           and media.get("idr_gap_max_s") is not None
                           and float(media["idr_gap_max_s"]) <= 1.1),
        "pcr_interval": (pcr.get("pcr_max_gap_ms") is not None
                         and float(pcr["pcr_max_gap_ms"]) <= PCR_LIMIT_MS),
        "audio_ac3_384k": (media.get("audio_codec") == "ac3"
                           and str(media.get("audio_sample_rate")) == "48000"
                           and media.get("audio_channels") == 2
                           and int(media.get("audio_bit_rate") or 0) == AC3_BITRATE),
        # Overlaps below 5 ms are AC-3 frame timestamp rounding; real splices
        # are reported as gaps and remain fatal.
        "audio_continuous": (audio.get("audio_pts_gaps_over_500ms") == 0
                             and (audio.get("audio_overlap_max_ms") or 0) <= 5.0),
        "no_decode_errors": not capture.get("stderr_categories", {}).get("decode_error"),
    }
    failed = [name for name, ok in checks.items() if not ok]
    return ("output_ok" if not failed else "output_fail"), {"checks": checks,
                                                            "failed": failed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path,
                        default=ROOT / "scratch" / "grade-1080p" / "evidence")
    parser.add_argument("--duration", type=float, default=25)
    parser.add_argument("--only", default="", help="comma separated channel ids")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--keep-media", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.export.read_text())
    profile_name = config.get("output_profile", "1080p30")
    if profile_name not in PROFILES:
        parser.error("unknown output profile")
    profile = PROFILES[profile_name]
    only = {value for value in args.only.split(",") if value}
    environment = probe.load_env()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in config["identities"]:
        chosen = item.get("chosen") or {}
        channel_id = str(chosen.get("channel_id", ""))
        if not channel_id or (only and channel_id not in only):
            continue
        record = {
            "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "identity": item["identity"],
            "channel_id": channel_id,
            "catalog_name": chosen.get("catalog_name"),
            "region": chosen.get("region"),
            "status": item.get("status"),
            "quality_status": item.get("quality_status"),
            "output_profile": profile_name,
        }
        directory = Path(tempfile.mkdtemp(prefix="fav-output-", dir="/tmp/opencode"))
        source_file = directory / "source.ts"
        output_file = directory / "output.ts"
        try:
            print(f"[output] {item['identity']} id={channel_id} ...", flush=True)
            url = probe.source_url(environment, channel_id)
            capture = capture_source(url, args.duration, source_file, args.ffmpeg)
            record["source_capture"] = capture
            if source_file.exists() and capture["bytes"] > TS * 100:
                record["source"] = probe.analyse(source_file, capture)
            if record.get("source"):
                prepared = prepare_output(source_file, output_file, profile_name, args.ffmpeg)
                record["output_capture"] = prepared
                if output_file.exists() and prepared["bytes"] > TS * 100:
                    record["output"] = probe.analyse(output_file, prepared)
                    verdict, details = output_verdict(record["output"], prepared, profile)
                    record["verdict"] = verdict
                    record.update(details)
                else:
                    record["verdict"] = "output_missing"
            else:
                record["verdict"] = probe.verdict(record)
        except Exception as error:  # noqa: BLE001 - report, never approve silently
            record["analysis_error"] = type(error).__name__
            record.setdefault("verdict", "probe_error")
        finally:
            if not args.keep_media:
                shutil.rmtree(directory, ignore_errors=True)
        path = args.out_dir / f"output-{item['identity']}-{channel_id}.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        rows.append(record)
        print(f"    -> {record.get('verdict')}", flush=True)
    csv_path = args.out_dir / "output-summary.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "identity", "channel_id", "status", "source_width", "source_height",
            "source_pcr_max_ms", "source_audio_gaps_500ms", "output_width",
            "output_height", "output_b_frames", "output_slices_max",
            "output_idr_gap_s", "output_pcr_max_ms", "output_audio_codec",
            "output_audio_gaps_500ms", "output_ts_kbps", "verdict", "failed",
        ])
        for row in rows:
            source = row.get("source") or {}
            output = row.get("output") or {}
            writer.writerow([
                row["identity"], row["channel_id"], row.get("status"),
                source.get("width"), source.get("height"),
                (source.get("pcr") or {}).get("pcr_max_gap_ms"),
                (source.get("audio") or {}).get("audio_pts_gaps_over_500ms"),
                output.get("width"), output.get("height"),
                output.get("b_frames_seen"), (output.get("slices") or {}).get("slices_max"),
                output.get("idr_gap_median_s"),
                (output.get("pcr") or {}).get("pcr_max_gap_ms"),
                output.get("audio_codec"),
                (output.get("audio") or {}).get("audio_pts_gaps_over_500ms"),
                output.get("ts_kbps_total"), row.get("verdict"),
                ",".join(row.get("failed") or []),
            ])
    print(json.dumps({"records": len(rows), "csv": str(csv_path)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
