import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from live_favorites_pcr import (
    MAX_INTERVAL_PACKETS, PCR_LIMIT, PCR_MOD, PcrBounder, packet_pid, pcr_packet, pcr_value,
)


NULL = bytes([0x47, 0x1F, 0xFF, 0x10]) + bytes([0xFF]) * 184
VIDEO = bytes([0x47, 0x01, 0x00, 0x18]) + bytes([0xFF]) * 184


class PcrBounderTests(unittest.TestCase):
    def test_replaces_null_at_proportional_position_preserving_bytes_and_cc(self):
        start = 1_000_000
        data = pcr_packet(0x100, 7, start) + VIDEO + NULL * 54 + pcr_packet(0x100, 8, start + 568_512)
        bounder = PcrBounder(4_000_000)
        output = bounder.feed(data) + bounder.finish()
        self.assertEqual(len(output), len(data))
        changes = [index for index in range(0, len(data), 188) if data[index:index + 188] != output[index:index + 188]]
        self.assertEqual(changes, [28 * 188])
        replacement = output[changes[0]:changes[0] + 188]
        self.assertEqual(packet_pid(replacement), 0x100)
        self.assertEqual(replacement[3] & 0x0F, 8)
        self.assertEqual(pcr_value(replacement), start + 568_512 // 2)
        self.assertEqual(replacement[3] >> 4, 2)  # adaptation only
        self.assertEqual(bounder.replaced_null_packets, 1)

    def test_streaming_chunks_and_pcr_wrap_preserve_identical_result(self):
        start = PCR_MOD - 300_000
        data = pcr_packet(0x100, 7, start) + NULL * 55 + pcr_packet(0x100, 7, start + 568_512)
        one = PcrBounder(4_000_000)
        expected = one.feed(data) + one.finish()
        many = PcrBounder(4_000_000)
        actual = b"".join(many.feed(data[index:index + 188]) for index in range(0, len(data), 188)) + many.finish()
        self.assertEqual(actual, expected)
        values = [value for index in range(0, len(actual), 188)
                  if (value := pcr_value(actual[index:index + 188])) is not None]
        self.assertTrue(all(0 < (b - a) % PCR_MOD <= PCR_LIMIT for a, b in zip(values, values[1:])))

    def test_fails_closed_without_null_or_with_excessive_lookahead(self):
        data = pcr_packet(0x100, 7, 1_000_000) + VIDEO * 55 + pcr_packet(0x100, 8, 1_568_512)
        with self.assertRaisesRegex(ValueError, "no NULL"):
            PcrBounder(4_000_000).feed(data)
        with self.assertRaisesRegex(ValueError, "lookahead exceeded"):
            PcrBounder().feed(pcr_packet(0x100, 7, 1_000_000) + NULL * (MAX_INTERVAL_PACKETS + 1))

    def test_finite_tail_and_already_short_intervals_are_unchanged(self):
        data = pcr_packet(0x100, 7, 1_000_000) + NULL * 20 + pcr_packet(0x100, 7, 1_142_128) + NULL * 5
        bounder = PcrBounder()
        self.assertEqual(bounder.feed(data) + bounder.finish(), data)
        self.assertEqual(bounder.replaced_null_packets, 0)


if __name__ == "__main__":
    unittest.main()
