"""Output validator: strict TV invariants after the bridge PCR filter."""

import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import probe_live_favorites_output as output  # noqa: E402
from favorites_source_manager import PROFILES  # noqa: E402


def media(**overrides):
    base = {
        "width": 1920,
        "height": 1080,
        "widths_seen": [1920],
        "heights_seen": [1080],
        "b_frames_seen": 0,
        "idr_gap_median_s": 1.0,
        "idr_gap_max_s": 1.01,
        "slices": {"slices_max": 1},
        "audio_codec": "ac3",
        "audio_sample_rate": "48000",
        "audio_channels": 2,
        "audio_bit_rate": "384000",
        "audio": {"audio_pts_gaps_over_500ms": 0, "audio_overlaps": 0},
        "pcr": {"pcr_max_gap_ms": 20.0},
    }
    base.update(overrides)
    return base


def capture(**overrides):
    base = {"returncode": 0, "stderr_categories": {},
            "bounder": {"replaced_null_packets": 12, "error": None}}
    base.update(overrides)
    return base


class OutputVerdictTests(unittest.TestCase):
    def test_compliant_output_passes(self):
        verdict, details = output.output_verdict(media(), capture(), PROFILES["1080p30"])
        self.assertEqual(verdict, "output_ok", details["failed"])
        self.assertEqual(details["failed"], [])

    def test_pcr_above_twenty_milliseconds_fails(self):
        result, details = output.output_verdict(
            media(pcr={"pcr_max_gap_ms": 20.5}), capture(), PROFILES["1080p30"])
        self.assertEqual(result, "output_fail")
        self.assertIn("pcr_interval", details["failed"])

    def test_ffmpeg_process_error_fails(self):
        result, details = output.output_verdict(
            media(), capture(returncode=1), PROFILES["1080p30"])
        self.assertEqual(result, "output_fail")
        self.assertIn("process_ok", details["failed"])

    def test_pcr_filter_failure_fails(self):
        result, details = output.output_verdict(
            media(), capture(bounder={"replaced_null_packets": 0,
                                      "error": "ValueError"}),
            PROFILES["1080p30"])
        self.assertEqual(result, "output_fail")
        self.assertIn("pcr_filter_ok", details["failed"])

    def test_missing_filter_evidence_fails(self):
        result, details = output.output_verdict(
            media(), {"returncode": 0, "stderr_categories": {}}, PROFILES["1080p30"])
        self.assertEqual(result, "output_fail")
        self.assertIn("pcr_filter_ok", details["failed"])

    def test_missing_stderr_evidence_fails(self):
        result, details = output.output_verdict(
            media(), {"returncode": 0, "bounder": {"error": None}}, PROFILES["1080p30"])
        self.assertEqual(result, "output_fail")
        self.assertIn("stderr_clean", details["failed"])

    def test_wrong_audio_bitrate_fails(self):
        result, details = output.output_verdict(
            media(audio_bit_rate="128000"), capture(), PROFILES["1080p30"])
        self.assertEqual(result, "output_fail")
        self.assertIn("audio_ac3_384k", details["failed"])

    def test_audio_gap_fails(self):
        result, details = output.output_verdict(
            media(audio={"audio_pts_gaps_over_500ms": 1, "audio_overlaps": 0}),
            capture(), PROFILES["1080p30"])
        self.assertEqual(result, "output_fail")
        self.assertIn("audio_continuous", details["failed"])

    def test_small_ac3_timestamp_overlap_is_tolerated(self):
        result, details = output.output_verdict(
            media(audio={"audio_pts_gaps_over_500ms": 0, "audio_overlaps": 5,
                         "audio_overlap_max_ms": 0.4}),
            capture(), PROFILES["1080p30"])
        self.assertEqual(result, "output_ok", details["failed"])

    def test_real_overlap_fails(self):
        result, details = output.output_verdict(
            media(audio={"audio_pts_gaps_over_500ms": 0, "audio_overlaps": 1,
                         "audio_overlap_max_ms": 50.0}),
            capture(), PROFILES["1080p30"])
        self.assertEqual(result, "output_fail")
        self.assertIn("audio_continuous", details["failed"])

    def test_idr_max_gap_fails(self):
        result, details = output.output_verdict(
            media(idr_gap_max_s=2.5), capture(), PROFILES["1080p30"])
        self.assertEqual(result, "output_fail")
        self.assertIn("idr_one_second", details["failed"])

    def test_stderr_and_decode_error_fail(self):
        result, details = output.output_verdict(
            media(), capture(stderr_categories={"decode_error": 1}),
            PROFILES["1080p30"])
        self.assertEqual(result, "output_fail")
        self.assertIn("no_decode_errors", details["failed"])


if __name__ == "__main__":
    unittest.main()
