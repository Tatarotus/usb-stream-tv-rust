"""Google Drive VOD HTTP/control integration without external APIs or devices."""

import copy
import concurrent.futures
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import server
from vod_storage import StorageError
from vod_history import VodHistory


MEDIA = bytes(range(256)) * 512
TASK_ID = "a1b2c3d4"


class FakeDrive:
    enabled = True

    def __init__(self):
        self.reads = []
        self.uploads = []
        self.deletions = []
        self.failure = None
        self.snapshot = {
            "enabled": True, "available": True, "total_gb": 5000.0,
            "used_gb": 500.0, "free_gb": 4500.0,
            "updating": False, "error": None,
        }

    def quota(self):
        return dict(self.snapshot)

    def read(self, task, start, length):
        self.reads.append((task["id"], start, length))
        if self.failure:
            raise self.failure
        return MEDIA[start:start + length]

    def warm(self, task):
        if self.failure:
            raise self.failure

    def upload(self, path, task_id):
        self.uploads.append((path, task_id))
        if self.failure:
            raise self.failure
        return {
            "storage": "gdrive", "remote_path": task_id + ".mp4",
            "file_size": os.path.getsize(path), "remote_md5": "verified",
        }

    def delete(self, task):
        if self.failure:
            raise self.failure
        self.deletions.append(dict(task))


class FakePlaybackCache:
    def __init__(self, events):
        self.events = events
        self.reads = []
        self.failure = None
        self.stops = 0

    def start(self, task, seed_path=None):
        self.events.append(("start", task["id"]))
        if self.failure:
            raise self.failure
        return True

    def read(self, task, start, length):
        self.reads.append((task["id"], start, length))
        return MEDIA[start:start + length]

    def stop(self, *args, **kwargs):
        self.stops += 1


class QuietHandler(server.RequestHandler):
    def log_message(self, *args):
        pass


