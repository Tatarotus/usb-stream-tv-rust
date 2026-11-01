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
                              ("-b:a", "384k"), ("-pcr_period", "19"), ("-pix_fmt", "yuv420p"),
                              ("-threads:v", "1"), ("-muxrate", "6000000"), ("-muxdelay", "0"),
                              ("-bufsize", "500k"), ("-maxrate", "4000k"), ("-b:v", "3500k"),
                              ("-level:v", "4.0")):
            self.assertEqual(command[command.index(option) + 1], value)
        scale = command[command.index("-vf") + 1]
        self.assertIn("1920:1080", scale)
        x264 = command[command.index("-x264-params") + 1]
        self.assertIn("slices=1", x264)
        self.assertIn("bframes=0", x264)
        self.assertIn("sliced-threads=0", x264)
        legacy = bridge.ffmpeg_command("https://iptv.example/live/test.ts", profile_name="720p30")
        self.assertIn("1280:720", legacy[legacy.index("-vf") + 1])
        self.assertEqual(legacy[legacy.index("-level:v") + 1], "3.1")
        self.assertEqual(legacy[legacy.index("-pcr_period") + 1], "20")

    def test_export_config_allowlist_uses_reviewed_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = manifest()
            data["entries"][0].update(namespace="live", channel_id="207198",
                                      folder=["Abertos"], filename="SBT.trp")
            identifier = bridge.file_id_for("207198")
            data["entries"][0].update(file_id=identifier,
                                      bootstrap_id=f"bootstrap-{identifier:08x}")
            entries = [data["entries"][0]]
            for channel, filename in (("214640", "Record.trp"), ("207177", "Band.trp")):
                identifier = bridge.file_id_for(channel)
                entries.append(dict(entries[0], file_id=identifier, channel_id=channel,
                                    filename=filename,
                                    bootstrap_id=f"bootstrap-{identifier:08x}"))
            data["entries"] = entries
            data["manifest_id"] = bridge.manifest_digest(data)
            (root / "manifest.json").write_text(json.dumps(data))
            export = {"schema_version": 1, "generation": 2, "output_profile": "1080p30",
                      "identities": [
                          {"identity": "sbt", "category": "Abertos", "filename": "SBT.trp",
                           "status": "approved", "chosen": {"channel_id": "207198"},
                           "favorite_ids": ["207198"], "candidates_tested": [{"channel_id": "207198"}]},
                          {"identity": "record", "category": "Abertos", "filename": "Record.trp",
                           "status": "approved", "chosen": {"channel_id": "214640"},
                           "favorite_ids": ["214640"], "candidates_tested": [{"channel_id": "214640"}]},
                          {"identity": "band", "category": "Abertos", "filename": "Band.trp",
                           "status": "approved", "chosen": {"channel_id": "207177"},
                           "favorite_ids": ["207171"], "candidates_tested": [{"channel_id": "207177"}]},
                      ]}
            (root / "export.json").write_text(json.dumps(export))
            rows = [{"stream_id": 207198, "stream_type": "live", "name": "SBT RJ FHD¹"},
                    {"stream_id": 214640, "stream_type": "live", "name": "RECORD TV RIO FHD²"},
                    {"stream_id": 207177, "stream_type": "live", "name": "BAND RJ FHD²"}]
            (root / "live.json").write_text(json.dumps(rows))
            # Band's chosen candidate is not in the personal favorites list.
            (root / "favorites.json").write_text(json.dumps(["207198", "214640", "207171"]))
            with patch.dict(os.environ, {"XTREAM_UPSTREAM": "https://iptv.example",
                                        "XTREAM_USER": "example", "XTREAM_PASS": "secret"}):
                manifest_data, sources = bridge.read_config(
                    root / "manifest.json", live_cache_path=root / "live.json",
                    export_config_path=root / "export.json")
            self.assertEqual(len(manifest_data["entries"]), 3)
            self.assertEqual(len(sources), 3)
            self.assertIn((bridge.file_id_for("207177"), "207177"), sources)

            export["identities"][2]["status"] = "pending"
            (root / "export.json").write_text(json.dumps(export))
            with patch.dict(os.environ, {"XTREAM_UPSTREAM": "https://iptv.example",
                                        "XTREAM_USER": "example", "XTREAM_PASS": "secret"}):
                with self.assertRaisesRegex(ValueError, "manifest does not match"):
                    bridge.read_config(root / "manifest.json", live_cache_path=root / "live.json",
                                       export_config_path=root / "export.json")
            export["identities"][2]["status"] = "approved"
            export["identities"][2]["chosen"]["channel_id"] = "999999"
            (root / "export.json").write_text(json.dumps(export))
            with patch.dict(os.environ, {"XTREAM_UPSTREAM": "https://iptv.example",
                                        "XTREAM_USER": "example", "XTREAM_PASS": "secret"}):
                with self.assertRaisesRegex(ValueError, "missing from Live catalog"):
                    bridge.read_config(root / "manifest.json", live_cache_path=root / "live.json",
                                       export_config_path=root / "export.json")


    def test_tv_root_nests_live_channels_and_stays_compatible(self):
        """`live_root` puts channels in TV/<categoria>/ without breaking the old shape."""
        def build(root, live_root):
            data = manifest()
            entries = []
            for channel, category, filename in (("207198", "Abertos", "SBT 1080 RJ.trp"),
                                                ("365243", "Filmes", "Telecine Action 1080.trp")):
                identifier = bridge.file_id_for(channel)
                folder = [live_root, category] if live_root else [category]
                entries.append({"file_id": identifier, "namespace": "live",
                                "channel_id": channel, "kind": "live", "folder": folder,
                                "filename": filename, "logical_size": 4_000_000_000,
                                "bootstrap_id": f"bootstrap-{identifier:08x}"})
            data["entries"] = entries
            data["manifest_id"] = bridge.manifest_digest(data)
            (root / "manifest.json").write_text(json.dumps(data))
            export = {"schema_version": 1, "generation": 2, "output_profile": "1080p30",
                      "identities": [
                          {"identity": "sbt", "category": "Abertos",
                           "filename": "SBT 1080 RJ.trp", "status": "approved",
                           "chosen": {"channel_id": "207198"}, "favorite_ids": ["207198"]},
                          {"identity": "telecine-action", "category": "Filmes",
                           "filename": "Telecine Action 1080.trp", "status": "approved",
                           "chosen": {"channel_id": "365243"}, "favorite_ids": ["365244"]}]}
            if live_root:
                export["live_root"] = live_root
            (root / "export.json").write_text(json.dumps(export))
            (root / "live.json").write_text(json.dumps([
                {"stream_id": 207198, "stream_type": "live", "name": "SBT RJ FHD1"},
                {"stream_id": 365243, "stream_type": "live", "name": "TELECINE ACTION H265"}]))
            (root / "favorites.json").write_text(json.dumps(["207198", "365244"]))
            with patch.dict(os.environ, {"XTREAM_UPSTREAM": "https://iptv.example",
                                        "XTREAM_USER": "example", "XTREAM_PASS": "secret"}):
                return bridge.read_config(root / "manifest.json",
                                          live_cache_path=root / "live.json",
                                          export_config_path=root / "export.json")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, sources = build(root, "TV")
            self.assertEqual(len(sources), 2)
            folders = sorted(tuple(e["folder"]) for e in data["entries"])
            self.assertEqual(folders, [("TV", "Abertos"), ("TV", "Filmes")])

        # A generation published before the TV/ layout keeps working unchanged.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, sources = build(root, "")
            self.assertEqual(len(sources), 2)
            self.assertEqual(sorted(tuple(e["folder"]) for e in data["entries"]),
                             [("Abertos",), ("Filmes",)])

    def test_a_deeper_nesting_than_a_category_is_rejected(self):
        """A path the operator never asked for must not be accepted silently."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = manifest()
            identifier = bridge.file_id_for("207198")
            data["entries"] = [{"file_id": identifier, "namespace": "live",
                                "channel_id": "207198", "kind": "live",
                                "folder": ["TV", "Abertos", "extra"],
                                "filename": "SBT.trp", "logical_size": 4_000_000_000,
                                "bootstrap_id": f"bootstrap-{identifier:08x}"}]
            data["manifest_id"] = bridge.manifest_digest(data)
            (root / "manifest.json").write_text(json.dumps(data))
            export = {"schema_version": 1, "generation": 2, "output_profile": "1080p30",
                      "live_root": "TV",
                      "identities": [{"identity": "sbt", "category": "Abertos",
                                      "filename": "SBT.trp", "status": "approved",
                                      "chosen": {"channel_id": "207198"},
                                      "favorite_ids": ["207198"]}]}
            (root / "export.json").write_text(json.dumps(export))
            (root / "live.json").write_text(json.dumps(
                [{"stream_id": 207198, "stream_type": "live", "name": "SBT RJ FHD1"}]))
            (root / "favorites.json").write_text(json.dumps(["207198"]))
            with patch.dict(os.environ, {"XTREAM_UPSTREAM": "https://iptv.example",
                                        "XTREAM_USER": "example", "XTREAM_PASS": "secret"}):
                with self.assertRaisesRegex(ValueError, "not a category path"):
                    bridge.read_config(root / "manifest.json",
                                       live_cache_path=root / "live.json",
                                       export_config_path=root / "export.json")


    def test_manager_derives_the_expected_paths_from_the_builder(self):
        """The manager must not re-implement the builder's path rules."""
        config = {"schema_version": 1, "generation": 1, "live_root": " TV ",
                  "identities": [
                      {"identity": "sbt", "status": "approved", "category": " Abertos ",
                       "filename": "SBT 1080 RJ", "chosen": {"channel_id": "207198"}}]}
        expected = bridge._expected_live_entries(config)
        self.assertEqual(expected, {(("TV", "Abertos"), "SBT 1080 RJ.trp", "207198")})

    def test_duplicate_live_entries_in_a_manifest_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Two DIFFERENT channels sharing one path: the manifest reader
            # cannot catch this by file_id, so the path check must.
            first = bridge.file_id_for("207198")
            second = bridge.file_id_for("207199")
            entry = {"file_id": first, "namespace": "live", "channel_id": "207198",
                     "kind": "live", "folder": ["TV", "Abertos"],
                     "filename": "SBT 1080 RJ.trp", "logical_size": 4_000_000_000,
                     "bootstrap_id": f"bootstrap-{first:08x}"}
            twin = dict(entry, file_id=second, channel_id="207199",
                        bootstrap_id=f"bootstrap-{second:08x}")
            data = {"schema_version": 1, "layout_version": 1, "generation": 1,
                    "entries": [entry, twin]}
            data["manifest_id"] = bridge.manifest_digest(data)
            (root / "manifest.json").write_text(json.dumps(data))
            export = {"schema_version": 1, "generation": 1, "output_profile": "1080p30",
                      "live_root": "TV",
                      "identities": [
                          {"identity": "sbt", "status": "approved",
                           "category": "Abertos", "filename": "SBT 1080 RJ.trp",
                           "chosen": {"channel_id": "207198"},
                           "favorite_ids": ["207198"]},
                          {"identity": "sbt-twin", "status": "approved",
                           "category": "Abertos", "filename": "SBT 1080 RJ.trp",
                           "chosen": {"channel_id": "207199"},
                           "favorite_ids": ["207199"]}]}
            (root / "export.json").write_text(json.dumps(export))
            (root / "live.json").write_text(json.dumps(
                [{"stream_id": 207198, "stream_type": "live", "name": "SBT RJ FHD1"},
                 {"stream_id": 207199, "stream_type": "live", "name": "SBT RJ FHD2"}]))
            (root / "favorites.json").write_text(json.dumps(["207198", "207199"]))
            with patch.dict(os.environ, {"XTREAM_UPSTREAM": "https://iptv.example",
                                        "XTREAM_USER": "example", "XTREAM_PASS": "secret"}):
                # Either layer may speak first: the builder refuses the export
                # itself, the manager refuses a duplicated manifest entry.
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    bridge.read_config(root / "manifest.json",
                                       live_cache_path=root / "live.json",
                                       export_config_path=root / "export.json")


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
