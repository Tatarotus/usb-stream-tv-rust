import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from vod_playback_cache import CurrentVideoCache


MIB = 1024**2
VIDEO = (bytes(range(256)) * (20 * MIB // 256))


class PlaybackCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "current"
        self.calls = []
        self.task = {"id": "deadbeef", "storage": "gdrive", "remote_path": "deadbeef.mp4",
                     "file_size": len(VIDEO), "remote_md5": hashlib.md5(VIDEO).hexdigest()}
        self.cache = CurrentVideoCache(self.directory, self.fetch, max_bytes=32 * MIB,
                                       min_free_bytes=0)

    def tearDown(self):
        self.cache.close()
        self.temp.cleanup()

    def fetch(self, task, start, count):
        self.calls.append((task["id"], start, count))
        return VIDEO[start:start + count]

    def wait_complete(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.cache._lock:
                state = self.cache._state
                if state and len(state["blocks"]) == 5:
                    return
            time.sleep(0.01)
        self.fail("Background cache did not complete")

    def test_constructor_is_inert(self):
        self.assertFalse(self.directory.exists())
        self.assertIsNone(self.cache._worker)
        self.assertEqual(self.calls, [])

    def test_warm_and_background_completion_serve_without_origin(self):
        self.assertTrue(self.cache.start(self.task))
        self.wait_complete()
        with mock.patch.object(self.cache, "fetch", side_effect=AssertionError("origin hit")):
            self.assertEqual(self.cache.read(self.task, 7 * MIB + 51, 2 * MIB),
                             VIDEO[7 * MIB + 51:9 * MIB + 51])
            self.assertEqual(self.cache.read(self.task, len(VIDEO) - 11, 100), VIDEO[-11:])
            self.assertEqual(self.cache.read(self.task, len(VIDEO), 4), b"")
        paths = list(self.directory.glob("*.cache"))
        self.assertEqual(len(paths), 1)
        self.assertLessEqual(paths[0].stat().st_blocks * 512, self.cache.max_bytes)

    def test_verified_seed_skips_cloud_and_preserves_source(self):
        seed = Path(self.temp.name) / "final.mp4"
        seed.write_bytes(VIDEO)
        with mock.patch.object(self.cache, "fetch", side_effect=AssertionError("origin hit")):
            self.assertTrue(self.cache.start(self.task, seed))
            self.assertEqual(self.cache.read(self.task, 9 * MIB, 500), VIDEO[9 * MIB:9 * MIB + 500])
        self.assertEqual(seed.read_bytes(), VIDEO)

    def test_same_filesystem_seed_reuses_inode_and_survives_source_unlink(self):
        seed = Path(self.temp.name) / "final.mp4"
        seed.write_bytes(VIDEO)
        self.cache.start(self.task, seed)
        cached = next(self.directory.glob("*.cache"))
        self.assertEqual(seed.stat().st_ino, cached.stat().st_ino)
        self.assertEqual(seed.stat().st_dev, cached.stat().st_dev)
        seed.unlink()
        with mock.patch.object(self.cache, "fetch", side_effect=AssertionError("origin hit")):
            self.assertEqual(self.cache.read(self.task, 9 * MIB, 200), VIDEO[9 * MIB:9 * MIB + 200])

    def test_seed_cross_filesystem_falls_back_to_bounded_copy(self):
        seed = Path(self.temp.name) / "final.mp4"
        seed.write_bytes(VIDEO)
        with mock.patch("vod_playback_cache.os.link", side_effect=OSError("cross-device link")):
            self.cache.start(self.task, seed)
        cached = next(self.directory.glob("*.cache"))
        self.assertNotEqual(seed.stat().st_ino, cached.stat().st_ino)
        with mock.patch.object(self.cache, "fetch", side_effect=AssertionError("origin hit")):
            self.assertEqual(self.cache.read(self.task, 9 * MIB, 200), VIDEO[9 * MIB:9 * MIB + 200])

    def test_seed_checksum_mismatch_uses_origin(self):
        seed = Path(self.temp.name) / "wrong.mp4"
        seed.write_bytes(b"x" * len(VIDEO))
        self.cache.start(self.task, seed)
        self.assertTrue(self.calls)
        self.assertEqual(self.cache.read(self.task, 9, 83), VIDEO[9:92])
        self.assertEqual(seed.stat().st_size, len(VIDEO))

    def test_budget_fallback_does_not_create_disk_movie(self):
        self.cache.max_bytes = MIB
        self.assertFalse(self.cache.start(self.task))
        self.assertFalse(self.directory.exists())
        self.assertIsNone(self.cache._worker)
        self.assertEqual(self.cache.read(self.task, 1234, 200), VIDEO[1234:1434])

    def test_low_disk_space_falls_back_without_failing_playback(self):
        usage = mock.Mock(free=1024)
        with mock.patch("vod_playback_cache.shutil.disk_usage", return_value=usage):
            self.assertFalse(self.cache.start(self.task))
            self.assertEqual(self.cache.read(self.task, 100, 200), VIDEO[100:300])
        self.assertEqual(list(self.directory.glob("*.cache")), [])

    def test_disk_space_drop_preserves_cached_bytes_and_stops_download(self):
        reached = threading.Event()
        resume = threading.Event()

        def slow(task, offset, count):
            if offset == 8 * MIB:
                reached.set()
                resume.wait(3)
            return self.fetch(task, offset, count)

        self.cache.fetch = slow
        self.cache.start(self.task)
        self.assertTrue(reached.wait(2))
        try:
            with mock.patch("vod_playback_cache.shutil.disk_usage", return_value=mock.Mock(free=0)):
                resume.set()
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    with self.cache._lock:
                        if self.cache._state["paused"]:
                            break
                    time.sleep(0.01)
                with self.cache._lock:
                    self.assertTrue(self.cache._state["paused"])
                with mock.patch.object(self.cache, "fetch", side_effect=AssertionError("origin hit")):
                    self.assertEqual(self.cache.read(self.task, 7, 19), VIDEO[7:26])
        finally:
            resume.set()

    def test_probe_of_another_video_does_not_replace_current(self):
        self.cache.start(self.task)
        other = dict(self.task, id="cafebabe", remote_path="cafebabe.mp4")
        self.assertEqual(self.cache.read(other, 50, 100), VIDEO[50:150])
        with self.cache._lock:
            self.assertEqual(self.cache._state["key"][0], "deadbeef")
        self.assertEqual(len(list(self.directory.glob("*.cache"))), 1)

    def test_stop_during_fetch_prevents_old_generation_publication(self):
        reached = threading.Event()
        resume = threading.Event()

        def slow(task, offset, count):
            if task["id"] == "deadbeef" and offset == 8 * MIB:
                reached.set()
                resume.wait(3)
            return self.fetch(task, offset, count)

        self.cache.fetch = slow
        self.cache.start(self.task)
        worker = self.cache._worker
        self.assertTrue(reached.wait(2))
        try:
            self.cache.stop()
            self.assertEqual(list(self.directory.glob("*.cache")), [])
            other = dict(self.task, id="cafebabe", remote_path="cafebabe.mp4")
            self.cache.start(other)
            self.assertIs(self.cache._worker, worker)
            resume.set()
            self.wait_complete()
            with self.cache._lock:
                self.assertEqual(self.cache._state["key"][0], "cafebabe")
            self.assertEqual(len(list(self.directory.glob("*.cache"))), 1)
        finally:
            resume.set()

    def test_short_origin_response_is_not_published(self):
        self.cache.fetch = lambda task, offset, count: b"short"
        with self.assertRaisesRegex(OSError, "Incomplete"):
            self.cache.start(self.task)
        with self.cache._lock:
            self.assertEqual(self.cache._state["blocks"], set())
            self.assertEqual(self.cache._state["inflight"], {})
        self.cache.fetch = self.fetch
        self.assertEqual(self.cache.read(self.task, 0, 12), VIDEO[:12])

    def test_parallel_readers_share_background_block_fetch(self):
        reached = threading.Event()
        resume = threading.Event()
        results = []

        def slow(task, offset, count):
            if offset == 8 * MIB:
                reached.set()
                resume.wait(3)
            return self.fetch(task, offset, count)

        self.cache.fetch = slow
        self.cache.start(self.task)
        self.assertTrue(reached.wait(2))
        readers = [threading.Thread(target=lambda: results.append(
            self.cache.read(self.task, 8 * MIB + 9, 200))) for _ in range(2)]
        try:
            for reader in readers:
                reader.start()
            resume.set()
            for reader in readers:
                reader.join(timeout=3)
                self.assertFalse(reader.is_alive())
            self.assertEqual(results, [VIDEO[8 * MIB + 9:8 * MIB + 209]] * 2)
            self.assertEqual(sum(offset == 8 * MIB for _, offset, _ in self.calls), 1)
        finally:
            resume.set()
            for reader in readers:
                reader.join(timeout=3)

    def test_restart_cleans_owned_stale_files_only(self):
        self.directory.mkdir()
        stale = self.directory / "current-vod-old123.cache"
        unrelated = self.directory / "keep.txt"
        stale.write_bytes(b"stale")
        unrelated.write_bytes(b"user data")
        self.cache.start(self.task)
        self.assertFalse(stale.exists())
        self.assertEqual(unrelated.read_bytes(), b"user data")

    def test_second_owner_cannot_remove_or_overwrite_active_cache(self):
        self.cache.start(self.task)
        owned = list(self.directory.glob("*.cache"))
        second = CurrentVideoCache(self.directory, self.fetch, max_bytes=32 * MIB,
                                   min_free_bytes=0)
        try:
            self.assertFalse(second.start(self.task))
            self.assertEqual(list(self.directory.glob("*.cache")), owned)
        finally:
            second.close()

    def test_invalid_identifier_or_path_rejected_before_io(self):
        for change in ({"id": "../bad"}, {"remote_path": "../deadbeef.mp4"},
                       {"file_size": -1}, {"remote_md5": "invalid"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.cache.start(dict(self.task, **change))
        self.assertFalse(self.directory.exists())

    def test_close_removes_only_owned_file_and_stops_worker(self):
        self.cache.start(self.task)
        worker = self.cache._worker
        self.cache.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(list(self.directory.glob("*.cache")), [])
        with self.assertRaises(RuntimeError):
            self.cache.start(self.task)


if __name__ == "__main__":
    unittest.main()