class TestGDriveServer(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vod-http-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = []
        self.drive = FakeDrive()
        self.cache = FakePlaybackCache(self.events)
        self.task = {
            "id": TASK_ID, "title": "Prepared film", "status": "ready",
            "storage": "gdrive", "remote_path": TASK_ID + ".mp4",
            "file_path": str(self.root / (TASK_ID + ".mp4")),
            "file_size": len(MEDIA), "remote_md5": "verified",
            "template_path": str(self.root / (TASK_ID + ".bin")),
            "display_name": "FILME.MP4",
        }
        self.tasks = {TASK_ID: self.task}
        self.hub = types.SimpleNamespace(tablet_vod_mode="")
        self.history = VodHistory(str(self.root / "watch_history.json"))
        replacements = {
            "GDRIVE_STORAGE": self.drive,
            "VOD_PLAYBACK_CACHE": self.cache,
            "VOD_TASKS": self.tasks,
            "VOD_DIR": str(self.root),
            "VOD_META_FILE": str(self.root / "vod_meta.json"),
            "ACTIVE_VOD_FILE": str(self.root / "active_vod.txt"),
            "ACTIVE_VOD_TASK": None,
            "VOD_SELECTION_GENERATION": 0,
            "VOD_SELECTION_TASK": None,
            "VOD_RUNNING_THREADS": set(),
            "VOD_RUNNING_PROCS": {},
            "LAST_VOD_STREAM_TIME": 0.0,
            "AUTH_PIN": "test-pin",
            "HUB": self.hub,
            "VOD_HISTORY": self.history,
        }
        for name, value in replacements.items():
            patcher = mock.patch.object(server, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.dispatch = mock.Mock(side_effect=lambda *args, **kwargs: self.events.append(("dispatch", args[0])))
        patcher = mock.patch.object(server, "dispatch_device_cmd", self.dispatch)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(server, "log_event")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.httpd = server.ThreadedHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_http)

    def _stop_http(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def request(self, method="GET", path=None, headers=None, payload=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        self.addCleanup(connection.close)
        fields = {"Connection": "close"}
        fields.update(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload)
            fields.setdefault("Content-Type", "application/json")
            fields.setdefault("X-Auth-PIN", "test-pin")
        connection.request(method, path or f"/vod/{TASK_ID}/movie.mp4", body=body, headers=fields)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()

    @contextlib.contextmanager
    def blocked_play(self):
        entered = threading.Event()
        release = threading.Event()

        def start(task, seed_path=None):
            self.events.append(("start", task["id"]))
            entered.set()
            if not release.wait(timeout=4):
                raise StorageError("Test warm was not released")
            return True

        self.cache.start = start
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(self.request, "POST", "/api/vod/play",
                                      payload={"task_id": TASK_ID})
            try:
                self.assertTrue(entered.wait(timeout=2), "Play never entered cache warm")
                yield release, pending
            finally:
                release.set()

    def test_remote_range_has_exact_body_and_metadata(self):
        status, headers, body = self.request(headers={"Range": "bytes=123-9999"})
        self.assertEqual(status, 206)
        self.assertEqual(body, MEDIA[123:10000])
        self.assertEqual(headers["Content-Length"], "9877")
        self.assertEqual(headers["Content-Range"], f"bytes 123-9999/{len(MEDIA)}")
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertFalse(Path(self.task["file_path"]).exists())

    def test_remote_suffix_and_open_ended_ranges(self):
        for range_value, expected, span in [
            ("bytes=-257", MEDIA[-257:], f"{len(MEDIA)-257}-{len(MEDIA)-1}"),
            ("bytes=130000-", MEDIA[130000:], f"130000-{len(MEDIA)-1}"),
        ]:
            with self.subTest(range=range_value):
                status, headers, body = self.request(headers={"Range": range_value})
                self.assertEqual(status, 206)
                self.assertEqual(body, expected)
                self.assertEqual(headers["Content-Range"], f"bytes {span}/{len(MEDIA)}")

    def test_invalid_ranges_return_416_without_reading_drive(self):
        for value in [f"bytes={len(MEDIA)}-", "bytes=99-1", "bytes=-0"]:
            with self.subTest(range=value):
                self.drive.reads.clear()
                status, headers, body = self.request(headers={"Range": value})
                self.assertEqual(status, 416)
                self.assertEqual(headers["Content-Range"], f"bytes */{len(MEDIA)}")
                self.assertFalse(self.drive.reads)

    def test_remote_full_response_and_cached_response(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Length"], str(len(MEDIA)))
        self.assertEqual(body, MEDIA)
        self.drive.reads.clear()
        self.cache.read = mock.Mock(side_effect=lambda task, start, length: MEDIA[start:start+length])
        status, _, body = self.request(headers={"Range": "bytes=9-19"})
        self.assertEqual(status, 206)
        self.assertEqual(body, MEDIA[9:20])
        self.assertFalse(self.drive.reads)

    def test_remote_read_failure_is_reported_before_success_headers(self):
        self.cache.read = mock.Mock(side_effect=StorageError("Google Drive unavailable"))
        status, headers, body = self.request(headers={"Range": "bytes=0-999"})
        self.assertEqual(status, 503)
        self.assertNotIn("Content-Range", headers)
        self.assertNotEqual(body, MEDIA[:1000])

    def test_uploading_video_cannot_be_streamed_as_ready(self):
        self.task["status"] = "uploading"
        status, _, _ = self.request(headers={"Range": "bytes=0-999"})
        self.assertEqual(status, 404)
        self.assertFalse(self.cache.reads)

    def test_remote_head_sends_headers_without_fetch_or_body(self):
        with socket.create_connection(("127.0.0.1", self.httpd.server_port), timeout=5) as connection:
            connection.sendall(f"HEAD /vod/{TASK_ID}/movie.mp4 HTTP/1.0\r\nHost: localhost\r\n\r\n".encode())
            output = bytearray()
            while True:
                block = connection.recv(4096)
                if not block:
                    break
                output.extend(block)
        headers, body = bytes(output).split(b"\r\n\r\n", 1)
        self.assertIn(b"200", headers.split(b"\r\n", 1)[0])
        self.assertIn(f"Content-Length: {len(MEDIA)}".encode(), headers)
        self.assertEqual(body, b"")
        self.assertFalse(self.drive.reads)
        self.assertFalse(self.cache.reads)

    def test_local_video_remains_readable_when_drive_is_enabled(self):
        self.task.pop("storage")
        Path(self.task["file_path"]).write_bytes(MEDIA)
        status, headers, body = self.request(headers={"Range": "bytes=10-99"})
        self.assertEqual(status, 206)
        self.assertEqual(body, MEDIA[10:100])
        self.assertEqual(headers["Content-Length"], "90")
        self.assertFalse(self.drive.reads)

    def test_status_includes_quota_and_remote_size_without_local_file(self):
        status, _, body = self.request(path="/api/vod/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["drive"], self.drive.snapshot)
        self.assertEqual(data["storage"], "gdrive")
        self.assertIn("VPS", data["disk"]["label"])
        self.assertEqual(data["tasks"][0]["file_size_bytes"], len(MEDIA))

    def test_history_checkpoint_requires_pin_and_is_exposed_in_status(self):
        self.task["title"] = "A Noiva de Istambul 172 - Star 528p"
        status, _, body = self.request("POST", "/api/vod/history/checkpoint",
                                       headers={"X-Auth-PIN": "wrong-pin"},
                                       payload={"task_id": TASK_ID})
        self.assertEqual(status, 401)
        self.assertFalse(json.loads(body)["success"])

        status, _, body = self.request("POST", "/api/vod/history/checkpoint",
                                       payload={"task_id": TASK_ID})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["success"])
        status, _, body = self.request(path="/api/vod/status")
        entry = json.loads(body)["continue_watching"][0]
        self.assertEqual(entry["task_id"], TASK_ID)
        self.assertEqual(entry["episode_number"], 172)

    def test_continue_resolves_next_episode_without_starting_it(self):
        self.task["title"] = "A Noiva de Istambul 172 - Star 528p"
        second = dict(self.task, id="b1c2d3e4", title="A Noiva de Istambul 173 - Star 528p",
                      remote_path="b1c2d3e4.mp4")
        self.tasks[second["id"]] = second
        status, _, body = self.request("POST", "/api/vod/history/completed", payload={"task_id": TASK_ID})
        self.assertEqual(status, 200)
        status, _, body = self.request("POST", "/api/vod/continue",
                                       payload={"series_key": "import:istanbullu-star"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["task_id"], second["id"])
        self.dispatch.assert_not_called()

    def test_play_records_selection_but_stream_reads_do_not_create_history(self):
        self.task["title"] = "A Noiva de Istambul 172 - Star 528p"
        status, _, _ = self.request(headers={"Range": "bytes=0-99"})
        self.assertEqual(status, 206)
        self.assertEqual(self.history.summary(self.tasks), [])
        status, _, body = self.request("POST", "/api/vod/play", payload={"task_id": TASK_ID})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["success"])
        self.assertEqual(self.history.summary(self.tasks)[0]["task_id"], TASK_ID)

    def test_unavailable_quota_is_not_reported_as_zero_free(self):
        self.drive.snapshot.update(available=False, total_gb=None, free_gb=None, used_gb=None,
                                   updating=True, error="Google Drive quota unavailable")
        _, _, body = self.request(path="/api/vod/status")
        quota = json.loads(body)["drive"]
        self.assertFalse(quota["available"])
        self.assertIsNone(quota["free_gb"])
        self.assertTrue(quota["updating"])

    def test_series_playlist_includes_remote_episodes(self):
        self.task["title"] = "Example S01E01"
        second = dict(self.task, id="b1c2d3e4", title="Example S01E02", remote_path="b1c2d3e4.mp4")
        self.tasks[second["id"]] = second
        self.assertEqual([t["id"] for t in server.get_series_playlist(TASK_ID)], [TASK_ID, second["id"]])

    def test_play_warms_current_video_before_dispatch(self):
        status, _, body = self.request("POST", "/api/vod/play", payload={"task_id": TASK_ID})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["success"])
        self.assertEqual([event[0] for event in self.events], ["start", "dispatch"])
        self.assertEqual(server.ACTIVE_VOD_TASK, TASK_ID)

    def test_warm_failure_preserves_active_state_and_does_not_dispatch(self):
        self.cache.failure = StorageError("Google Drive unavailable")
        server.ACTIVE_VOD_TASK = "old-live"
        self.hub.tablet_vod_mode = "old-live"
        status, _, body = self.request("POST", "/api/vod/play", payload={"task_id": TASK_ID})
        self.assertEqual(status, 503)
        self.assertFalse(json.loads(body)["success"])
        self.assertEqual(server.ACTIVE_VOD_TASK, "old-live")
        self.assertEqual(self.hub.tablet_vod_mode, "old-live")
        self.dispatch.assert_not_called()

    def test_disk_cache_unavailable_still_plays_after_remote_warm(self):
        self.cache.start = mock.Mock(return_value=False)
        status, _, body = self.request("POST", "/api/vod/play", payload={"task_id": TASK_ID})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["success"])
        self.dispatch.assert_called_once()

    def test_warm_only_loads_cache_without_selecting_or_dispatching(self):
        status, _, body = self.request("POST", "/api/vod/warm", payload={"task_id": TASK_ID})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertTrue(result["success"])
        self.assertTrue(result["warmed"])
        self.assertEqual(self.events, [("start", TASK_ID)])
        self.assertIsNone(server.ACTIVE_VOD_TASK)
        self.assertIsNone(server.VOD_SELECTION_TASK)
        self.assertEqual(server.VOD_SELECTION_GENERATION, 0)
        self.assertEqual(self.hub.tablet_vod_mode, "")
        self.dispatch.assert_not_called()

    def test_warm_requires_correct_pin_before_loading_cache(self):
        status, _, body = self.request("POST", "/api/vod/warm",
                                       headers={"X-Auth-PIN": "wrong-pin"},
                                       payload={"task_id": TASK_ID})
        self.assertEqual(status, 401)
        self.assertFalse(json.loads(body)["success"])
        self.assertFalse(self.events)
        self.dispatch.assert_not_called()

    def test_warm_cannot_interrupt_another_active_or_pending_selection(self):
        self.cache.start = mock.Mock()
        for active, pending in [("b1c2d3e4", None), (None, "b1c2d3e4")]:
            with self.subTest(active=active, pending=pending):
                server.ACTIVE_VOD_TASK = active
                server.VOD_SELECTION_TASK = pending
                status, _, _ = self.request("POST", "/api/vod/warm", payload={"task_id": TASK_ID})
                self.assertEqual(status, 409)
                self.assertEqual(server.ACTIVE_VOD_TASK, active)
                self.assertEqual(server.VOD_SELECTION_TASK, pending)
                self.cache.start.assert_not_called()
                self.dispatch.assert_not_called()

    def test_return_to_live_cancels_a_play_still_warming(self):
        with self.blocked_play() as (release, pending):
            status, _, body = self.request("POST", "/api/vod/live", payload={})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["success"])
            release.set()
            play_status, _, _ = pending.result(timeout=3)
        self.assertEqual(play_status, 409)
        self.assertIsNone(server.ACTIVE_VOD_TASK)
        self.assertEqual(self.hub.tablet_vod_mode, "")
        self.assertFalse(any("switch_vod" in call.args[0] for call in self.dispatch.call_args_list))

    def test_delete_cancels_a_play_still_warming(self):
        with self.blocked_play() as (release, pending):
            status, _, body = self.request("POST", "/api/vod/delete", payload={"task_id": TASK_ID})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["success"])
            release.set()
            play_status, _, _ = pending.result(timeout=3)
        self.assertEqual(play_status, 409)
        self.assertNotIn(TASK_ID, self.tasks)
        self.assertIsNone(server.ACTIVE_VOD_TASK)
        self.assertFalse(any("switch_vod" in call.args[0] for call in self.dispatch.call_args_list))

    def test_deleted_upload_cannot_be_reprepared_until_old_worker_finishes(self):
        url = "https://example.invalid/movie/prepared.mp4"
        task_id = hashlib.md5(url.encode()).hexdigest()[:8]
        old_task = dict(self.task, id=task_id, status="uploading", storage="local", url=url)
        self.tasks.clear()
        self.tasks[task_id] = old_task
        server.VOD_RUNNING_THREADS.add(task_id)
        status, _, body = self.request("POST", "/api/vod/delete", payload={"task_id": task_id})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["success"])
        self.assertNotIn(task_id, self.tasks)
        with mock.patch.object(server, "_prepare_vod_thread") as prepare:
            status, _, body = self.request("POST", "/api/vod/prepare",
                                           payload={"url": url, "title": "Prepared film"})
            self.assertEqual(status, 409)
            self.assertFalse(json.loads(body)["success"])
            prepare.assert_not_called()
        self.assertNotIn(task_id, self.tasks)
        self.assertIn(task_id, server.VOD_RUNNING_THREADS)

    def test_remote_delete_failure_retains_catalog(self):
        self.drive.failure = StorageError("Google Drive delete failed")
        status, _, body = self.request("POST", "/api/vod/delete", payload={"task_id": TASK_ID})
        self.assertEqual(status, 502)
        self.assertFalse(json.loads(body)["success"])
        self.assertIn(TASK_ID, self.tasks)

    def test_remote_delete_removes_only_selected_catalog_entry(self):
        self.tasks["b1c2d3e4"] = dict(self.task, id="b1c2d3e4", remote_path="b1c2d3e4.mp4")
        status, _, body = self.request("POST", "/api/vod/delete", payload={"task_id": TASK_ID})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["success"])
        self.assertNotIn(TASK_ID, self.tasks)
        self.assertIn("b1c2d3e4", self.tasks)
        self.assertEqual([task["id"] for task in self.drive.deletions], [TASK_ID])

    def test_invalid_task_ids_are_rejected_before_storage_or_dispatch(self):
        for endpoint in ("play", "delete"):
            for invalid_id in ("../victim", "a1b2c3d4;echo x", "", [TASK_ID]):
                with self.subTest(endpoint=endpoint, task_id=invalid_id):
                    status, _, _ = self.request("POST", "/api/vod/" + endpoint,
                                                payload={"task_id": invalid_id})
                    self.assertEqual(status, 400)
        self.assertFalse(self.drive.deletions)
        self.dispatch.assert_not_called()


