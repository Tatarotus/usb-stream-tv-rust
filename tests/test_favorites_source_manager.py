import pathlib
import socket
import struct
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import favorites_source_manager as fsm  # noqa: E402


class FrameTests(unittest.TestCase):
    def setUp(self):
        self.request = {
            "session_id": "session",
            "manifest_id": "manifest",
            "file_id": 7,
            "channel_id": "sbt",
            "request_generation": 3,
            "source_epoch": 4,
        }
        self.payload = bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xa1" * 184
        self.payload *= 4

    def test_roundtrip_and_authentication(self):
        wire = fsm.frame_bytes(self.request, 1, self.payload, "secret")
        request, sequence, payload = fsm.decode_frame(wire, "secret")
        self.assertEqual(sequence, 1)
        self.assertEqual(payload, self.payload)
        self.assertEqual(request["source_epoch"], 4)
        tampered = bytearray(wire)
        tampered[-1] ^= 1
        with self.assertRaises(fsm.TransportError):
            fsm.decode_frame(bytes(tampered), "secret")
        with self.assertRaises(fsm.TransportError):
            fsm.frame_bytes(self.request, 1, b"short", "secret")

    def test_align_chunks_only_emits_whole_ts_packets(self):
        raw = bytes([0x47]) * (fsm.TS_PKT_SZ * 1000 + 17)
        chunks = list(fsm.align_chunks([raw[:1000], raw[1000:]], max_payload=64 * 1024))
        self.assertGreater(len(chunks), 1)
        self.assertEqual(b"".join(chunks), raw[: fsm.TS_PKT_SZ * 1000])
        self.assertTrue(all(len(chunk) % fsm.TS_PKT_SZ == 0 for chunk in chunks))


class ControlTests(unittest.TestCase):
    def test_header_auth_has_no_query_or_fallback(self):
        headers = (
            "Authorization: Bearer secret\r\n"
            "X-Mount-Session: s1\r\n"
            "X-Manifest-Id: m1\r\n"
            "Content-Length: 0\r\n"
        )
        self.assertEqual(fsm.authenticate_headers("secret", headers), ("s1", "m1"))
        with self.assertRaises(fsm.ControlError):
            fsm.authenticate_headers("secret", "X-Mount-Session: s1\r\nX-Manifest-Id: m1\r\n")
        with self.assertRaises(fsm.ControlError):
            fsm.authenticate_headers("secret", headers.replace("Bearer secret", "Basic secret"))

    def test_generations_epochs_and_cancel(self):
        manager = fsm.FavoritesSourceManager("secret", "m1")
        headers = "Authorization: Bearer secret\r\nX-Mount-Session: s1\r\nX-Manifest-Id: m1\r\n"
        first = manager.handle_control(headers, {"operation": "prepare", "file_id": 7, "channel_id": "sbt"})
        self.assertEqual((first["request_generation"], first["source_epoch"]), (1, 1))
        same = manager.handle_control(headers, {"operation": "prepare", "file_id": 7, "channel_id": "sbt"})
        self.assertEqual(same["source_epoch"], 1)
        second = manager.handle_control(headers, {"operation": "prepare", "file_id": 8, "channel_id": "record"})
        self.assertEqual((second["request_generation"], second["source_epoch"]), (2, 2))
        self.assertIsNone(manager.authorize_media(1, 1, 7, "sbt"))
        self.assertIsNotNone(manager.authorize_media(2, 2, 8, "record"))
        with self.assertRaises(fsm.ControlError):
            manager.handle_control(headers, {"operation": "cancel", "generation": 1})
        manager.handle_control(headers, {"operation": "cancel", "generation": 2})
        self.assertIsNone(manager.grant())
        other = "Authorization: Bearer secret\r\nX-Mount-Session: s2\r\nX-Manifest-Id: m1\r\n"
        with self.assertRaises(fsm.ControlError):
            manager.handle_control(other, {"operation": "prepare", "file_id": 7, "channel_id": "sbt"})


class MediaTests(unittest.TestCase):
    def test_loopback_media_link_streams_one_authenticated_epoch(self):
        payload = bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xb2" * 184
        payload *= 8
        provider = fsm.FakeProvider([payload])
        manager = fsm.FavoritesSourceManager("secret", "m1")
        headers = "Authorization: Bearer secret\r\nX-Mount-Session: s1\r\nX-Manifest-Id: m1\r\n"
        grant = manager.handle_control(headers, {"operation": "prepare", "file_id": 7, "channel_id": "sbt"})
        server = fsm.FavoritesMediaServer(manager, provider, port=0)
        server.start()
        try:
            host, port = server.address
            client = socket.create_connection((host, port), timeout=2.0)
            request = grant
            line = (
                f"OPEN {request['request_generation']} {request['source_epoch']} {request['file_id']} "
                f"{len(request['session_id'])} {len(request['manifest_id'])} "
                f"{len(request['channel_id'])} {len('secret')}\n"
            ).encode()
            client.sendall(
                line
                + request["session_id"].encode()
                + request["manifest_id"].encode()
                + request["channel_id"].encode()
                + b"secret"
            )
            response = b""
            while not response.endswith(b"\n"):
                response += client.recv(1)
            self.assertEqual(response, b"OK 1 1\n")
            header = b""
            while len(header) < fsm.FRAME_HEADER.size:
                header += client.recv(fsm.FRAME_HEADER.size - len(header))
            fields = fsm.FRAME_HEADER.unpack(header)
            payload_len = fields[8]
            session_len, manifest_len, channel_len = fields[9:12]
            total = fsm.FRAME_HEADER.size + session_len + manifest_len + channel_len + payload_len + fsm.FRAME_TAG_LEN
            wire = header
            while len(wire) < total:
                wire += client.recv(total - len(wire))
            decoded_request, sequence, decoded_payload = fsm.decode_frame(wire, "secret")
            self.assertEqual(sequence, 1)
            self.assertEqual(decoded_payload, payload)
            self.assertEqual(decoded_request["file_id"], 7)
            client.close()
            self.assertEqual(provider.requests[0]["channel_id"], "sbt")
        finally:
            server.stop()

    def test_stale_media_link_is_rejected(self):
        provider = fsm.FakeProvider([])
        manager = fsm.FavoritesSourceManager("secret", "m1")
        headers = "Authorization: Bearer secret\r\nX-Mount-Session: s1\r\nX-Manifest-Id: m1\r\n"
        manager.handle_control(headers, {"operation": "prepare", "file_id": 7, "channel_id": "sbt"})
        server = fsm.FavoritesMediaServer(manager, provider, port=0)
        server.start()
        try:
            host, port = server.address
            client = socket.create_connection((host, port), timeout=2.0)
            line = b"OPEN 9 9 7 2 2 3 6\n"
            client.sendall(line + b"s1m1sbt" + b"secret")
            self.assertTrue(client.recv(64).startswith(b"ERR stale epoch"))
            client.close()
            self.assertEqual(provider.requests, [])
        finally:
            server.stop()

    def test_ffmpeg_profile_parameters(self):
        provider = fsm.FFmpegProvider(lambda channel: "http://private/live.ts", ffmpeg="ffmpeg")
        command = " ".join(provider.command("http://private/live.ts"))
        self.assertIn("bframes=0", command)
        self.assertIn("slices=1", command)
        self.assertIn("keyint=30", command)
        self.assertIn("-b:a 384k", command)
        self.assertIn("-ar 48000", command)
        self.assertIn("-ac 2", command)
        self.assertIn("-pcr_period 20", command)
        self.assertIn("-f mpegts", command)


if __name__ == "__main__":
    unittest.main()
