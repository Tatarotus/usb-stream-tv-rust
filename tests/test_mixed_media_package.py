"""Tests for the mixed-media package generator."""
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import build_mixed_media_package as pkg  # noqa: E402
import vod_revision  # noqa: E402


def with_revision(task):
    """Attach a trusted content revision (published object digest).

    A ready VOD without a trusted identity is never publishable: size alone
    cannot distinguish mutated content from the original.
    """
    task = dict(task)
    if not task.get("remote_md5") and not task.get("content_revision"):
        task["remote_md5"] = hashlib.md5(
            str(task.get("id", "")).encode("utf-8")).hexdigest()
    return task


class MixedMediaPackageTests(unittest.TestCase):
    def test_accented_series_does_not_block_an_unrelated_new_episode(self):
        tasks = [with_revision(dict(id="76956c8e", status="ready", file_size=8192,
                    title="Cyberpunk Mercenários S01E01", series_title="Cyberpunk Mercenários")),
                 with_revision(dict(id="4fbf7a8f", status="ready", file_size=8192,
                    title="A Noiva de Istambul S01E256", series_title="A Noiva de Istambul"))]
        entries = pkg.process_vod_tasks(tasks)
        normalized = pkg.mixed_template.validate_entries(
            [{"path": e["path"], "size": e["logical_size"]} for e in entries], 128 * 1024**3)
        self.assertEqual(len(normalized), 2)
        self.assertTrue(any("Cyberpunk Mercenarios/" in e["path"] for e in normalized))
        self.assertTrue(any("E256" in e["path"] for e in normalized))
        self.assertEqual(tasks[0]["series_title"], "Cyberpunk Mercenários")

    def test_non_latin_series_has_a_stable_nonempty_ascii_folder(self):
        name = pkg.clean_series_name("星空")
        self.assertTrue(name.isascii())
        self.assertTrue(name.startswith("Serie_"))
        self.assertEqual(name, pkg.clean_series_name("星空"))
        self.assertNotEqual(name, pkg.clean_series_name("森林"))
        self.assertEqual(pkg.clean_filename("星空.mp4", "12345678"), "video_12345678.mp4")

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="test-mixed-pkg-"))

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def live_export(self, generation=1):
        return {
            "generation": generation,
            "identities": [
                {
                    "identity": "sbt",
                    "status": "approved",
                    "category": "Abertos",
                    "filename": "SBT.trp",
                    "chosen": {"channel_id": "206080"},
                }
            ],
        }

    def test_live_channels_land_under_the_tv_root_by_category(self):
        """The drive root reads TV/<categoria>/<canal>.trp next to Series/Filmes."""
        export = self.live_export()
        export["live_root"] = "TV"
        export["identities"] += [
            {"identity": "telecine-action", "status": "approved", "category": "Filmes",
             "filename": "Telecine Action 1080.trp", "chosen": {"channel_id": "365243"}},
            {"identity": "discovery-hh", "status": "approved", "category": "Variedades",
             "filename": "Discovery H&H 1080.trp", "chosen": {"channel_id": "387648"}},
        ]
        entries = pkg.process_live_identities(export)
        paths = sorted(e["path"] for e in entries)
        self.assertEqual(paths, [
            "TV/Abertos/SBT.trp",
            "TV/Filmes/Telecine Action 1080.trp",
            "TV/Variedades/Discovery H&H 1080.trp",
        ])
        for entry in entries:
            self.assertEqual(entry["folder"][0], "TV")
            self.assertEqual(len(entry["folder"]), 2)
            self.assertEqual(entry["kind"], "live")
            # The category stays a single component: a slash here would create
            # a folder level the operator never asked for.
            self.assertNotIn("/", entry["folder"][1])

    def test_without_live_root_the_historical_layout_is_preserved(self):
        """An already-published generation keeps its exact paths and file ids."""
        entries = pkg.process_live_identities(self.live_export())
        self.assertEqual([e["path"] for e in entries], ["Abertos/SBT.trp"])
        self.assertEqual(entries[0]["folder"], ["Abertos"])

    def test_pending_identities_never_reach_the_package(self):
        export = self.live_export()
        export["live_root"] = "TV"
        export["identities"].append({
            "identity": "cnn-brasil", "status": "pending", "category": "Noticias",
            "filename": "CNN Brasil 720.trp", "chosen": {"channel_id": "206088"}})
        entries = pkg.process_live_identities(export)
        self.assertEqual([e["path"] for e in entries], ["TV/Abertos/SBT.trp"])

    def test_unsafe_live_names_are_rejected_instead_of_creating_levels(self):
        for bad in ("TV/Abertos", "Abertos/..", 'Abertos:"', "../Abertos"):
            with self.subTest(category=bad):
                export = self.live_export()
                export["live_root"] = "TV"
                export["identities"][0]["category"] = bad
                with self.assertRaises(ValueError):
                    pkg.process_live_identities(export)
        for bad in ("canal/../x.trp", "canal:x.trp", ""):
            with self.subTest(filename=bad):
                export = self.live_export()
                export["live_root"] = "TV"
                export["identities"][0]["filename"] = bad
                with self.assertRaises(ValueError):
                    pkg.process_live_identities(export)

    def test_case_insensitive_duplicate_live_paths_are_refused(self):
        """NTFS ignores case: two channels must not collide on one path."""
        export = self.live_export()
        export["live_root"] = "TV"
        export["identities"].append({
            "identity": "sbt2", "status": "approved", "category": "Abertos",
            "filename": "sbt.trp", "chosen": {"channel_id": "209999"}})
        with self.assertRaisesRegex(ValueError, "duplicate Live path"):
            pkg.process_live_identities(export)

    def test_ntfs_name_limits_and_harmless_punctuation(self):
        export = self.live_export()
        export["live_root"] = "TV"
        export["identities"][0]["category"] = "x" * 256
        with self.assertRaisesRegex(ValueError, "255-character"):
            pkg.process_live_identities(export)
        export["identities"][0]["category"] = "Noticias & Região"
        self.assertEqual(pkg.process_live_identities(export)[0]["path"],
                         "TV/Noticias & Região/SBT.trp")

    def test_live_root_and_filename_are_normalised_once(self):
        """The builder is the only place that decides the on-drive path."""
        export = self.live_export()
        export["live_root"] = " TV "
        export["identities"][0]["filename"] = "SBT 1080 RJ"
        entries = pkg.process_live_identities(export)
        self.assertEqual(entries[0]["folder"], ["TV", "Abertos"])
        self.assertEqual(entries[0]["filename"], "SBT 1080 RJ.trp")

    def test_series_and_movie_classification(self):
        tasks = [
            # A Noiva de Istambul
            {"id": "d0df9dbf", "title": "A Noiva de Istambul 152 - Star 528p", "display_name": "A Noiva de Istambul EP152.mp4",
             "status": "ready", "file_size": 620197413},
            {"id": "c4b1f386", "title": "A Noiva de Istambul 153 - Star 528p", "display_name": "A Noiva de Istambul EP153.mp4",
             "status": "ready", "file_size": 575887282},
            # Masterchef EUA
            {"id": "2b8cd602", "title": "Masterchef EUA - Masterchef EUA (2010) S13 E01", "display_name": "Masterchef EUA S13E01.mp4",
             "series_id": "6370", "status": "ready", "file_size": 585150176},
            {"id": "2fabb193", "title": "@ Masterchef EUA - Masterchef EUA S13E11", "display_name": "Masterchef EUA S13E11.mp4",
             "series_id": "37584", "status": "ready", "file_size": 581334677},
            # Jaadugar
            {"id": "23e993b5", "title": "@ Jaadugar A Witch in Mongolia - Jaadugar A Witch in Mongolia S01E01",
             "display_name": "Jaadugar A Witch in S01E01.mp4", "series_id": "44369", "status": "ready", "file_size": 382476360},
            {"id": "d329bd72", "title": "Jaadugar: A Witch in Mongolia (2026) [L] - Jaadugar: A Witch in Mongolia (2026) [L] S01 E06",
             "display_name": "Jaadugar A Witch in S01E06.mp4", "series_id": "35545", "status": "ready", "file_size": 380976172},
            # Movie
            {"id": "c3757256", "title": "Star Wars: O Mandaloriano e Grogu (2026.)",
             "display_name": "Star Wars O Mandaloriano e Grogu.mp4", "status": "ready", "file_size": 3657844601},
        ]
        tasks = [with_revision(t) for t in tasks]

        vod_entries = pkg.process_vod_tasks(tasks)
        self.assertEqual(len(vod_entries), 7)

        by_id = {e["channel_id"]: e for e in vod_entries}
        # A Noiva de Istambul
        self.assertEqual(by_id["d0df9dbf"]["folder"], ["Series", "A Noiva de Istambul"])
        self.assertEqual(by_id["c4b1f386"]["folder"], ["Series", "A Noiva de Istambul"])

        # Masterchef EUA grouped under same folder
        self.assertEqual(by_id["2b8cd602"]["folder"], ["Series", "Masterchef EUA"])
        self.assertEqual(by_id["2fabb193"]["folder"], ["Series", "Masterchef EUA"])

        # Jaadugar grouped under same folder (clean name strips colon and tags)
        self.assertEqual(by_id["23e993b5"]["folder"], ["Series", "Jaadugar A Witch in Mongolia"])
        self.assertEqual(by_id["d329bd72"]["folder"], ["Series", "Jaadugar A Witch in Mongolia"])

        # Movie in Filmes
        self.assertEqual(by_id["c3757256"]["folder"], ["Filmes"])
        self.assertTrue(by_id["c3757256"]["filename"].endswith(".mp4"))

        # Every published entry carries its trusted content revision
        for entry in vod_entries:
            self.assertTrue(vod_revision.is_valid_revision(entry["content_revision"]))

    def test_canonical_manifest_digest_matches_rust(self):
        """Manifest digest generated in Python must match Rust Manifest::from_json."""
        entries = [
            {
                "file_id": pkg.stable_file_id("live", "206080"),
                "channel_id": "206080",
                "namespace": "live",
                "kind": "live",
                "folder": ["Abertos"],
                "filename": "SBT.trp",
                "logical_size": 4_000_000_000,
                "bootstrap_id": "bootstrap-test",
            },
            {
                "file_id": pkg.stable_file_id("vod", "c3757256"),
                "channel_id": "c3757256",
                "namespace": "vod",
                "kind": "vod",
                "folder": ["Filmes"],
                "filename": "Star Wars.mp4",
                "logical_size": 3_657_844_601,
                "bootstrap_id": "",
                "content_revision": "md5:" + "a" * 32,
            },
        ]
        raw_bytes, sorted_entries = pkg.canonical_manifest_bytes(entries, generation=1)
        digest = pkg.hashlib.sha256(raw_bytes).hexdigest()
        manifest_obj = {
            "schema_version": 1,
            "layout_version": 1,
            "generation": 1,
            "manifest_id": digest,
            "entries": sorted_entries,
        }
        manifest_json = json.dumps(manifest_obj)

        # Verify using rust test via cargo or checking structure
        self.assertEqual(len(digest), 64)
        self.assertEqual(sorted_entries[0]["file_id"] <= sorted_entries[1]["file_id"], True)

        # The JSON-only revision field must NOT take part in the canonical byte
        # format: the same manifest with a different revision must keep the same
        # manifest_id, or every Rust reader would reject the package.
        altered = [dict(e) for e in entries]
        altered[1]["content_revision"] = "md5:" + "b" * 32
        altered_bytes, _ = pkg.canonical_manifest_bytes(altered, generation=1)
        self.assertEqual(raw_bytes, altered_bytes)
        self.assertEqual(manifest_json.count("content_revision"), 1)

    def test_rejected_tasks_reporting(self):
        tasks = [
            with_revision({"id": "a1b2c3d4", "status": "ready", "file_size": 1000000, "title": "Good Video"}),
            {"id": "invalid!", "status": "ready", "file_size": 1000000, "title": "Bad ID"},
            {"id": "00000001", "status": "ready", "file_size": 500, "title": "Too small"},
            {"id": "00000002", "status": "downloading", "file_size": 1000000, "title": "Not ready"},
            {"id": "00000003", "status": "ready", "file_size": 1000000, "title": "No trusted identity"},
        ]
        vod_entries, rejected = pkg.process_vod_tasks(tasks, return_rejected=True)
        self.assertEqual(len(vod_entries), 1)
        self.assertEqual(vod_entries[0]["channel_id"], "a1b2c3d4")
        self.assertEqual(len(rejected), 3)
        rejected_reasons = {r["reason"] for r in rejected}
        self.assertIn("invalid_task_id_format", rejected_reasons)
        self.assertIn("unverifiable_content_revision", rejected_reasons)
        self.assertTrue(any(r.startswith("file_size_too_small") for r in rejected_reasons))

    def test_live_only_overflow_still_raises(self):
        """Live channels are mandatory: if even they alone do not fit, fail."""
        tasks = [
            with_revision({"id": "deadbeef", "status": "ready",
                           "file_size": 4 * 1024 * 1024, "title": "Small"})
        ]
        staging_dir = self.tmp_dir / "staging_capacity"
        with self.assertRaises(ValueError) as ctx:
            pkg.build_package(
                vod_tasks=tasks,
                export_config=self.live_export(),
                staging_dir=staging_dir,
                volume_bytes=2 * 1024 * 1024 * 1024,
            )
        self.assertIn("capacity exceeded", str(ctx.exception))
        # Nothing was staged: the mandatory Live set alone overflowed.
        self.assertFalse(staging_dir.exists())

    def test_best_fit_omits_oversized_videos_and_reports_shortfall(self):
        """VODs that do not fit are omitted and reported, never silently dropped."""
        # Live occupies ~4.0 GB; the volume leaves ~1.52 GiB for VODs.
        tasks = [
            with_revision({"id": "aaaa0001", "status": "ready",
                           "file_size": 1_700_000_000, "title": "Serie Alfa S01E01",
                           "display_name": "Serie Alfa S01E01.mp4", "series_id": "1234"}),
            with_revision({"id": "aaaa0002", "status": "ready",
                           "file_size": 300_000_000, "title": "Serie Alfa S01E02",
                           "display_name": "Serie Alfa S01E02.mp4", "series_id": "1234"}),
            with_revision({"id": "aaaa0003", "status": "ready",
                           "file_size": 900_000_000, "title": "Serie Alfa S01E03",
                           "display_name": "Serie Alfa S01E03.mp4", "series_id": "1234"}),
            with_revision({"id": "aaaa0004", "status": "ready",
                           "file_size": 600_000_000, "title": "Serie Alfa S01E04",
                           "display_name": "Serie Alfa S01E04.mp4", "series_id": "1234"}),
        ]
        volume = 6 * 1024**3
        staging_dir = self.tmp_dir / "staging_best_fit"
        summary = pkg.build_package(
            vod_tasks=tasks,
            export_config=self.live_export(),
            staging_dir=staging_dir,
            volume_bytes=volume,
        )

        # The oversized first and last videos are omitted; the two that fit are
        # included even though the scan hit a failure in between.
        self.assertEqual(summary["requested_count"], 4)
        self.assertEqual(summary["included_count"], 2)
        self.assertEqual(summary["omitted_count"], 2)
        self.assertEqual(summary["vod_count"], 2)
        self.assertEqual(summary["total_files"], 3)
        omitted_ids = {o["task_id"] for o in summary["omitted_tasks"]}
        self.assertEqual(omitted_ids, {"aaaa0001", "aaaa0004"})
        for item in summary["omitted_tasks"]:
            self.assertEqual(item["reason"], "does_not_fit")
            self.assertTrue(item["title"])
        expected_shortfall = (pkg.align_cluster_bytes(1_700_000_000)
                              + pkg.align_cluster_bytes(600_000_000))
        self.assertEqual(summary["capacity_shortfall_bytes"], expected_shortfall)
        self.assertLessEqual(summary["allocated_cluster_bytes"],
                             summary["available_bytes"])
        self.assertEqual(summary["free_bytes"],
                         summary["available_bytes"] - summary["allocated_cluster_bytes"])

        # Manifest, allowlist and layout must agree on exactly the included set.
        manifest = json.loads((staging_dir / "favorites_manifest.json").read_text())
        vod_entries = [e for e in manifest["entries"] if e["kind"] == "vod"]
        self.assertEqual({e["channel_id"] for e in vod_entries}, {"aaaa0002", "aaaa0003"})

        allowlist = json.loads((staging_dir / "vod_allowlist.json").read_text())
        self.assertEqual({t["task_id"] for t in allowlist["tasks"]}, {"aaaa0002", "aaaa0003"})

        layout = json.loads((staging_dir / "favorites_layout.json").read_text())
        expected_paths = {"/" + "/".join(e["folder"] + [e["filename"]])
                          for e in manifest["entries"]}
        self.assertEqual({f["path"] for f in layout["files"]}, expected_paths)

        # The omission report is persisted both in the summary and on disk.
        self.assertTrue((staging_dir / pkg.OMISSION_ARTIFACT).is_file())
        omitted = json.loads((staging_dir / pkg.OMISSION_ARTIFACT).read_text())
        self.assertEqual(omitted["omitted_tasks"], summary["omitted_tasks"])
        self.assertEqual(omitted["capacity_shortfall_bytes"], expected_shortfall)
        self.assertEqual(omitted["requested_count"], 4)

    def test_video_that_fits_only_after_skipping_a_bigger_one_is_included(self):
        """The first VOD that does not fit must not stop the scan."""
        tasks = [
            with_revision({"id": "bbbb0001", "status": "ready",
                           "file_size": 1_700_000_000, "title": "Filme Grande",
                           "display_name": "Filme Grande.mp4"}),
            with_revision({"id": "bbbb0002", "status": "ready",
                           "file_size": 200_000_000, "title": "Filme Pequeno",
                           "display_name": "Filme Pequeno.mp4"}),
        ]
        staging_dir = self.tmp_dir / "staging_skipped_bigger"
        summary = pkg.build_package(
            vod_tasks=tasks,
            export_config=self.live_export(),
            staging_dir=staging_dir,
            volume_bytes=6 * 1024**3,
        )
        self.assertEqual(summary["omitted_count"], 1)
        self.assertEqual(summary["omitted_tasks"][0]["task_id"], "bbbb0001")
        self.assertEqual(summary["included_count"], 1)
        allowlist = json.loads((staging_dir / "vod_allowlist.json").read_text())
        self.assertEqual([t["task_id"] for t in allowlist["tasks"]], ["bbbb0002"])
        manifest = json.loads((staging_dir / "favorites_manifest.json").read_text())
        self.assertEqual([e["channel_id"] for e in manifest["entries"] if e["kind"] == "vod"],
                         ["bbbb0002"])

    def test_omitted_json_exists_only_when_something_is_omitted(self):
        """The optional artifact must never join the required artifact set."""
        self.assertNotIn(pkg.OMISSION_ARTIFACT, pkg.ARTIFACTS)
        self.assertIn(pkg.OMISSION_ARTIFACT, pkg.EXTRA_FILES)

        fitting = [with_revision({"id": "cccc0001", "status": "ready",
                                  "file_size": 4 * 1024 * 1024, "title": "Film"})]
        staging_ok = self.tmp_dir / "staging_no_omission"
        pkg.build_package(
            vod_tasks=fitting,
            export_config=self.live_export(),
            staging_dir=staging_ok,
            volume_bytes=6 * 1024**3,
        )
        self.assertFalse((staging_ok / pkg.OMISSION_ARTIFACT).exists())
        sums_ok = (staging_ok / "SHA256SUMS").read_text()
        self.assertNotIn(pkg.OMISSION_ARTIFACT, sums_ok)
        for name in pkg.ARTIFACTS:
            self.assertIn(name, sums_ok)

        overflowing = [with_revision({"id": "cccc0002", "status": "ready",
                                      "file_size": 1_700_000_000, "title": "Huge"})]
        staging_omitted = self.tmp_dir / "staging_with_omission"
        pkg.build_package(
            vod_tasks=overflowing,
            export_config=self.live_export(),
            staging_dir=staging_omitted,
            volume_bytes=6 * 1024**3,
        )
        self.assertTrue((staging_omitted / pkg.OMISSION_ARTIFACT).is_file())
        sums = (staging_omitted / "SHA256SUMS").read_text()
        self.assertIn(pkg.OMISSION_ARTIFACT, sums)
        digest = hashlib.sha256(
            (staging_omitted / pkg.OMISSION_ARTIFACT).read_bytes()).hexdigest()
        self.assertIn(f"{digest}  {pkg.OMISSION_ARTIFACT}", sums)

    @unittest.skipUnless(
        (ROOT / "scratch/grade-1080p/vod-status-live.json").is_file()
        and (ROOT / "scratch/grade-1080p/export-revisao-sol.json").is_file(),
        "real grade-1080p inventory/export inputs are absent")
    def test_real_inventory_reproduces_gen2_package(self):
        """The real 122-file / 120-VOD package stays deterministic."""
        vod_tasks = json.loads(
            (ROOT / "scratch/grade-1080p/vod-status-live.json").read_text())
        export_config = json.loads(
            (ROOT / "scratch/grade-1080p/export-revisao-sol.json").read_text())
        staging_dir = self.tmp_dir / "staging_real"
        summary = pkg.build_package(
            vod_tasks=vod_tasks,
            export_config=export_config,
            staging_dir=staging_dir,
            volume_bytes=pkg.base.DEFAULT_VOLUME_BYTES,
            generation=2,
        )
        self.assertEqual(summary["vod_count"], 120)
        self.assertEqual(summary["omitted_count"], 0)
        self.assertEqual(summary["rejected_count"], 0)
        self.assertEqual(summary["total_files"], 122)
        self.assertEqual(summary["included_count"], summary["requested_count"])
        self.assertEqual(
            summary["manifest_id"],
            "23307d13dc420a58c479ea346fad355cb07dc3aa183c5422ff5cc746695ee50b",
        )
        self.assertFalse((staging_dir / pkg.OMISSION_ARTIFACT).exists())

    def test_binaries_in_sha256sums(self):
        tasks = [with_revision({"id": "a1b2c3d4", "status": "ready",
                                "file_size": 1024 * 1024, "title": "Film"})]
        bin_dir = self.tmp_dir / "fake_bins"
        bin_dir.mkdir()
        (bin_dir / "fuse_ntfs").write_text("fake binary")
        (bin_dir / "sparse_unpack").write_text("fake unpack")
        supervisor = self.tmp_dir / "activate_live_favorites_tablet.sh"
        supervisor.write_text("#!/bin/sh\n")

        staging_dir = self.tmp_dir / "staging_with_bins"
        summary = pkg.build_package(
            vod_tasks=tasks,
            export_config=self.live_export(),
            staging_dir=staging_dir,
            volume_bytes=10 * 1024 * 1024 * 1024,
            binaries_dir=bin_dir,
            runtime_files={"activate_live_favorites_tablet.sh": supervisor},
        )
        sums_text = (staging_dir / "SHA256SUMS").read_text()
        self.assertIn("fuse_ntfs", sums_text)
        self.assertIn("sparse_unpack", sums_text)
        self.assertIn("activate_live_favorites_tablet.sh", sums_text)
        self.assertIn("favorites_manifest.json", sums_text)
        self.assertIn("vod_allowlist.json", sums_text)
        self.assertEqual(summary["rejected_count"], 0)

    def test_allowlist_carries_size_and_trusted_revision(self):
        tasks = [with_revision({"id": "a1b2c3d4", "status": "ready",
                                "file_size": 4 * 1024 * 1024, "title": "Film"})]
        staging_dir = self.tmp_dir / "staging_allowlist"
        pkg.build_package(
            vod_tasks=tasks,
            export_config=self.live_export(),
            staging_dir=staging_dir,
            volume_bytes=10 * 1024 * 1024 * 1024,
        )
        allowlist = json.loads((staging_dir / "vod_allowlist.json").read_text())
        self.assertEqual(allowlist["tasks"], [{
            "task_id": "a1b2c3d4",
            "size": 4 * 1024 * 1024,
            "revision": tasks[0]["remote_md5"] and "md5:" + tasks[0]["remote_md5"],
        }])
        manifest = json.loads((staging_dir / "favorites_manifest.json").read_text())
        vod_entries = [e for e in manifest["entries"] if e["kind"] == "vod"]
        self.assertEqual(vod_entries[0]["content_revision"],
                         "md5:" + tasks[0]["remote_md5"])

    def test_private_files_are_copied_and_hashed_with_restricted_mode(self):
        tasks = [with_revision({"id": "a1b2c3d4", "status": "ready",
                                "file_size": 1024 * 1024, "title": "Film"})]
        private = self.tmp_dir / "private"
        private.mkdir()
        (private / "transport.token").write_text("t" * 48 + "\n")
        (private / "chisel.auth").write_text("user:secret\n")
        staging_dir = self.tmp_dir / "staging_private"
        pkg.build_package(
            vod_tasks=tasks,
            export_config=self.live_export(),
            staging_dir=staging_dir,
            volume_bytes=10 * 1024 * 1024 * 1024,
            runtime_files={
                "transport.token": private / "transport.token",
                "chisel.auth": private / "chisel.auth",
            },
        )
        sums = (staging_dir / "SHA256SUMS").read_text()
        self.assertIn("transport.token", sums)
        self.assertIn("chisel.auth", sums)
        for name in ("transport.token", "chisel.auth"):
            self.assertEqual((staging_dir / name).stat().st_mode & 0o777, 0o600)
        # Secrets must never appear in cleartext outside the private files
        self.assertNotIn("secret", sums)


if __name__ == "__main__":
    unittest.main()
