import json
import tempfile
import unittest
from pathlib import Path

from vod_history import VodHistory, resolve_episode_identity


def episode(task_id, number, *, status="ready", storage="gdrive"):
    return {
        "id": task_id,
        "title": f"A Noiva de Istambul {number} - Star 528p",
        "status": status,
        "storage": storage,
        "file_size": 1000 + number,
        "remote_md5": "same-bytes-" + str(number),
    }


class TestVodHistory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vod-history-test-")
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "watch_history.json")
        self.now = 1000.0
        self.history = VodHistory(self.path, clock=lambda: self.now)

    def test_istanbullu_identity_never_treats_resolution_as_episode(self):
        identity = resolve_episode_identity(episode("a1b2c3d4", 178))
        self.assertEqual(identity["series_key"], "import:istanbullu-star")
        self.assertEqual(identity["series_title"], "A Noiva de Istambul")
        self.assertEqual(identity["episode_number"], 178)
        self.assertIsNone(identity["season_number"])
        self.assertIsNone(resolve_episode_identity({"id": "a1b2c3d4", "title": "Filme 2026 528p"}))

    def test_checkpoint_is_atomic_and_survives_reload(self):
        task = episode("a1b2c3d4", 172)
        self.history.checkpoint(task, 321.5)
        on_disk = json.loads(Path(self.path).read_text())
        self.assertIn("import:istanbullu-star", on_disk["series"])
        reloaded = VodHistory(self.path)
        summary = reloaded.summary({task["id"]: task})
        self.assertEqual(summary[0]["task_id"], task["id"])
        self.assertEqual(summary[0]["position_seconds"], 321.5)
        self.assertEqual(summary[0]["position_confidence"], "manual")

    def test_manual_backward_checkpoint_replaces_prior_position(self):
        task = episode("a1b2c3d4", 172)
        self.history.checkpoint(task, 800)
        self.history.checkpoint(task, 120)
        summary = self.history.summary({task["id"]: task})
        self.assertEqual(summary[0]["position_seconds"], 120)

    def test_completion_offers_only_immediate_next_ready_episode(self):
        first, second, fourth = episode("a1b2c3d4", 172), episode("b1c2d3e4", 173), episode("c1d2e3f4", 175)
        self.history.complete(first)
        summary = self.history.summary({item["id"]: item for item in (first, second, fourth)})
        self.assertEqual(summary[0]["task_id"], second["id"])
        self.assertTrue(summary[0]["available"])

        self.history.complete(second)
        summary = self.history.summary({item["id"]: item for item in (first, second, fourth)})
        self.assertEqual(summary[0]["episode_number"], 174, "must not skip missing episode 174")
        self.assertFalse(summary[0]["available"])
        self.assertIsNone(summary[0]["task_id"])

    def test_checkpoint_corrects_an_accidentally_completed_episode(self):
        first, second = episode("a1b2c3d4", 172), episode("b1c2d3e4", 173)
        self.history.complete(first)
        self.history.checkpoint(first)
        summary = self.history.summary({item["id"]: item for item in (first, second)})
        self.assertEqual(summary[0]["task_id"], first["id"])
        self.assertFalse(summary[0]["completed"])

    def test_migration_keeps_progress_when_task_and_bytes_do_not_change(self):
        task = episode("a1b2c3d4", 172, storage="local")
        self.history.checkpoint(task)
        migrated = dict(task, storage="gdrive", remote_path="a1b2c3d4.mp4")
        summary = self.history.summary({migrated["id"]: migrated})
        self.assertEqual(summary[0]["task_id"], migrated["id"])
        self.assertTrue(summary[0]["available"])

    def test_invalid_json_recovers_empty_without_touching_vod_catalog(self):
        Path(self.path).write_text("not json")
        recovered = VodHistory(self.path)
        self.assertEqual(recovered.summary({}), [])

    def test_non_series_task_is_never_recorded(self):
        task = {"id": "a1b2c3d4", "title": "Filme 528p", "status": "ready"}
        self.assertIsNone(self.history.record_selected(task))
        self.assertFalse(Path(self.path).exists())


if __name__ == "__main__":
    unittest.main()
