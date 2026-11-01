"""Tests for the Live byte-level endpoint probe.

A fake manager implements the real wire protocol (OPEN handshake + HMAC-framed
MPEG-TS payloads) so the probe is validated against actual bytes, not against a
mock of the probe itself.
"""
import socket
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import probe_mixed_live_endpoint as probe  # noqa: E402
from favorites_source_manager import (  # noqa: E402
    FRAME_HEADER,
    FRAME_TAG_LEN,
    frame_bytes,
)

SESSION = "mixed-gen2-aaaaaaaa"
MANIFEST_ID = "a" * 64
CHANNEL_ID = "206080"
FILE_ID = 1234
GENERATION = 2
EPOCH = 7
TOKEN = "probe-test-token-0123456789abcdef0123456789abcdef"


def ts_packet(payload_byte=0xAA):
    return bytes([0x47, 0x01, 0x00, 0x10]) + bytes([payload_byte]) * 184


class FakeManager(threading.Thread):
    """Serves the documented manager protocol on a loopback port."""

    def __init__(self, *, reply=b"OK 2 7\n", frames=1, corrupt=False,
                 payload=None, handshake_garbage=False):
        super().__init__(daemon=True)
        self.reply = reply
        self.frames = frames
        self.corrupt = corrupt
        self.payload = payload if payload is not None else ts_packet()
        self.handshake_garbage = handshake_garbage
        self.seen = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.error = None

    def run(self):
        try:
            conn, _ = self.listener.accept()
            with conn:
                line = b""
                while not line.endswith(b"\n"):
                    chunk = conn.recv(1)
                    if not chunk:
                        return
                    line += chunk
                self.seen.append(line.decode().strip())
                if self.handshake_garbage:
                    conn.sendall(b"TVFT not a real reply\n")
                    return
                conn.sendall(self.reply)
                if not self.reply.startswith(b"OK "):
                    return
                request = {
                    "session_id": SESSION,
                    "manifest_id": MANIFEST_ID,
                    "channel_id": CHANNEL_ID,
                    "file_id": FILE_ID,
                    "request_generation": GENERATION,
                    "source_epoch": EPOCH,
                }
                for sequence in range(self.frames):
                    if self.corrupt:
                        header = FRAME_HEADER.pack(
                            b"TVFT", 1, 0, FRAME_HEADER.size, FILE_ID,
                            GENERATION, EPOCH, sequence, len(self.payload),
                            len(SESSION), len(MANIFEST_ID), len(CHANNEL_ID))
                        conn.sendall(header + b"x" * (len(SESSION) + len(MANIFEST_ID)
                                                      + len(CHANNEL_ID))
                                     + self.payload + b"\x00" * FRAME_TAG_LEN)
                    else:
                        conn.sendall(frame_bytes(request, sequence, self.payload, TOKEN))
        except OSError as exc:  # pragma: no cover
            self.error = exc


class ProbeMixedLiveEndpointTests(unittest.TestCase):
    def probe(self, manager):
        manager.start()
        return probe.probe_endpoint(
            host="127.0.0.1", port=manager.port, session=SESSION,
            manifest_id=MANIFEST_ID, channel_id=CHANNEL_ID, file_id=FILE_ID,
            generation=GENERATION, epoch=EPOCH, token=TOKEN, frames=2, timeout=5.0)

    def test_real_ts_frames_prove_the_live_path(self):
        manager = FakeManager(frames=2)
        verdict = self.probe(manager)
        manager.join(timeout=5)
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(verdict["handshake"], "ok")
        self.assertEqual(verdict["frames"], 2)
        self.assertEqual(verdict["ts_bytes"], 2 * 188)
        self.assertTrue(verdict["payloads_are_ts"])
        self.assertEqual(manager.seen[0].split()[0], "OPEN")

    def test_manager_denial_fails_the_probe(self):
        manager = FakeManager(reply=b"ERR source unavailable\n")
        verdict = self.probe(manager)
        manager.join(timeout=5)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["handshake"], "denied")

    def test_garbage_handshake_reply_fails_the_probe(self):
        manager = FakeManager(handshake_garbage=True)
        verdict = self.probe(manager)
        manager.join(timeout=5)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["handshake"], "denied")

    def test_bad_frame_tag_is_rejected(self):
        manager = FakeManager(corrupt=True)
        verdict = self.probe(manager)
        manager.join(timeout=5)
        self.assertFalse(verdict["ok"])
        self.assertIsNotNone(verdict["error"])

    def test_non_ts_payload_is_rejected(self):
        manager = FakeManager(payload=b"A" * 188)
        verdict = self.probe(manager)
        manager.join(timeout=5)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["error"], "payload is not aligned MPEG-TS")

    def test_unreachable_endpoint_fails_closed(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        verdict = probe.probe_endpoint(
            host="127.0.0.1", port=port, session=SESSION, manifest_id=MANIFEST_ID,
            channel_id=CHANNEL_ID, file_id=FILE_ID, generation=GENERATION,
            epoch=EPOCH, token=TOKEN, frames=1, timeout=2.0)
        self.assertFalse(verdict["ok"])
        self.assertIsNotNone(verdict["error"])

    def test_verdict_never_contains_the_token(self):
        manager = FakeManager()
        verdict = self.probe(manager)
        manager.join(timeout=5)
        self.assertNotIn(TOKEN, repr(verdict))
        self.assertNotIn(TOKEN[:8], repr(verdict))


if __name__ == "__main__":
    unittest.main()
