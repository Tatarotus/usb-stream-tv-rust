"""Export configuration: 19 Live favorites -> 16 identities, fail-closed."""

import copy
import json
import re
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_live_favorites_export as exporter  # noqa: E402

PLAN = json.loads((ROOT / "config" / "live_favorites_candidates.json").read_text())
REFERENCE_FAVORITES = [
    "206080", "206087", "206098", "206111", "207171", "207172", "207177",
    "207198", "208423", "208430", "214640", "365244", "387624", "387640",
    "387648", "1765055", "3030726", "10006", "10007",
]


def catalog_from_plan(plan):
    rows = {}
    for identity in plan["identities"]:
        for candidate in identity["candidates"]:
            rows[candidate["channel_id"]] = {
                "stream_id": int(candidate["channel_id"]),
                "name": candidate["catalog_name"],
                "stream_type": "live",
            }
    return rows


def evidence_record(channel_id, phase="winner", duration=300, width=1920,
                    height=1080, verdict="candidate_ok"):
    return {
        "identity": "fixture",
        "phase": phase,
        "duration_s": duration,
        "channel_id": channel_id,
        "verdict": verdict,
        "media": {
            "width": width,
            "height": height,
            "video_codec": "h264",
            "profile": "High",
            "video_kbps": 2000,
            "ts_kbps_total": 2500,
            "audio_codec": "aac",
            "audio": {"audio_pts_gaps_over_500ms": 0, "audio_pts_gaps_over_2000ms": 0,
                      "audio_pts_max_gap_ms": 100.0},
            "pcr": {"pcr_max_gap_ms": 133.0},
            "widths_seen": [width],
            "heights_seen": [height],
            "media_duration_s": duration,
            "capture_coverage": 1.0,
        },
    }


def output_record(identity, channel_id):
    return {
        "identity": identity, "channel_id": channel_id,
        "verdict": "output_ok", "output_profile": "1080p30", "failed": [],
        "source_capture": {"returncode": 0, "bytes": 100000},
        "output_capture": {"returncode": 0, "bytes": 100000, "raw_bytes": 100000,
                           "profile": "1080p30", "artifact_sha256": "a" * 64,
                           "stderr_categories": {}, "bounder": {"error": None}},
        "output": {"width": 1920, "height": 1080, "widths_seen": [1920],
                   "heights_seen": [1080], "b_frames_seen": 0,
                   "idr_gap_median_s": 1.0, "idr_gap_max_s": 1.0,
                   "slices": {"slices_max": 1}, "pcr": {"pcr_max_gap_ms": 19.0},
                   "audio_codec": "ac3", "audio_sample_rate": "48000",
                   "audio_channels": 2, "audio_bit_rate": "384000",
                   "audio": {"audio_pts_gaps_over_500ms": 0}},
    }


def decode_record(identity, channel_id):
    return {
        "identity": identity, "channel_id": channel_id, "verdict": "decode_ok",
        "duration_s": 60, "capture": {"returncode": 0, "bytes": 100000},
        "media": {"video_span_s": 60, "video_packet_span_s": 60,
                  "video_packet_covered_s": 60, "video_packets": 1800,
                  "video_packets_timestamped": 1800, "video_decoded_frames": 1800,
                  "audio_duration_s": 60},
        "decode": {"returncode": 0, "lines": 0},
        "validation": {"checks": {name: True for name in (
            "valid_duration", "capture_ok", "capture_has_media", "video_coverage", "video_timestamps_valid",
            "audio_coverage", "video_frames", "decode_process_ok", "decode_clean")}},
    }


def write_evidence(directory, rows):
    for index, record in enumerate(rows):
        (Path(directory) / f"record-{index}.json").write_text(json.dumps(record))


def build(plan=None, favorites=None, evidence_rows=None, generation=2, outputs=None,
          decodes=None):
    plan = plan or PLAN
    catalog = {cid: row for cid, row in catalog_from_plan(plan).items()}
    for row in catalog.values():
        row["stream_id"] = str(row["stream_id"])
    rows = evidence_rows
    if rows is None:
        rows = [evidence_record(candidate["channel_id"])
                for identity in plan["identities"]
                for candidate in identity["candidates"]]
    with tempfile.TemporaryDirectory() as directory:
        for index, record in enumerate(rows):
            (Path(directory) / f"record-{index}.json").write_text(json.dumps(record))
        evidence = exporter.load_evidence(Path(directory))
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "favorites.json"
        path.write_text(json.dumps(favorites or REFERENCE_FAVORITES))
        saved = exporter.load_favorites(path)
    return exporter.build_export(plan, catalog, saved, evidence, generation, outputs,
                                 decodes)


