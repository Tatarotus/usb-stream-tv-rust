import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from vod_playback_cache import CurrentVideoCache
import vod_revision


MIB = 1024**2
VIDEO = (bytes(range(256)) * (20 * MIB // 256))
GEN1 = bytes([0x11]) * len(VIDEO)
GEN2 = bytes([0x22]) * len(VIDEO)


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

    def test_episode_prefetch_reuses_next_and_retains_previous(self):
        self.cache.max_bytes = 64 * MIB
        self.cache._background_interval = 0
        nxt = dict(self.task, id="cafebabe", remote_path="cafebabe.mp4")
        self.assertTrue(self.cache.select_episode(self.task, nxt))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.cache._lock:
                states = list(self.cache._episode_states.values())
                if len(states) == 2 and all(len(s["blocks"]) == 5 for s in states):
                    break
            time.sleep(.01)
        else:
            self.fail("Prefetch did not finish")
        self.assertLess(max(i for i, call in enumerate(self.calls) if call[0] == "deadbeef"),
                        min(i for i, call in enumerate(self.calls) if call[0] == "cafebabe"))
        self.assertTrue(self.cache.select_episode(nxt))
        with mock.patch.object(self.cache, "fetch", side_effect=AssertionError("origin hit")):
            self.assertEqual(self.cache.read(nxt, 9 * MIB, 16), VIDEO[9 * MIB:9 * MIB + 16])
            self.assertEqual(self.cache.read(self.task, 9 * MIB, 16), VIDEO[9 * MIB:9 * MIB + 16])

    def test_episode_prefetch_obeys_shared_limit_and_evicts_old(self):
        self.cache.max_bytes = 40 * MIB
        second = dict(self.task, id="cafebabe", remote_path="cafebabe.mp4")
        third = dict(self.task, id="00000001", remote_path="00000001.mp4")
        self.assertTrue(self.cache.select_episode(self.task, second))
        self.assertTrue(self.cache.select_episode(second, third))
        with self.cache._lock:
            self.assertNotIn(self.cache._identity(self.task), self.cache._episode_states)
            self.assertEqual(set(self.cache._episode_states), {self.cache._identity(second), self.cache._identity(third)})
            self.assertLessEqual(sum(k[2] for k in self.cache._episode_states), self.cache.max_bytes)

    def test_episode_worker_yields_between_requests_and_reads_do_not_wait(self):
        self.cache._background_interval = .3
        first = threading.Event()
        times = []
        def fetch(task, start, count):
            times.append(time.monotonic())
            first.set()
            return VIDEO[start:start + count]
        self.cache.fetch = fetch
        self.assertTrue(self.cache.select_episode(self.task))
        self.assertTrue(first.wait(2))
        deadline = time.monotonic() + 2
        while len(times) < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertGreaterEqual(len(times), 2)
        self.assertGreaterEqual(times[1] - times[0], .25)
        started = time.monotonic()
        self.assertEqual(self.cache.read(self.task, 0, 64), VIDEO[:64])
        self.assertLess(time.monotonic() - started, .2)

    def test_episode_cache_expires_after_inactivity_even_for_current_and_next(self):
        self.cache.max_bytes = 64 * MIB
        nxt = dict(self.task, id="cafebabe", remote_path="cafebabe.mp4")
        self.assertTrue(self.cache.select_episode(self.task, nxt))
        with self.cache._lock:
            self.cache._episode_last_activity = time.monotonic() - self.cache._episode_retention_seconds - 1
        self.cache._wake.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with self.cache._lock:
                if not self.cache._episode_states:
                    break
            time.sleep(.01)
        else:
            self.fail("Idle cache did not expire")
        self.assertEqual(list(self.directory.glob("*.cache")), [])

    def test_episode_retention_is_72h_and_the_boundary_is_respected(self):
        """Limiar temporal: 72 h — preso à CONSTANTE, não ao valor dinâmico.

        O teste antigo subtraía `_episode_retention_seconds` do relógio, então
        passava com 6 h e com 72 h: não provava nada sobre o LIMIAR. Aqui o valor
        é afirmado e o comportamento é exercitado nos dois lados da fronteira.
        """
        self.assertEqual(self.cache._episode_retention_seconds, 3 * 24 * 3600,
                         "retenção temporal deve ser 72 h")
        self.assertEqual(self.cache._episode_max_entries, 4,
                         "o limite de ENTRADAS segue 4 e é independente do tempo")
        # A mudança de hoje é SÓ temporal: a reserva de espaço e o teto do cache
        # continuam os mesmos defaults do construtor (6 GiB de cache, 8 GiB de
        # reserva livre). Se alguém mexer neles, este teste falha aqui.
        import inspect
        params = inspect.signature(CurrentVideoCache.__init__).parameters
        self.assertEqual(params["max_bytes"].default, 6 * 1024**3,
                         "teto do cache deve seguir 6 GiB")
        self.assertEqual(params["min_free_bytes"].default, 8 * 1024**3,
                         "reserva de espaço livre deve seguir 8 GiB")
        self.assertEqual(params["block_size"].default, 4 * 1024**2,
                         "o bloco de 4 MiB (mesmo teto da requisição da TV) segue igual")
        self.cache.max_bytes = 64 * MIB      # espaço de sobra: nada evicta por capacidade
        nxt = dict(self.task, id="cafebabe", remote_path="cafebabe.mp4")
        self.assertTrue(self.cache.select_episode(self.task, nxt))

        # 71 h de inatividade: RETIDO (antes do limiar).
        with self.cache._lock:
            self.cache._episode_last_activity = time.monotonic() - 71 * 3600
        self.cache._wake.set()
        time.sleep(.6)
        with self.cache._lock:
            self.assertTrue(self.cache._episode_states,
                            "71 h de inatividade NÃO pode descartar o cache")

        # 73 h: DESCARTADO (após o limiar).
        with self.cache._lock:
            self.cache._episode_last_activity = time.monotonic() - 73 * 3600
        self.cache._wake.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.cache._lock:
                if not self.cache._episode_states:
                    break
            time.sleep(.02)
        else:
            self.fail("73 h de inatividade deveria descartar o cache")

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

    def revision_task(self, revision=None):
        task = {"id": "deadbeef", "storage": "gdrive", "remote_path": "deadbeef.mp4",
                "file_size": len(VIDEO)}
        if revision is not None:
            task["media_revision"] = revision
        return task

    def generation_fetch(self, generations):
        def fetch(task, start, count):
            marker = task.get("media_revision") or task.get("content_revision")
            source = generations[marker]
            self.calls.append((task["id"], start, count))
            return source[start:start + count]
        return fetch

    def test_changed_revision_discards_disk_backed_bytes(self):
        first = self.revision_task("generation-1")
        second = dict(first, media_revision="generation-2")
        self.cache.fetch = self.generation_fetch({"generation-1": GEN1, "generation-2": GEN2})
        self.assertTrue(self.cache.start(first))
        self.assertEqual(self.cache.read(first, 0, 16), GEN1[:16])
        before = len(self.calls)
        self.assertTrue(self.cache.start(second))
        with self.cache._lock:
            self.assertEqual(self.cache._state["key"], self.cache._identity(second))
            self.assertNotEqual(self.cache._state["key"], self.cache._identity(first))
        self.assertEqual(self.cache.read(second, 0, 16), GEN2[:16])
        self.assertNotEqual(self.cache.read(second, 0, 16), GEN1[:16])
        self.assertGreater(len(self.calls), before)
        self.assertEqual(len(list(self.directory.glob("*.cache"))), 1)

    def test_changed_revision_misses_in_memory_secondary_cache(self):
        self.cache.max_bytes = MIB  # too small for a disk-backed movie
        first = self.revision_task("generation-1")
        second = dict(first, media_revision="generation-2")
        self.cache.fetch = self.generation_fetch({"generation-1": GEN1, "generation-2": GEN2})
        self.assertEqual(self.cache.read(first, 0, 32), GEN1[:32])
        before = len(self.calls)
        self.assertEqual(self.cache.read(second, 0, 32), GEN2[:32])
        self.assertGreater(len(self.calls), before)
        with self.cache._lock:
            self.assertIsNone(self.cache._state)
            self.assertIn((self.cache._identity(second), 0), self.cache._secondary_cache)
        after = len(self.calls)
        self.assertEqual(self.cache.read(second, 0, 32), GEN2[:32])
        self.assertEqual(len(self.calls), after)

    def test_unchanged_revision_hits_disk_cache_without_origin(self):
        task = self.revision_task("generation-1")
        self.cache.fetch = self.generation_fetch({"generation-1": GEN1})
        self.assertTrue(self.cache.start(task))
        before = len(self.calls)
        with mock.patch.object(self.cache, "fetch", side_effect=AssertionError("origin hit")):
            self.assertEqual(self.cache.read(task, MIB + 3, 4096), GEN1[MIB + 3:MIB + 3 + 4096])
        self.assertEqual(len(self.calls), before)

    def test_md5_identity_still_hits_cache_and_carries_revision(self):
        self.cache.start(self.task)
        self.wait_complete()
        digest = hashlib.md5(VIDEO).hexdigest()
        self.assertEqual(self.cache._identity(self.task),
                         ("deadbeef", digest, len(VIDEO), "md5:" + digest))
        before = len(self.calls)
        with mock.patch.object(self.cache, "fetch", side_effect=AssertionError("origin hit")):
            self.assertEqual(self.cache.read(self.task, 2, 8), VIDEO[2:10])
        self.assertEqual(len(self.calls), before)

    def test_unverifiable_revision_still_readable_without_raising(self):
        task = self.revision_task()
        self.assertIsNone(vod_revision.content_revision(task))
        self.assertTrue(self.cache.start(task))
        self.assertEqual(self.cache.read(task, 100, 64), VIDEO[100:164])
        self.assertEqual(self.cache._identity(task), ("deadbeef", "", len(VIDEO), None))

    def test_explicit_revision_change_invalidates_memory_cache(self):
        self.cache.max_bytes = MIB
        first = self.revision_task()
        first["content_revision"] = "md5:" + "a" * 32
        second = dict(first, content_revision="md5:" + "b" * 32)
        self.cache.fetch = self.generation_fetch({"md5:" + "a" * 32: GEN1, "md5:" + "b" * 32: GEN2})
        self.assertEqual(self.cache.read(first, 0, 32), GEN1[:32])
        before = len(self.calls)
        self.assertEqual(self.cache.read(second, 0, 32), GEN2[:32])
        self.assertGreater(len(self.calls), before)


if __name__ == "__main__":
    unittest.main()
