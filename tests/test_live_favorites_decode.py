"""Decode evidence must cover the requested sample and fail closed."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import probe_live_favorites_decode as probe  # noqa: E402
import probe_live_favorites_sources as sources  # noqa: E402


def valid():
    return ({"returncode": 0, "bytes": 100_000},
            {"video_span_s": 57, "video_packet_span_s": 60,
             "video_packet_covered_s": 59.9, "video_packets": 1800,
             "video_packets_timestamped": 1800, "frames_decoded": 1800,
             "audio": {"audio_duration_s": 59.9}},
            {"returncode": 0, "lines": 0})


class DecodeVerdictTests(unittest.TestCase):
    def test_decode_pass_uses_one_thread_and_rawvideo(self):
        """Regra do projeto: decodificacao deterministica, sem ruido do muxer.

        Duas rodadas do mesmo arquivo alternavam entre ``decode_ok`` e
        ``decode_errors`` por corrida do decodificador multithread, e o muxer
        ``-f null`` acrescentava avisos de DTS que nao sao dano no stream. A
        referencia passou a ser: um thread, ``rawvideo`` para /dev/null, e
        qualquer linha de erro reprova.
        """
        import tempfile
        from pathlib import Path as _Path

        captured = {}

        class _Done:
            returncode = 0
            stderr = b""

        def fake_run(command, capture_output=True, timeout=None):
            captured["command"] = command
            return _Done()

        original = probe.subprocess.run
        probe.subprocess.run = fake_run
        try:
            with tempfile.TemporaryDirectory() as tmp:
                sample = _Path(tmp) / "capture.ts"
                sample.write_bytes(b"not a real stream")
                probe.decode_pass(sample, 60, "ffmpeg")
        finally:
            probe.subprocess.run = original
        command = captured["command"]
        self.assertIn("-threads", command)
        self.assertEqual(command[command.index("-threads") + 1], "1")
        self.assertIn("rawvideo", command)
        self.assertIn("s16le", command)      # o áudio não pode ir para rawvideo
        self.assertNotIn("null", command)
        self.assertEqual(command.count("/dev/null"), 2)

    def test_complete_clean_sample_passes(self):
        self.assertEqual(probe.decode_verdict(*valid(), 60)[0], "decode_ok")

    def test_failed_capture_cannot_be_approved_even_if_partial_file_decodes(self):
        capture, media, decode = valid()
        capture["returncode"] = -1
        verdict, details = probe.decode_verdict(capture, media, decode, 60)
        self.assertEqual(verdict, "decode_errors")
        self.assertIn("capture_ok", details["failed"])

    def test_truncated_or_missing_video_and_audio_are_rejected(self):
        for field, value, failed in [("video_packet_span_s", 20, "video_coverage"),
                                     ("video_packet_span_s", None, "video_coverage"),
                                     ("video_packet_covered_s", 20, "video_coverage"),
                                     ("video_packet_covered_s", None, "video_coverage"),
                                     ("video_packets_timestamped", 10, "video_timestamps_valid"),
                                     ("audio", {"audio_duration_s": 20}, "audio_coverage"),
                                     ("audio", {}, "audio_coverage")]:
            with self.subTest(field=field, value=value):
                capture, media, decode = valid()
                media[field] = value
                # The generic duration must not fill in absent stream timestamps.
                media["media_duration_s"] = 60
                verdict, details = probe.decode_verdict(capture, media, decode, 60)
                self.assertEqual(verdict, "decode_errors")
                self.assertIn(failed, details["failed"])

    def test_decode_errors_and_nonfinite_duration_fail(self):
        capture, media, decode = valid()
        decode["lines"] = 1
        self.assertEqual(probe.decode_verdict(capture, media, decode, 60)[0], "decode_errors")
        self.assertEqual(probe.decode_verdict(*valid(), float("nan"))[0], "decode_errors")

    def test_packet_measurement_handles_reordered_frames_and_gaps(self):
        packets = [{"pts_time": "2", "duration_time": "1"},
                   {"pts_time": "0", "duration_time": "1"},
                   {"pts_time": "1", "duration_time": "1"},
                   {"pts_time": "5", "duration_time": "1"}]
        metrics = sources.video_packet_metrics(packets)
        self.assertEqual(metrics["video_packet_span_s"], 6)
        self.assertEqual(metrics["video_packet_covered_s"], 4)
        capture, media, decode = valid()
        media.update(metrics)
        self.assertEqual(probe.decode_verdict(capture, media, decode, 6)[0], "decode_errors")

    def test_packets_with_missing_or_invalid_timestamps_do_not_prove_coverage(self):
        metrics = sources.video_packet_metrics([
            {"pts_time": "NaN", "duration_time": "1"},
            {"pts_time": "0", "duration_time": "100"},
            {"pts_time": "0"},
        ])
        self.assertIsNone(metrics["video_packet_covered_s"])
        self.assertEqual(metrics["video_packets_timestamped"], 0)

    def test_keyframe_tail_is_not_used_as_duration(self):
        capture, media, decode = valid()
        self.assertEqual(media["video_span_s"], 57)
        self.assertEqual(probe.decode_verdict(capture, media, decode, 60)[0], "decode_ok")


if __name__ == "__main__":
    unittest.main()