class ExportFixtureTests(unittest.TestCase):
    def test_globo_family_accepts_only_explicit_rj_and_sp_affiliates(self):
        family = re.compile(exporter.FAMILY_PATTERNS["globo"], re.I)
        for name in ("GLOBO RIO DE JANEIRO (RJ) FHD¹", "GLOBO RIO SUL (RJ) FHD¹",
                     "GLOBO RJ ALTO LITORAL (RJ) FHD²", "GLOBO RJ PLANICE (RJ) FHD¹",
                     "GLOBO SAO PAULO (SP) FHD²", "GLOBO SÃO PAULO (SP) FHD¹"):
            with self.subTest(name=name):
                self.assertIsNotNone(family.search(name))
        for name in ("GLOBO NEWS FHD¹", "GLOBO RIO DE JANEIRO NEWS (RJ) FHD¹",
                     "GLOBO MINAS (MG) FHD¹", "GLOBO SAO PAULO (RJ) FHD¹",
                     "GLOBO RJ ALTO LITORAL (SP) FHD²"):
            with self.subTest(name=name):
                self.assertIsNone(family.search(name))

    def test_globo_sp_and_rj_affiliate_fallbacks_require_full_evidence(self):
        plan = copy.deepcopy(PLAN)
        globo = next(item for item in plan["identities"] if item["identity"] == "globo")
        globo["candidates"].extend([
            {"channel_id": "10012", "catalog_name": "GLOBO RIO SUL (RJ) FHD¹",
             "region": "rj-interior", "quality_label": "FHD1"},
            {"channel_id": "10001", "catalog_name": "GLOBO SAO PAULO (SP) FHD²",
             "region": "sp", "quality_label": "FHD2"},
        ])
        rows = [evidence_record(c["channel_id"]) for i in plan["identities"]
                for c in i["candidates"]]
        for row in rows:
            row["windows"] = 2
            row["window2"] = {"coverage": 1.0, "audio_gaps_over_2s": 0,
                              "height": 1080, "width": 1920, "duration_s": 300,
                              "verdict": "candidate_ok", "media_duration_s": 300}
        ids = ("10006", "10012", "10001")
        outputs = {("globo", cid): output_record("globo", cid) for cid in ids}
        decodes = {("globo", cid): decode_record("globo", cid) for cid in ids}

        def chosen():
            config = build(plan=plan, evidence_rows=rows, outputs=outputs, decodes=decodes)
            return next(item for item in config["identities"] if item["identity"] == "globo")

        self.assertEqual(chosen()["chosen"]["channel_id"], "10006")
        decodes[("globo", "10006")]["decode"]["lines"] = 1
        affiliate = chosen()
        self.assertEqual(affiliate["chosen"]["channel_id"], "10012")
        self.assertEqual(affiliate["chosen"]["region"], "rj-interior")
        self.assertEqual(affiliate["status"], "approved")
        decodes[("globo", "10012")]["decode"]["lines"] = 1
        fallback = chosen()
        self.assertEqual(fallback["chosen"]["channel_id"], "10001")
        self.assertEqual(fallback["chosen"]["region"], "sp")
        self.assertEqual(fallback["status"], "approved")
        sp = next(row for row in rows if row["channel_id"] == "10001")
        sp.pop("window2")
        sp["windows"] = 1
        self.assertNotEqual(chosen()["status"], "approved")

    def test_fully_validated_alternative_beats_preferred_failed_source(self):
        plan = copy.deepcopy(PLAN)
        rows = [evidence_record(candidate["channel_id"])
                for item in plan["identities"] for candidate in item["candidates"]]
        for record in rows:
            record["windows"] = 2
            record["window2"] = {"coverage": 1.0, "audio_gaps_over_2s": 0,
                                 "height": 1080, "width": 1920, "duration_s": 300,
                                 "verdict": "candidate_ok", "media_duration_s": 300}
        sbt = next(item for item in plan["identities"] if item["identity"] == "sbt")
        rj = next(c for c in sbt["candidates"] if c["channel_id"] == "207198")
        sp = next(c for c in sbt["candidates"] if c["channel_id"] == "207192")
        outputs = {("sbt", c["channel_id"]): output_record("sbt", c["channel_id"])
                   for c in (rj, sp)}
        decodes = {("sbt", c["channel_id"]): decode_record("sbt", c["channel_id"])
                   for c in (rj, sp)}
        decodes[("sbt", "207198")]["decode"]["lines"] = 1
        chosen = next(item for item in build(plan=plan, evidence_rows=rows,
                                             outputs=outputs, decodes=decodes)["identities"]
                      if item["identity"] == "sbt")
        self.assertEqual(chosen["chosen"]["channel_id"], "207192")
        self.assertEqual(chosen["status"], "approved")

        # Once RJ passes too, regional preference is retained.
        decodes[("sbt", "207198")] = decode_record("sbt", "207198")
        chosen = next(item for item in build(plan=plan, evidence_rows=rows,
                                             outputs=outputs, decodes=decodes)["identities"]
                      if item["identity"] == "sbt")
        self.assertEqual(chosen["chosen"]["channel_id"], "207198")
        self.assertEqual(chosen["status"], "approved")

        # Gathering probes without both validation sets keeps the old order.
        decodes[("sbt", "207198")]["decode"]["lines"] = 1
        provisional = next(item for item in build(plan=plan, evidence_rows=rows,
                                                  outputs=None, decodes=decodes)["identities"]
                           if item["identity"] == "sbt")
        self.assertEqual(provisional["chosen"]["channel_id"], "207198")
        self.assertEqual(provisional["status"], "pending")

        # An output failure also allows the validated alternative to win.
        decodes[("sbt", "207198")] = decode_record("sbt", "207198")
        outputs[("sbt", "207198")]["output"]["pcr"]["pcr_max_gap_ms"] = 49
        chosen = next(item for item in build(plan=plan, evidence_rows=rows,
                                             outputs=outputs, decodes=decodes)["identities"]
                      if item["identity"] == "sbt")
        self.assertEqual(chosen["chosen"]["channel_id"], "207192")
        self.assertEqual(chosen["status"], "approved")

    def test_reference_snapshot_maps_nineteen_favorites_to_sixteen_identities(self):
        config = build()
        self.assertEqual(config["snapshot"]["live_favorites"], 19)
        self.assertEqual(config["snapshot"]["identities"], 16)
        self.assertEqual(config["snapshot"]["approved"], 16)
        self.assertEqual(config["snapshot"]["pending"], 0)
        exported = sorted(favorite for identity in config["identities"]
                          for favorite in identity["favorite_ids"])
        self.assertEqual(exported, sorted(REFERENCE_FAVORITES))
        names = [entry["filename"] for entry in config["identities"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("SBT.trp", names)
        self.assertIn("SBT News.trp", names)
        self.assertIn("Record.trp", names)
        self.assertIn("Record News.trp", names)
        self.assertNotIn("Favoritos", json.dumps(config))

    def test_order_of_personal_favorites_does_not_change_the_result(self):
        first = build()
        second = build(favorites=list(reversed(REFERENCE_FAVORITES)))
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_better_substitute_outside_personal_favorites_is_allowed(self):
        plan = copy.deepcopy(PLAN)
        identity = next(item for item in plan["identities"] if item["identity"] == "sbt")
        identity["candidates"].insert(0, {
            "channel_id": "207194", "catalog_name": "SBT SP HD¹",
            "region": "sp", "quality_label": "HD1",
        })
        rows = [evidence_record(candidate["channel_id"])
                for item in plan["identities"] for candidate in item["candidates"]]
        config = build(plan=plan, evidence_rows=rows)
        sbt = next(item for item in config["identities"] if item["identity"] == "sbt")
        self.assertEqual(sbt["chosen"]["channel_id"], "207198")
        # RJ (approved 1080p) wins over SP even though both are tested.
        self.assertEqual(sbt["chosen"]["region"], "rj")

    def test_missing_and_vod_candidates_are_rejected_explicitly(self):
        plan = copy.deepcopy(PLAN)
        identity = next(item for item in plan["identities"] if item["identity"] == "cnn-brasil")
        identity["candidates"].append({"channel_id": "999", "catalog_name": "VOD",
                                       "region": "national", "quality_label": "HD1"})
        catalog = {cid: row for cid, row in catalog_from_plan(plan).items()}
        catalog["999"] = {"stream_id": "999", "name": "CNN BRASIL MOVIE", "stream_type": "movie"}
        rows = [evidence_record(candidate["channel_id"])
                for item in plan["identities"] for candidate in item["candidates"]]
        with tempfile.TemporaryDirectory() as directory:
            for index, record in enumerate(rows):
                (Path(directory) / f"record-{index}.json").write_text(json.dumps(record))
            evidence = exporter.load_evidence(Path(directory))
        config = exporter.build_export(plan, catalog, REFERENCE_FAVORITES, evidence, 2)
        cnn = next(item for item in config["identities"] if item["identity"] == "cnn-brasil")
        reasons = {item["channel_id"]: item["reason"] for item in cnn["rejected"]}
        self.assertEqual(reasons.get("999"), "not_live")

    def test_wrong_family_candidate_is_rejected(self):
        plan = copy.deepcopy(PLAN)
        identity = next(item for item in plan["identities"] if item["identity"] == "sbt")
        identity["candidates"].append({"channel_id": "3030726", "catalog_name": "SBT NEWS FHD¹",
                                       "region": "national", "quality_label": "FHD1"})
        rows = [evidence_record(candidate["channel_id"])
                for item in plan["identities"] for candidate in item["candidates"]]
        config = build(plan=plan, evidence_rows=rows)
        sbt = next(item for item in config["identities"] if item["identity"] == "sbt")
        self.assertNotIn("3030726", [item["channel_id"] for item in sbt["candidates_tested"]])

    def test_traversal_filename_is_rejected(self):
        plan = copy.deepcopy(PLAN)
        plan["identities"][0]["filename"] = "../../SBT.trp"
        with self.assertRaises(exporter.ExportError):
            build(plan=plan)

    def test_unapproved_candidate_cannot_be_chosen(self):
        plan = copy.deepcopy(PLAN)
        rows = [record for record in
                (evidence_record(candidate["channel_id"])
                 for identity in plan["identities"] for candidate in identity["candidates"])
                if record["channel_id"] != "207198"]
        config = build(plan=plan, evidence_rows=rows)
        sbt = next(item for item in config["identities"] if item["identity"] == "sbt")
        self.assertNotEqual(sbt["chosen"]["channel_id"], "207198")

    def test_no_native_1080_is_reported_pending(self):
        plan = copy.deepcopy(PLAN)
        sbt_ids = {candidate["channel_id"] for identity in plan["identities"]
                   if identity["identity"] == "sbt" for candidate in identity["candidates"]}
        rows = []
        for identity in plan["identities"]:
            for candidate in identity["candidates"]:
                if candidate["channel_id"] in sbt_ids:
                    rows.append(evidence_record(candidate["channel_id"], width=1280,
                                                height=720, verdict="not_1080p"))
                else:
                    rows.append(evidence_record(candidate["channel_id"]))
        config = build(plan=plan, evidence_rows=rows)
        self.assertEqual(config["snapshot"]["approved"], 15)
        self.assertEqual(config["snapshot"]["native_1080"], 15)
        self.assertEqual(config["snapshot"]["pending_1080"], 1)
        self.assertEqual(config["snapshot"]["unavailable"], 0)
        self.assertEqual(config["snapshot"]["pending"], 1)
        pending = next(item for item in config["identities"]
                       if item["quality_status"] != "native_1080")
        self.assertEqual(pending["identity"], "sbt")
        self.assertEqual(pending["status"], "pending")
        self.assertFalse(pending["approved"])
        self.assertEqual(len(config["pending"]), 1)
        self.assertEqual(config["pending"][0]["best_native"], "1280x720")

    def test_probe_error_is_pending_measurement_never_approved(self):
        plan = copy.deepcopy(PLAN)
        sbt_ids = {candidate["channel_id"] for identity in plan["identities"]
                   if identity["identity"] == "sbt" for candidate in identity["candidates"]}
        rows = []
        for identity in plan["identities"]:
            for candidate in identity["candidates"]:
                if candidate["channel_id"] in sbt_ids:
                    rows.append(evidence_record(candidate["channel_id"]))
                else:
                    rows.append({
                        "identity": identity["identity"], "phase": "winner",
                        "duration_s": 300, "channel_id": candidate["channel_id"],
                        "verdict": "probe_error", "analysis_error": "TimeoutExpired",
                        "capture": {"bytes": 1_000_000, "returncode": 0,
                                    "stderr_categories": {}},
                    })
        config = build(plan=plan, evidence_rows=rows)
        self.assertEqual(config["snapshot"]["approved"], 1)
        self.assertEqual(config["snapshot"]["pending_measurement"],
                         len(plan["identities"]) - 1)
        self.assertEqual(config["snapshot"]["native_1080"], config["snapshot"]["approved"])
        for item in config["identities"]:
            if item["identity"] != "sbt":
                self.assertEqual(item["status"], "pending_measurement")
                self.assertFalse(item["approved"])
        self.assertTrue(all(entry["reason"] == "probe_error" for entry in config["pending"]
                            if entry["status"] == "pending_measurement"))

    def test_output_validation_gates_approval(self):
        plan = copy.deepcopy(PLAN)
        rows = [evidence_record(candidate["channel_id"])
                for identity in plan["identities"] for candidate in identity["candidates"]]
        for record in rows:
            record["windows"] = 2
            record["window2"] = {"coverage": 1.0, "audio_gaps_over_2s": 0,
                                 "height": 1080, "width": 1920,
                                 "duration_s": 300, "verdict": "candidate_ok",
                                 "media_duration_s": 300}
        outputs = {}
        for identity in plan["identities"]:
            for candidate in identity["candidates"]:
                ok = identity["identity"] == "sbt"
                result = output_record(identity["identity"], candidate["channel_id"])
                if not ok:
                    result["verdict"] = "output_fail"
                    result["failed"] = ["audio_continuous"]
                outputs[(identity["identity"], candidate["channel_id"])] = result
        decodes = {}
        for identity in plan["identities"]:
            for candidate in identity["candidates"]:
                ok = identity["identity"] == "sbt"
                decodes[(identity["identity"], candidate["channel_id"])] = {
                    "identity": identity["identity"],
                    "channel_id": candidate["channel_id"],
                    "verdict": "decode_ok",
                    "duration_s": 60,
                    "capture": {"returncode": 0, "bytes": 100000},
                    "media": {"video_span_s": 60, "video_packet_span_s": 60,
                              "video_packet_covered_s": 60, "video_packets": 1800,
                              "video_packets_timestamped": 1800, "video_decoded_frames": 1800,
                              "audio_duration_s": 60},
                    "decode": {"returncode": 0, "lines": 0},
                    "validation": {"checks": {name: True for name in (
                        "valid_duration", "capture_ok", "capture_has_media", "video_coverage", "video_timestamps_valid",
                        "audio_coverage", "video_frames", "decode_process_ok", "decode_clean")}},
                }
        config = build(plan=plan, evidence_rows=rows, outputs=outputs, decodes=decodes)
        self.assertFalse(config["provisional"])
        self.assertEqual(config["snapshot"]["approved"], 1)
        self.assertEqual(config["snapshot"]["pending"], len(plan["identities"]) - 1)
        self.assertEqual(config["snapshot"]["output_validated"], 1)
        pending = [item for item in config["identities"] if item["status"] == "pending"]
        self.assertTrue(all(item["quality_status"] == "pending_output_validation"
                            for item in pending))
        self.assertEqual(pending[0]["chosen"]["output_failed"], ["audio_continuous"])
        bad_outputs = copy.deepcopy(outputs)
        for (identity, _), result in bad_outputs.items():
            if identity == "sbt":
                result["output"]["pcr"]["pcr_max_gap_ms"] = 49
        gated = build(plan=plan, evidence_rows=rows, outputs=bad_outputs, decodes=decodes)
        self.assertEqual(gated["snapshot"]["approved"], 0)
        catalog = catalog_from_plan(plan)
        exporter.validate_config(config, {cid: {"stream_id": cid, "name": row["name"],
                                               "stream_type": "live"}
                                         for cid, row in catalog.items()},
                                 REFERENCE_FAVORITES)
        selected = next(item for item in config["identities"] if item["status"] == "approved")
        for mutation in ("window", "decode", "old_decode", "coverage", "output"):
            with self.subTest(mutation=mutation):
                tampered = copy.deepcopy(config)
                chosen = next(item["chosen"] for item in tampered["identities"]
                              if item["identity"] == selected["identity"])
                if mutation == "window":
                    chosen["source_evidence"]["window2"] = None
                elif mutation == "decode":
                    chosen["decode_verdict"] = None
                elif mutation == "old_decode":
                    chosen["decode_evidence"].pop("validation")
                elif mutation == "output":
                    chosen["output_evidence"]["output"]["pcr"]["pcr_max_gap_ms"] = 49
                else:
                    chosen["source_evidence"]["media"]["capture_coverage"] = 0.5
                with self.assertRaises(exporter.ExportError):
                    exporter.validate_config(tampered, catalog, REFERENCE_FAVORITES)

    def test_validate_config_rejects_omitted_favorite(self):
        config = build()
        catalog = catalog_from_plan(PLAN)
        tampered = copy.deepcopy(config)
        tampered["identities"] = [item for item in tampered["identities"]
                                   if item["identity"] != "globo"]
        with self.assertRaisesRegex(exporter.ExportError, "omits Live favorites"):
            exporter.validate_config(tampered, catalog, REFERENCE_FAVORITES)

    def test_plan_missing_a_live_favorite_identity_is_rejected(self):
        plan = copy.deepcopy(PLAN)
        plan["identities"] = [item for item in plan["identities"]
                              if item["identity"] != "globo"]
        catalog = catalog_from_plan(plan)
        for favorite in REFERENCE_FAVORITES:
            catalog.setdefault(favorite, {"stream_id": favorite, "name": "GLOBO RIO DE JANEIRO (RJ) FHD¹",
                                          "stream_type": "live"})
        for row in catalog.values():
            row["stream_id"] = str(row["stream_id"])
        with self.assertRaisesRegex(exporter.ExportError, "Live favorites not mapped"):
            exporter.validate_plan(plan, catalog, REFERENCE_FAVORITES)

    def test_duplicate_candidate_across_identities_is_rejected(self):
        plan = copy.deepcopy(PLAN)
        sbt = next(item for item in plan["identities"] if item["identity"] == "sbt")
        sbt["candidates"].append({"channel_id": "207192", "catalog_name": "SBT SP FHD¹",
                                  "region": "sp", "quality_label": "FHD1"})
        with self.assertRaisesRegex(exporter.ExportError, "duplicate candidate"):
            build(plan=plan)

    def test_invalid_second_window_never_approves(self):
        plan = copy.deepcopy(PLAN)
        rows = [evidence_record(candidate["channel_id"])
                for identity in plan["identities"] for candidate in identity["candidates"]]
        for record in rows:
            record["windows"] = 2
            record["window2"] = {"coverage": 0.003, "audio_gaps_over_2s": 0,
                                 "height": 1080, "width": 640, "duration_s": 1,
                                 "verdict": "probe_error"}
        config = build(plan=plan, evidence_rows=rows)
        self.assertEqual(config["snapshot"]["approved"], 0)
        self.assertTrue(all(item["status"] != "approved" for item in config["identities"]))
        self.assertEqual(config["snapshot"]["pending_stability"],
                         len(plan["identities"]))

    def test_decode_evidence_is_required_for_approval(self):
        config = build()
        plan = copy.deepcopy(PLAN)
        rows = [evidence_record(candidate["channel_id"])
                for identity in plan["identities"] for candidate in identity["candidates"]]
        for record in rows:
            record["windows"] = 2
            record["window2"] = {"coverage": 1.0, "audio_gaps_over_2s": 0,
                                 "height": 1080, "width": 1920, "duration_s": 300,
                                 "verdict": "candidate_ok", "media_duration_s": 300}
        outputs = {(item["identity"], candidate["channel_id"]):
                   {"identity": item["identity"], "channel_id": candidate["channel_id"],
                    "verdict": "output_ok", "failed": [], "output": {}}
                   for item in plan["identities"] for candidate in item["candidates"]}
        decodes = {(item["identity"], candidate["channel_id"]):
                   {"identity": item["identity"], "channel_id": candidate["channel_id"],
                    "verdict": "decode_errors", "decode": {"lines": 4}}
                   for item in plan["identities"] for candidate in item["candidates"]}
        gated = build(plan=plan, evidence_rows=rows, outputs=outputs, decodes=decodes)
        self.assertEqual(gated["snapshot"]["approved"], 0)
        self.assertEqual(gated["snapshot"]["pending_decode"], len(plan["identities"]))
        self.assertTrue(all(item["quality_status"] == "pending_decode_validation"
                            for item in gated["identities"]))
        self.assertTrue(config["provisional"])

    def test_duplicate_chosen_candidate_is_rejected(self):
        config = build()
        catalog = {cid: row for cid, row in catalog_from_plan(PLAN).items()}
        for row in catalog.values():
            row["stream_id"] = str(row["stream_id"])
        tampered = copy.deepcopy(config)
        tampered["identities"][1]["chosen"]["channel_id"] = \
            tampered["identities"][0]["chosen"]["channel_id"]
        with self.assertRaisesRegex(exporter.ExportError, "duplicate chosen"):
            exporter.validate_config(tampered, catalog, REFERENCE_FAVORITES)

    def test_second_window_missing_audio_never_becomes_zero_gaps(self):
        first = evidence_record("123")
        second = evidence_record("123")
        del second["media"]["audio"]
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "winner-test.json").write_text(json.dumps(second))
            records = {("123", "winner"): first}
            self.assertEqual(exporter.attach_second_window(records, Path(directory)), 1)
        self.assertIsNone(first["window2"]["audio_gaps_over_2s"])
        self.assertFalse(exporter.record_stability(first)[2])

    def test_old_decode_ok_without_checks_is_not_valid(self):
        self.assertFalse(exporter.valid_decode_evidence(
            {"verdict": "decode_ok", "decode": {"returncode": 0, "lines": 0}}))

    def test_output_ok_label_cannot_override_invalid_evidence(self):
        valid = output_record("sbt", "207198")
        self.assertTrue(exporter.valid_output_evidence(valid))
        mutations = [
            ("missing output", lambda r: r.pop("output")),
            ("wrong profile", lambda r: r.update(output_profile="720p30")),
            ("wrong capture profile", lambda r: r["output_capture"].update(profile="720p30")),
            ("source error", lambda r: r["source_capture"].update(returncode=1)),
            ("output error", lambda r: r["output_capture"].update(returncode=1)),
            ("filter error", lambda r: r["output_capture"]["bounder"].update(error="ValueError")),
            ("PCR", lambda r: r["output"]["pcr"].update(pcr_max_gap_ms=49)),
            ("audio", lambda r: r["output"].update(audio_bit_rate="128000")),
            ("missing artifact hash", lambda r: r["output_capture"].pop("artifact_sha256")),
        ]
        for name, mutate in mutations:
            with self.subTest(name=name):
                record = copy.deepcopy(valid)
                mutate(record)
                self.assertFalse(exporter.valid_output_evidence(record))

    def test_both_windows_require_300_seconds_and_coherent_coverage(self):
        first = evidence_record("123")
        first["windows"] = 2
        first["window2"] = {"coverage": 1.0, "audio_gaps_over_2s": 0,
                            "height": 1080, "width": 1920, "duration_s": 300,
                            "verdict": "candidate_ok", "media_duration_s": 300}
        self.assertTrue(exporter.record_stability(first)[2])
        cases = [
            ("first short", lambda r: r.update(duration_s=120)),
            ("second short", lambda r: r["window2"].update(duration_s=290,
                                                              media_duration_s=290)),
            ("second missing duration", lambda r: r["window2"].pop("media_duration_s")),
            ("second false coverage", lambda r: r["window2"].update(media_duration_s=1)),
            ("first false coverage", lambda r: r["media"].update(media_duration_s=1)),
        ]
        for name, mutate in cases:
            with self.subTest(name=name):
                record = copy.deepcopy(first)
                mutate(record)
                self.assertFalse(exporter.record_stability(record)[2])

    def test_short_triage_sample_cannot_approve_a_channel(self):
        plan = copy.deepcopy(PLAN)
        rows = [evidence_record(candidate["channel_id"], phase="triage", duration=20)
                for identity in plan["identities"] for candidate in identity["candidates"]]
        with self.assertRaisesRegex(exporter.ExportError, "no measured source"):
            build(plan=plan, evidence_rows=rows)

    def test_validate_config_rejects_tampered_chosen_candidate(self):
        config = build()
        catalog = {cid: row for cid, row in catalog_from_plan(PLAN).items()}
        for row in catalog.values():
            row["stream_id"] = str(row["stream_id"])
        tampered = copy.deepcopy(config)
        tampered["identities"][0]["chosen"]["channel_id"] = "999999"
        with self.assertRaises(exporter.ExportError):
            exporter.validate_config(tampered, catalog, REFERENCE_FAVORITES)


if __name__ == "__main__":
    unittest.main()