class TestPreparedUpload(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vod-finalize-test-")
        self.addCleanup(self.temp.cleanup)
        self.local = Path(self.temp.name) / (TASK_ID + ".mp4")
        self.local.write_bytes(MEDIA)
        self.task = {"id": TASK_ID, "status": "processing", "file_path": str(self.local),
                     "file_size": len(MEDIA), "title": "Prepared film"}
        self.tasks = {TASK_ID: self.task}
        self.drive = FakeDrive()
        self.cache = FakePlaybackCache([])
        for name, value in {
            "GDRIVE_STORAGE": self.drive, "VOD_PLAYBACK_CACHE": self.cache,
            "VOD_TASKS": self.tasks,
            "VOD_DIR": self.temp.name,
            "VOD_META_FILE": str(Path(self.temp.name) / "meta.json"),
        }.items():
            patcher = mock.patch.object(server, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_verified_upload_persists_remote_metadata_before_unlink(self):
        persisted = []

        def save(**kwargs):
            persisted.append((copy.deepcopy(self.tasks), self.local.exists()))

        with mock.patch.object(server, "save_vod_tasks", side_effect=save):
            published = server.upload_prepared_vod(TASK_ID, self.task)
        self.assertTrue(published)
        self.assertEqual(self.task["storage"], "gdrive")
        self.assertEqual(self.task["remote_path"], TASK_ID + ".mp4")
        remote_saves = [(tasks, exists) for tasks, exists in persisted
                        if tasks.get(TASK_ID, {}).get("storage") == "gdrive"]
        self.assertTrue(remote_saves, "No remote metadata commit before local deletion")
        self.assertTrue(remote_saves[0][1], "Local source removed before remote metadata was persisted")
        self.assertFalse(self.local.exists())

    def test_upload_error_preserves_source_for_retry(self):
        self.drive.failure = StorageError("Google Drive upload failed")
        try:
            published = server.upload_prepared_vod(TASK_ID, self.task)
        except StorageError:
            published = False
        self.assertFalse(published)
        self.assertTrue(self.local.exists())
        self.assertNotEqual(self.task.get("storage"), "gdrive")
        self.assertNotEqual(self.task.get("status"), "ready")

    def test_catalog_commit_failure_keeps_source_and_retry_state(self):
        def fail_remote_commit(**kwargs):
            if self.task.get("storage") == "gdrive":
                raise OSError("catalog cannot be written")

        with mock.patch.object(server, "save_vod_tasks", side_effect=fail_remote_commit):
            with self.assertRaises(StorageError):
                server.upload_prepared_vod(TASK_ID, self.task)
        self.assertTrue(self.local.exists())
        self.assertTrue(self.task["upload_pending"])
        self.assertEqual(self.task["status"], "error")
        self.assertNotEqual(self.task.get("storage"), "gdrive")

    def test_cancelled_task_is_not_republished_after_upload(self):
        upload = self.drive.upload

        def cancel_after_upload(path, task_id):
            metadata = upload(path, task_id)
            del self.tasks[task_id]
            return metadata

        self.drive.upload = cancel_after_upload
        published = server.upload_prepared_vod(TASK_ID, self.task)
        self.assertFalse(published)
        self.assertNotIn(TASK_ID, self.tasks)
        self.assertEqual([task["id"] for task in self.drive.deletions], [TASK_ID])


class TestGDriveMigrate(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vod-migrate-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.drive = FakeDrive()
        self.cache = FakePlaybackCache([])
        self.local = self.root / (TASK_ID + ".mp4")
        self.local.write_bytes(MEDIA)
        self.task = {
            "id": TASK_ID, "title": "Local movie", "status": "ready",
            "storage": "local", "file_path": str(self.local),
            "file_size": len(MEDIA), "template_path": str(self.root / (TASK_ID + ".bin")),
            "display_name": "FILME.MP4",
        }
        self.tasks = {TASK_ID: self.task}
        replacements = {
            "GDRIVE_STORAGE": self.drive,
            "VOD_PLAYBACK_CACHE": self.cache,
            "VOD_TASKS": self.tasks,
            "VOD_DIR": str(self.root),
            "VOD_META_FILE": str(self.root / "vod_meta.json"),
            "ACTIVE_VOD_TASK": None,
            "VOD_RUNNING_THREADS": set(),
            "VOD_RUNNING_PROCS": {},
            "AUTH_PIN": "secret-pin",
        }
        for name, value in replacements.items():
            patcher = mock.patch.object(server, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.httpd = server.ThreadedHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_http)

    def _stop_http(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def request(self, payload=None, pin="secret-pin"):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        self.addCleanup(conn.close)
        headers = {"Content-Type": "application/json", "Connection": "close"}
        if pin:
            headers["X-Auth-PIN"] = pin
        body = json.dumps(payload or {})
        conn.request("POST", "/api/vod/migrate", body=body, headers=headers)
        res = conn.getresponse()
        data = res.read()
        try:
            parsed = json.loads(data)
        except Exception:
            parsed = {}
        return res.status, parsed

    def test_migrate_auth_failure(self):
        status, data = self.request(payload={"task_id": TASK_ID}, pin="wrong")
        self.assertEqual(status, 401)
        self.assertFalse(data.get("success"))

    def test_migrate_invalid_task_id(self):
        for bad_id in ["", "not-hex", "1234567", "123456789", "../etc"]:
            with self.subTest(bad_id=bad_id):
                status, data = self.request(payload={"task_id": bad_id})
                self.assertEqual(status, 400)
                self.assertFalse(data.get("success"))

    def test_migrate_task_not_found(self):
        status, data = self.request(payload={"task_id": "11223344"})
        self.assertEqual(status, 404)
        self.assertFalse(data.get("success"))

    def test_migrate_idempotent_already_gdrive(self):
        self.task["storage"] = "gdrive"
        self.task["status"] = "ready"
        status, data = self.request(payload={"task_id": TASK_ID})
        self.assertEqual(status, 200)
        self.assertTrue(data.get("success"))
        self.assertFalse(self.drive.uploads)

    def test_migrate_refuses_active_task_on_tv(self):
        with mock.patch.object(server, "ACTIVE_VOD_TASK", TASK_ID):
            status, data = self.request(payload={"task_id": TASK_ID})
            self.assertEqual(status, 409)
            self.assertFalse(data.get("success"))

    def test_migrate_refuses_warming_cache_task(self):
        cache_state = {"key": (TASK_ID, "verified", len(MEDIA))}
        with mock.patch.object(self.cache, "_state", cache_state, create=True):
            status, data = self.request(payload={"task_id": TASK_ID})
            self.assertEqual(status, 409)
            self.assertFalse(data.get("success"))

    def test_migrate_refuses_deleting_or_processing_task(self):
        for bad_status in ["deleting", "processing", "queued"]:
            with self.subTest(status=bad_status):
                self.task["status"] = bad_status
                status, data = self.request(payload={"task_id": TASK_ID})
                self.assertEqual(status, 409)
                self.assertFalse(data.get("success"))

    def test_migrate_missing_local_file(self):
        self.local.unlink()
        status, data = self.request(payload={"task_id": TASK_ID})
        self.assertEqual(status, 400)
        self.assertFalse(data.get("success"))

    def test_migrate_success_uploads_and_unlinks(self):
        status, data = self.request(payload={"task_id": TASK_ID})
        self.assertEqual(status, 202)
        self.assertTrue(data.get("success"))

        # Wait for worker thread to complete
        for _ in range(50):
            if not server.VOD_RUNNING_THREADS:
                break
            time.sleep(0.05)

        self.assertEqual(self.task["status"], "ready")
        self.assertEqual(self.task["storage"], "gdrive")
        self.assertEqual(self.task["remote_path"], TASK_ID + ".mp4")
        self.assertFalse(self.local.exists(), "Source file was not unlinked after successful upload")
        # Ensure seed_cache=False was respected (events should not contain start)
        self.assertNotIn(("start", TASK_ID), self.cache.events)


if __name__ == "__main__":
    unittest.main()
