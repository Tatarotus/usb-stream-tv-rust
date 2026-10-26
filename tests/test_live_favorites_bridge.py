"""Operational tests for OPEN-driven sources, without network IPTV or ADB."""

import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import run_live_favorites_manager as bridge
from favorites_source_manager import FRAME_HEADER, FRAME_TAG_LEN, decode_frame


TOKEN = "physical-test-token-with-at-least-32-bytes"


def manifest():
    result = {"schema_version": 1, "layout_version": 1, "generation": 1,
              "entries": [{"file_id": 66, "namespace": "catalog", "channel_id": "sbt-rio",
                           "kind": "live", "folder": ["Favoritos", "Abertos"],
                           "filename": "SBT.trp", "logical_size": 4_000_000_000,
                           "bootstrap_id": "bootstrap-00000042"}]}
    result["manifest_id"] = bridge.manifest_digest(result)
    return result


class BridgeConfigTests(unittest.TestCase):
    def test_live_cache_requires_saved_live_ids_and_resolves_exact_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = manifest()
            data["entries"][0]["namespace"] = "live"
            data["entries"][0]["channel_id"] = "207198"
            second = dict(data["entries"][0], file_id=67, channel_id="214640",
                          filename="Record.trp", bootstrap_id="bootstrap-00000043")
            data["entries"].append(second)
            data["manifest_id"] = bridge.manifest_digest(data)
            (root / "manifest.json").write_text(json.dumps(data))
            rows = [{"stream_id": 207198, "stream_type": "live", "name": "SBT RJ FHD¹"},
                    {"stream_id": 214640, "stream_type": "live", "name": "RECORD TV RIO FHD²"},
                    {"stream_id": 888, "stream_type": "movie", "name": "Not a Live channel"}]
            (root / "live.json").write_text(json.dumps(rows))
            (root / "favorites.json").write_text(json.dumps(["207198", "214640", "888"]))
            with patch.dict(os.environ, {"XTREAM_UPSTREAM": "https://iptv.example",
                                        "XTREAM_USER": "example", "XTREAM_PASS": "secret"}):
                _, sources = bridge.read_config(root / "manifest.json", live_cache_path=root / "live.json",
                                                 favorites_path=root / "favorites.json")
                self.assertEqual(sources[(66, "207198")], "https://iptv.example/live/example/secret/207198.ts")
                self.assertEqual(sources[(67, "214640")], "https://iptv.example/live/example/secret/214640.ts")
                (root / "favorites.json").write_text(json.dumps(["207198"]))
                with self.assertRaisesRegex(ValueError, "missing from catalog"):
                    bridge.read_config(root / "manifest.json", live_cache_path=root / "live.json",
                                       favorites_path=root / "favorites.json")
                (root / "favorites.json").write_text(json.dumps(["207198", "214640"]))
                rows[1]["stream_type"] = "movie"
                (root / "live.json").write_text(json.dumps(rows))
                with self.assertRaisesRegex(ValueError, "missing from catalog"):
                    bridge.read_config(root / "manifest.json", live_cache_path=root / "live.json",
                                       favorites_path=root / "favorites.json")

    def test_digest_binds_channel_and_rejects_catalog_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = manifest()
            (root / "manifest.json").write_text(json.dumps(data))
            (root / "channels.json").write_text(json.dumps({"sbt-rio": {
                "id": "sbt-rio", "url": "{XTREAM_UPSTREAM}/live/{XTREAM_USER}/{XTREAM_PASS}/207198.ts",
            }}))
            with patch.dict(os.environ, {"XTREAM_UPSTREAM": "https://iptv.example",
                                        "XTREAM_USER": "example", "XTREAM_PASS": "secret"}):
                _, sources = bridge.read_config(root / "manifest.json", root / "channels.json")
            self.assertEqual(sources[(66, "sbt-rio")], "https://iptv.example/live/example/secret/207198.ts")
            data["entries"][0]["channel_id"] = "record-rio"
            (root / "manifest.json").write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                bridge.read_config(root / "manifest.json", root / "channels.json")

    def test_forbids_vod_and_non_loopback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(json.dumps(manifest()))
            (root / "channels.json").write_text(json.dumps({"sbt-rio": {
                "url": "https://iptv.example/movie/user/password/123.ts",
            }}))
            with self.assertRaisesRegex(ValueError, "VOD source forbidden"):
                bridge.read_config(root / "manifest.json", root / "channels.json")
        with self.assertRaisesRegex(ValueError, "loopback"):
            bridge.LiveBridge(("0.0.0.0", 0), token=TOKEN, session="test", manifest=manifest(), sources={})

    def test_samsung_ffmpeg_contract(self):
        command = bridge.ffmpeg_command("https://iptv.example/live/test.ts")
        for option, value in (("-g", "30"), ("-keyint_min", "30"), ("-sc_threshold", "0"),
                              ("-c:a", "ac3"), ("-ar", "48000"), ("-ac", "2"),
                              ("-b:a", "384k"), ("-pcr_period", "20"), ("-pix_fmt", "yuv420p"),
                              ("-threads:v", "1"), ("-muxrate", "6000000"), ("-muxdelay", "0"),
                              ("-bufsize", "100k")):
            self.assertEqual(command[command.index(option) + 1], value)
        x264 = command[command.index("-x264-params") + 1]
        self.assertIn("slices=1", x264)
        self.assertIn("bframes=0", x264)
        self.assertIn("sliced-threads=0", x264)


class BridgeSocketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        # Stand-in emits real packet framing immediately, then stays alive so
        # disconnect/cancellation can be observed independently of source EOF.
        executable = root / "fake-ffmpeg"
        executable.write_text("#!/usr/bin/env python3\nimport os,time\nos.write(1,bytes([0x47,0x01,0,0x10])+bytes([255])*184)\ntime.sleep(30)\n")
        executable.chmod(0o700)
        self.executable = executable
        self.manifest = manifest()
        self.server = bridge.LiveBridge(("127.0.0.1", 0), token=TOKEN, session="mount-test",
                                       manifest=self.manifest, sources={(66, "sbt-rio"): "https://iptv.example/live/1.ts"},
                                       ffmpeg=str(executable))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def connect(self, generation=1, epoch=1, channel="sbt-rio", token=TOKEN):
        client = socket.create_connection(self.server.server_address, timeout=2)
        self.clients.append(client)
        identities = [b"mount-test", self.manifest["manifest_id"].encode(), channel.encode(), token.encode()]
        line = f"OPEN {generation} {epoch} 66 {' '.join(str(len(v)) for v in identities)}\n".encode()
        client.sendall(line + b"".join(identities))
        result = bytearray()
        while not result.endswith(b"\n"):
            result.extend(client.recv(1))
        return client, bytes(result)

    def recv_exact(self, client, count):
        data = bytearray()
        while len(data) < count:
            chunk = client.recv(count - len(data))
            self.assertTrue(chunk)
            data.extend(chunk)
        return bytes(data)

    def test_open_needs_no_missing_prepare_and_binds_frame_identity(self):
        client, response = self.connect()
        self.assertEqual(response, b"OK 1 1\n")
        header = self.recv_exact(client, FRAME_HEADER.size)
        fields = FRAME_HEADER.unpack(header)
        remaining = fields[8] + sum(fields[9:]) + FRAME_TAG_LEN
        identity, sequence, payload = decode_frame(header + self.recv_exact(client, remaining), TOKEN)
        self.assertEqual(identity["channel_id"], "sbt-rio")
        self.assertEqual(identity["source_epoch"], 1)
        self.assertEqual(sequence, 1)
        self.assertEqual(len(payload), 188)
        process = self.server._active[1]
        client.close()
        deadline = time.monotonic() + 2
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(process.poll(), "tablet disconnect must terminate stalled ffmpeg")

    def test_rejects_wrong_token_channel_and_replay(self):
        for kwargs in ({"token": "x" * 40}, {"channel": "record-rio"}):
            client, response = self.connect(**kwargs)
            self.assertTrue(response.startswith(b"ERR"))
            client.close()
            self.assertIsNone(self.server._active)
        client, response = self.connect()
        self.assertEqual(response, b"OK 1 1\n")
        process = self.server._active[1]
        # Epoch2 retires the previous process before publishing the new link.
        newer, response = self.connect(generation=2, epoch=2)
        self.assertEqual(response, b"OK 2 2\n")
        self.assertIsNotNone(process.poll())
        client.close()
        # Let the old handler release its bounded connection slot.
        time.sleep(0.1)
        replay, response = self.connect(generation=1, epoch=1)
        self.assertTrue(response.startswith(b"ERR"))

    def test_early_ffmpeg_error_is_logged_without_credentials(self):
        secret_url = "https://provider.example/live/private-user/private-password/207198.ts"
        self.executable.write_text(
            "#!/usr/bin/env python3\nimport sys\n"
            "sys.stderr.write('HTTP error 403 Forbidden " + secret_url + "\\n')\n"
            "sys.stderr.write('" + TOKEN + "\\n')\nsys.exit(1)\n"
        )
        with self.assertLogs(bridge.LOG, level="INFO") as captured:
            client, response = self.connect()
            self.assertEqual(response, b"OK 1 1\n")
            deadline = time.monotonic() + 2
            while not any("source_closed" in line for line in captured.output) and time.monotonic() < deadline:
                time.sleep(0.01)
        logs = "\n".join(captured.output)
        self.assertIn("http_403", logs)
        self.assertIn('"frames": 0', logs)
        self.assertNotIn(secret_url, logs)
        self.assertNotIn("private-password", logs)
        self.assertNotIn(TOKEN, logs)


if __name__ == "__main__":
    unittest.main()
