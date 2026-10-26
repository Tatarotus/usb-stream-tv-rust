"""Cloud VOD verification, real HTTP range checks, and bounded cache behavior."""

import hashlib
import http.server
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from vod_storage import GDriveStorage, StorageError


def task(size=48, ident="a1b2c3d4"):
    return {"id": ident, "storage": "gdrive", "remote_path": ident + ".mp4",
            "file_size": size, "remote_md5": "abc"}


class StorageTests(unittest.TestCase):
    def storage(self, **kwargs):
        storage = GDriveStorage("private:TV/VOD", **kwargs)
        self.addCleanup(storage.close)
        return storage

    def test_constructor_and_disabled_quota_are_inert(self):
        with mock.patch("subprocess.Popen", side_effect=AssertionError("process")), \
                mock.patch("subprocess.run", side_effect=AssertionError("process")), \
                mock.patch("threading.Thread.start", side_effect=AssertionError("thread")):
            storage = GDriveStorage()
            self.assertFalse(storage.enabled)
            self.assertEqual(storage.quota()["enabled"], False)
            enabled = GDriveStorage("drive:movies")
            self.assertTrue(enabled.enabled)
            storage.close()
            enabled.close()

    def test_upload_verifies_hash_and_size_without_deleting_local(self):
        storage = self.storage()
        content = b"complete prepared MP4"
        md5 = hashlib.md5(content).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "movie.mp4")
            with open(path, "wb") as output:
                output.write(content)
            with mock.patch.object(storage, "_run", side_effect=["", json.dumps({
                    "Size": len(content), "Hashes": {"MD5": md5}})]) as run:
                result = storage.upload(path, "a1b2c3d4")
                self.assertEqual(result["remote_md5"], md5)
                self.assertEqual(result["file_size"], len(content))
                self.assertEqual(run.call_args_list[0].args[1:4],
                                 ("copyto", path, "private:TV/VOD/a1b2c3d4.mp4"))
                self.assertTrue(os.path.exists(path))

    def test_upload_failure_or_mismatch_always_preserves_source(self):
        storage = self.storage()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "movie.mp4")
            with open(path, "wb") as output:
                output.write(b"media")
            failures = [StorageError("upload failed"),
                        json.dumps({"Size": 5, "Hashes": {"MD5": "bad"}}),
                        json.dumps({"Size": 4, "Hashes": {"MD5": hashlib.md5(b"media").hexdigest()}}),
                        json.dumps({"Size": 5, "Hashes": {}})]
            for failure in failures:
                with self.subTest(failure=failure):
                    responses = ([failure] if isinstance(failure, Exception) else ["", failure])
                    with mock.patch.object(storage, "_run", side_effect=responses):
                        with self.assertRaises(StorageError):
                            storage.upload(path, "a1b2c3d4")
                    self.assertTrue(os.path.exists(path))

    def test_upload_accepts_lowercase_hash_name_from_rclone_160(self):
        storage = self.storage()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "prepared.mp4")
            with open(path, "wb") as output:
                output.write(b"verified media")
            checksum = hashlib.md5(b"verified media").hexdigest()
            with mock.patch.object(storage, "_run", side_effect=["", json.dumps({
                    "Size": len(b"verified media"), "Hashes": {"md5": checksum}})]):
                self.assertEqual(storage.upload(path, "a1b2c3d4")["remote_md5"], checksum)
                self.assertTrue(os.path.exists(path))

    def test_paths_and_delete_cannot_escape_configured_prefix(self):
        storage = self.storage()
        for bad in ("../a1b2c3d4.mp4", "other:a1b2c3d4.mp4", "/a1b2c3d4.mp4",
                    "a1b2c3d4.mp4/../../", "A1B2C3D4.mp4", "a1b2c3d4.mp4?x"):
            with self.subTest(path=bad), mock.patch.object(storage, "_run") as run:
                invalid = task()
                invalid["remote_path"] = bad
                with self.assertRaises(StorageError):
                    storage.delete(invalid)
                run.assert_not_called()
        wrong_owner = task()
        wrong_owner["id"] = "deadbeef"
        with self.assertRaises(StorageError):
            storage.delete(wrong_owner)
        with mock.patch.object(storage, "_run") as run:
            storage.delete(task())
            run.assert_called_once_with("delete", "deletefile", "private:TV/VOD/a1b2c3d4.mp4")
        for remote in ("drive:../movies", "drive:movies/../other", "-bad:movies", "drive:a\\b"):
            with self.assertRaises(ValueError):
                GDriveStorage(remote)

    def test_lru_byte_limit_cross_block_reads_and_invalidation(self):
        storage = self.storage(cache_bytes=16)
        storage.BLOCK_SIZE = 8
        data = bytes(range(48))
        fetches = []

        def fetch(filename, total, start, length):
            fetches.append(start)
            return data[start:start + length]

        with mock.patch.object(storage, "_fetch", side_effect=fetch):
            self.assertEqual(storage.read(task(), 5, 9), data[5:14])
            storage.read(task(), 0, 1)  # Block 0 most recently used.
            storage.read(task(), 16, 1)  # Evicts block 1.
            self.assertEqual(fetches, [0, 8, 16])
            self.assertLessEqual(storage._cache_size, 16)
            storage.read(task(), 8, 1)
            self.assertEqual(fetches, [0, 8, 16, 8])
            with mock.patch.object(storage, "_run"):
                storage.delete(task())
            self.assertEqual(storage._cache_size, 0)

    def test_concurrent_same_block_is_deduplicated(self):
        storage = self.storage()
        storage.BLOCK_SIZE = 8
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []

        def fetch(*args):
            entered.set()
            release.wait(2)
            return b"abcdefgh"

        def worker():
            try:
                results.append(storage.read(task(), 0, 4))
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(storage, "_fetch", side_effect=fetch) as fetch_mock:
            threads = [threading.Thread(target=worker) for _ in range(6)]
            for thread in threads:
                thread.start()
            self.assertTrue(entered.wait(1))
            release.set()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())
            self.assertFalse(errors)
            self.assertEqual(results, [b"abcd"] * 6)
            self.assertEqual(fetch_mock.call_count, 1)

    def test_maximum_two_parallel_fetches(self):
        storage = self.storage()
        storage.BLOCK_SIZE = 8
        guard = threading.Lock()
        running = peak = 0
        entered, release = threading.Event(), threading.Event()

        def fetch(*args):
            nonlocal running, peak
            with guard:
                running += 1
                peak = max(peak, running)
                if running == 2:
                    entered.set()
            release.wait(2)
            with guard:
                running -= 1
            return b"abcdefgh"

        with mock.patch.object(storage, "_fetch", side_effect=fetch):
            threads = [threading.Thread(target=storage.read, args=(task(), index * 8, 1))
                       for index in range(5)]
            for thread in threads:
                thread.start()
            self.assertTrue(entered.wait(1))
            release.set()
            for thread in threads:
                thread.join(3)
                self.assertFalse(thread.is_alive())
            self.assertEqual(peak, 2)

    def test_quota_returns_immediately_refreshes_once_and_uses_account_root(self):
        storage = self.storage()
        entered, release = threading.Event(), threading.Event()

        def about(*args, **kwargs):
            entered.set()
            release.wait(2)
            return json.dumps({"total": 5 * 1024**4, "used": 1024**4, "free": 4 * 1024**4})

        with mock.patch.object(storage, "_run", side_effect=about) as run:
            before = time.monotonic()
            first = storage.quota()
            self.assertLess(time.monotonic() - before, .2)
            self.assertTrue(first["updating"])
            self.assertTrue(entered.wait(1))
            storage.quota()
            self.assertEqual(run.call_count, 1)
            release.set()
            deadline = time.monotonic() + 2
            while storage.quota()["updating"] and time.monotonic() < deadline:
                time.sleep(.01)
            result = storage.quota()
            self.assertTrue(result["available"])
            self.assertEqual(result["total_gb"], 5120)
            run.assert_called_once_with("quota", "about", "private:", "--json", timeout=30)

    def test_subprocess_failure_does_not_expose_credentials(self):
        storage = self.storage()
        with mock.patch("subprocess.run", side_effect=subprocess.CalledProcessError(
                1, ["secret"], stderr="token=private_password")):
            with self.assertRaisesRegex(StorageError, "Google Drive quota failed") as caught:
                storage._run("quota", "about", "private:")
            self.assertNotIn("private_password", str(caught.exception))

    def test_warm_loads_head_next_window_and_tail(self):
        storage = self.storage()
        storage.BLOCK_SIZE = 8
        with mock.patch.object(storage, "_block") as block:
            storage.warm(task(48))
            self.assertEqual([call.args[1] for call in block.call_args_list], [0, 1, 2, 5])

    def test_invalidation_signals_only_owned_reader_without_holding_ram_lock(self):
        storage = self.storage()
        process = mock.Mock()
        process.poll.return_value = None

        def send(sig):
            self.assertEqual(sig, signal.SIGHUP)
            self.assertTrue(storage._lock.acquire(blocking=False))
            storage._lock.release()

        process.send_signal.side_effect = send
        storage._proc = process
        try:
            storage._invalidate("a1b2c3d4.mp4")
            process.send_signal.assert_called_once_with(signal.SIGHUP)
            process.terminate.assert_not_called()
        finally:
            storage._proc = None

    @unittest.skipUnless(shutil.which("rclone"), "rclone binary unavailable")
    def test_real_rclone_upload_verifies_hash_then_serves_published_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            config = os.path.join(directory, "rclone.conf")
            with open(config, "w") as output:
                output.write("[fixture]\ntype = local\n")
            remote = os.path.join(directory, "remote")
            os.mkdir(remote)
            source = os.path.join(directory, "prepared.mp4")
            content = bytes(range(256)) * 64
            with open(source, "wb") as output:
                output.write(content)
            storage = GDriveStorage("fixture:" + remote, config_path=config)
            try:
                result = storage.upload(source, "a1b2c3d4")
                self.assertEqual(result["remote_md5"], hashlib.md5(content).hexdigest())
                self.assertTrue(os.path.exists(source))
                self.assertEqual(storage.read(dict(result, id="a1b2c3d4"), 37, 500), content[37:537])
                process = storage._proc
                second = os.path.join(directory, "second.mp4")
                second_content = b"second prepared video" * 500
                with open(second, "wb") as output:
                    output.write(second_content)
                published = storage.upload(second, "deadbeef")
                self.assertEqual(storage.read(dict(published, id="deadbeef"), 37, 500), second_content[37:537])
                self.assertIs(storage._proc, process)
                self.assertIsNone(process.poll())
            finally:
                storage.close()

    @unittest.skipUnless(shutil.which("rclone"), "rclone binary unavailable")
    def test_real_rclone_lazy_server_and_cleanup_with_local_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            config = os.path.join(directory, "rclone.conf")
            with open(config, "w") as output:
                output.write("[fixture]\ntype = local\n")
            content = b"prepared media content" * 100
            with open(os.path.join(directory, "a1b2c3d4.mp4"), "wb") as output:
                output.write(content)
            storage = GDriveStorage("fixture:" + directory, config_path=config)
            try:
                self.assertIsNone(storage._proc)
                self.assertEqual(storage.read(task(len(content)), 12, 80), content[12:92])
                process = storage._proc
                self.assertIsNone(process.poll())
                # New publications must be visible in an already warm reader.
                other = b"second prepared VOD" * 100
                with open(os.path.join(directory, "deadbeef.mp4"), "wb") as output:
                    output.write(other)
                storage._invalidate("deadbeef.mp4")
                self.assertEqual(storage.read(task(len(other), "deadbeef"), 12, 80), other[12:92])
                self.assertIs(storage._proc, process)
            finally:
                storage.close()
            self.assertIsNotNone(process.poll())


class HTTPRangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.content = bytes(range(128))
        cls.requests = []
        cls.mode = "valid"

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                cls.requests.append(self.headers.get("Range"))
                start, end = map(int, self.headers["Range"].removeprefix("bytes=").split("-"))
                data = cls.content[start:end + 1]
                if cls.mode == "header_trickle":
                    try:
                        for value in b"HTTP/1.1 206 Partial Content\r\n":
                            self.connection.sendall(bytes([value]))
                            time.sleep(.04)
                    except OSError:
                        pass
                    return
                self.send_response(200 if cls.mode == "ignored" else 206)
                cr_start = start + 1 if cls.mode == "wrong_offset" else start
                self.send_header("Content-Range", "bytes %d-%d/%d" % (cr_start, end, len(cls.content)))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    if cls.mode == "trickle":
                        for value in data:
                            self.wfile.write(bytes([value]))
                            self.wfile.flush()
                            time.sleep(.04)
                    else:
                        self.wfile.write(data[:2] if cls.mode == "short" else data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        cls.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join(2)

    def setUp(self):
        type(self).mode = "valid"
        type(self).requests.clear()
        self.storage = GDriveStorage("drive:VOD")
        self.storage.BLOCK_SIZE = 8
        self.patch = mock.patch.object(self.storage, "_ensure_server", return_value=
                                       "http://127.0.0.1:%d/" % self.http.server_port)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.storage.close)

    def test_valid_ranges_read_exact_offsets_and_hit_cache(self):
        self.assertEqual(self.storage.read(task(128), 10, 4), self.content[10:14])
        self.assertEqual(self.storage.read(task(128), 12, 2), self.content[12:14])
        self.assertEqual(self.requests, ["bytes=8-15"])

    def test_bad_http_status_range_or_short_body_is_not_cached(self):
        for mode in ("ignored", "wrong_offset", "short"):
            with self.subTest(mode=mode):
                type(self).mode = mode
                with self.assertRaises(StorageError):
                    self.storage.read(task(128), 8, 4)
                self.assertFalse(self.storage._cache)
                self.assertFalse(self.storage._inflight)
        type(self).mode = "valid"
        self.assertEqual(self.storage.read(task(128), 8, 4), self.content[8:12])

    def test_slow_trickle_exceeds_absolute_deadline_despite_regular_bytes(self):
        self.storage.HTTP_TIMEOUT = .15
        for mode in ("trickle", "header_trickle"):
            with self.subTest(mode=mode):
                type(self).mode = mode
                before = time.monotonic()
                with self.assertRaises(StorageError):
                    self.storage.read(task(128), 8, 4)
                self.assertLess(time.monotonic() - before, .4)
                self.assertFalse(self.storage._cache)
                self.assertFalse(self.storage._inflight)

    def test_saturated_fetch_slots_fail_within_queue_deadline(self):
        self.storage.SLOT_TIMEOUT = .05
        self.storage._fetch_slots.acquire()
        self.storage._fetch_slots.acquire()
        try:
            before = time.monotonic()
            with self.assertRaisesRegex(StorageError, "busy"):
                self.storage.read(task(128), 8, 4)
            self.assertLess(time.monotonic() - before, .3)
            self.assertFalse(self.storage._inflight)
        finally:
            self.storage._fetch_slots.release()
            self.storage._fetch_slots.release()


if __name__ == "__main__":
    unittest.main()
