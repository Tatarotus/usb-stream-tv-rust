#!/usr/bin/env python3
"""Build and validate the reviewed Live Favorites export configuration.

Inputs: the candidate plan (public IDs), the Live catalog snapshot, the saved
favorites, and the sanitized probe evidence. Output: one deterministic export
configuration consumed by the NTFS template builder, the manifest builder and
the operational manager. No URL, credential or provider text is written.

Rules enforced here (fail closed):
* only Live catalog rows; VOD/series are rejected;
* each candidate must belong to the declared channel family;
* the chosen candidate must appear in the tested list and have a qualified
  probe record (``final`` or ``winner`` >= 300 s);
* ``status=approved`` means a qualified operational source exists; the quality
  field records ``native_1080`` or ``pending_no_native_1080`` (upscaled best
  alternative, explicit pending decision for the reviewer);
* identities, NTFS names and root categories are unique and sanitized.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from probe_live_favorites_decode import decode_verdict
from probe_live_favorites_output import output_verdict
from favorites_source_manager import PROFILES

ROOT = Path(__file__).resolve().parents[1]
MAX_IDENTITIES = 32
CATEGORIES = {"Abertos", "Noticias", "Filmes", "Documentarios", "Variedades", "Outros"}
OUTPUT_PROFILE = "1080p30"
REGION_RANK = {"rj": 0, "rj-interior": 1, "national": 2, "sp": 3}
APPROVABLE_REGIONS = {"rj", "rj-interior", "national", "sp"}
QUALITY_RANK = {"FHD1": 0, "FHD2": 1, "HD1": 2, "HD2": 3, "H264": 4, "SD1": 5}
MIN_FINAL_SECONDS = 300
MIN_WINNER_SECONDS = 300

FAMILY_PATTERNS = {
    "band-news": r"^BAND NEWS\b",
    "cnn-brasil": r"^CNN BRASIL\b(?! MONEY)",
    "globo-news": r"^GLOBO NEWS\b",
    "record-news": r"^RECORD NEWS\b",
    "band": r"^BAND (RJ|SP|INTERIOR RJ)\b",
    "band-sports": r"^BAND SPORTS\b",
    "sbt": r"^SBT (RJ|SP)\b",
    "redetv": r"^REDE ?TV\b",
    "tv-cultura": r"^TV CULTURA\b",
    "record": r"^RECORD (TV RIO|RJ|SP)\b",
    "telecine-action": r"^TELECINE ACTION\b",
    "history": r"^HISTORY\b",
    "discovery-channel": r"^DISCOVERY CHANNEL\b",
    "discovery-hh": r"^DISCOVERY H&H\b",
    "bbc-world": r"^BBC WORLD\b",
    "sbt-news": r"^SBT NEWS\b",
    # Canais medidos pelo host em 30/10 (fila de diversidade: um canal por marca).
    # O padrao e a unica coisa que autoriza a identidade a existir no export; a
    # aprovacao continua dependendo de 2 janelas de 300 s + decode + saida.
    "telecine-fhd2": r"^TELECINE ACTION\b",
    # Identidades genericas por marca, usadas pela fila de diversidade: cada
    # familia entrega UM canal para a grade, e o nome do arquivo diz qual variante.
    "telecine": r"^TELECINE\b",
    "discovery": r"^DISCOVERY\b",
    "off": r"^OFF\b",
    "gnt": r"^GNT\b",
    # Explicit Globo broadcast affiliates only: never fold Globo News or an
    # unrelated Globo channel into the over-the-air station identity.
    "globo": (r"^GLOBO (?:(?:RIO DE JANEIRO|RIO SUL|RJ ALTO LITORAL|"
              r"RJ PLANICE|RJ PLAN[IÍ]CIE)\s*\(RJ\)|"
              r"(?:SAO PAULO|SÃO PAULO)\s*\(SP\))(?=\s|$)"),
}


class ExportError(ValueError):
    pass


def load_json(path: Path):
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ExportError(f"{path.name} exceeds size limit")
    return json.loads(path.read_text())


def load_catalog(path: Path) -> dict:
    rows = load_json(path)
    if not isinstance(rows, list):
        raise ExportError("Live catalog must be a list")
    catalog = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        channel_id = str(row.get("stream_id", ""))
        if not re.fullmatch(r"[1-9][0-9]{0,19}", channel_id):
            continue
        if channel_id in catalog:
            raise ExportError("duplicate Live catalog identity")
        catalog[channel_id] = row
    return catalog


def load_favorites(path: Path) -> list[str]:
    values = load_json(path)
    if not isinstance(values, list):
        raise ExportError("favorites must be a list")
    return [str(value) for value in values]


def load_evidence(directory: Path) -> dict:
    records = {}
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        key = (str(record.get("channel_id")), record.get("phase"))
        records[key] = record
    return records


def attach_second_window(records: dict, directory: Path) -> int:
    """Attach an independent second window to the primary measurement.

    Astra's gate requires two independent windows per approved source. The
    second sample stays a separate file; only its continuity summary is added
    to the primary record so selection and reporting can require both.
    """
    attached = 0
    for path in sorted(directory.glob("winner-*.json")):
        try:
            record = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        key = (str(record.get("channel_id")), "winner")
        primary = records.get(key)
        if primary is None:
            continue
        media = record.get("media") or {}
        duration = record.get("duration_s") or 0
        coverage = media.get("capture_coverage")
        if coverage is None and duration and media.get("media_duration_s"):
            coverage = media["media_duration_s"] / duration
        audio = media.get("audio") or {}
        gaps = audio.get("audio_pts_gaps_over_2000ms")
        if gaps is None:
            max_gap = audio.get("audio_pts_max_gap_ms")
            gaps = None if max_gap is None else (1 if max_gap > 2000 else 0)
        primary["windows"] = 2
        primary["window2"] = {
            "duration_s": duration,
            "media_duration_s": media.get("media_duration_s"),
            "coverage": coverage,
            "audio_gaps_over_2s": gaps,
            "audio_gaps_over_500ms": (media.get("audio") or {}).get(
                "audio_pts_gaps_over_500ms"),
            "width": media.get("width"),
            "height": media.get("height"),
            "verdict": record.get("verdict"),
            "reconnects": (record.get("capture") or {}).get(
                "stderr_categories", {}).get("source_reconnect"),
        }
        attached += 1
    return attached


def load_outputs(directory: Path) -> dict:
    """Prepared-output evidence keyed by (identity, chosen channel)."""
    records = {}
    for path in sorted(directory.glob("output-*.json")):
        try:
            record = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        records[(str(record.get("identity")), str(record.get("channel_id")))] = record
    return records


def load_decodes(directory: Path) -> dict:
    """Decode-check evidence keyed by (identity, chosen channel)."""
    records = {}
    for path in sorted(directory.glob("decode-*.json")):
        try:
            record = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        records[(str(record.get("identity")), str(record.get("channel_id")))] = record
    return records


def valid_decode_evidence(record: dict) -> bool:
    """Require the current decode validation and recheck its measured inputs."""
    required = {"valid_duration", "capture_ok", "capture_has_media", "video_coverage", "video_timestamps_valid",
                "audio_coverage", "video_frames", "decode_process_ok", "decode_clean"}
    checks = (record.get("validation") or {}).get("checks") or {}
    if record.get("verdict") != "decode_ok" or set(checks) != required \
            or any(checks[name] is not True for name in required):
        return False
    media = record.get("media") or {}
    try:
        verdict, _ = decode_verdict(record.get("capture") or {},
                                    {"video_packet_span_s": media.get("video_packet_span_s"),
                                     "video_packet_covered_s": media.get("video_packet_covered_s"),
                                     "video_packets": media.get("video_packets"),
                                     "video_packets_timestamped": media.get("video_packets_timestamped"),
                                     "frames_decoded": media.get("video_decoded_frames"),
                                     "audio": {"audio_duration_s": media.get("audio_duration_s")}},
                                    record.get("decode") or {}, record.get("duration_s"))
        return verdict == "decode_ok"
    except (TypeError, ValueError):
        return False


def valid_output_evidence(record: dict) -> bool:
    """Revalidate the measured output and both captures; never trust a label alone."""
    if record.get("verdict") != "output_ok" or record.get("output_profile") != OUTPUT_PROFILE:
        return False
    source = record.get("source_capture") or {}
    capture = record.get("output_capture") or {}
    if (source.get("returncode") != 0 or capture.get("profile") != OUTPUT_PROFILE
            or not isinstance(source.get("bytes"), int) or source["bytes"] <= 188 * 100
            or not isinstance(capture.get("bytes"), int) or capture["bytes"] <= 188 * 100
            or not isinstance(capture.get("raw_bytes"), int) or capture["raw_bytes"] <= 188 * 100
            or not isinstance(capture.get("artifact_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", capture["artifact_sha256"])):
        return False
    try:
        verdict, _ = output_verdict(record.get("output") or {}, capture, PROFILES[OUTPUT_PROFILE])
        return verdict == "output_ok"
    except (TypeError, ValueError, OverflowError):
        return False


def validate_plan(plan: dict, catalog: dict, favorites: list[str]) -> list[dict]:
    if plan.get("schema_version") != 1:
        raise ExportError("unsupported candidate plan schema")
    identities = plan.get("identities")
    if not isinstance(identities, list) or not 1 <= len(identities) <= MAX_IDENTITIES:
        raise ExportError("identity count outside the supported limit")
    saved = set(favorites)
    allowed_numbers = {channel_id for channel_id, row in catalog.items()
                       if row.get("stream_type") == "live"}
    seen_identities = set()
    seen_names = set()
    used_channels = set()
    result = []
    for item in identities:
        identity = str(item.get("identity", ""))
        if identity not in FAMILY_PATTERNS:
            raise ExportError(f"unknown identity {identity!r}")
        if identity in seen_identities:
            raise ExportError(f"duplicate identity {identity!r}")
        seen_identities.add(identity)
        category = str(item.get("category", ""))
        if category not in CATEGORIES:
            raise ExportError(f"unsupported root category for {identity}")
        filename = str(item.get("filename", ""))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._&()-]{2,60}\.trp", filename):
            raise ExportError(f"invalid NTFS filename for {identity}")
        key = f"{category}/{filename}".lower()
        if key in seen_names:
            raise ExportError("duplicate NTFS path")
        seen_names.add(key)
        favorite_ids = [str(value) for value in item.get("favorite_ids", [])]
        if not favorite_ids:
            raise ExportError(f"{identity} has no original favorite")
        for favorite_id in favorite_ids:
            if favorite_id not in saved:
                raise ExportError(f"{identity} favorite is not in the personal list")
            if favorite_id not in allowed_numbers:
                raise ExportError(f"{identity} favorite is not a Live catalog row")
        family = re.compile(FAMILY_PATTERNS[identity], re.I)
        candidates = item.get("candidates")
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 8:
            raise ExportError(f"{identity} candidate list outside the limit")
        tested = []
        rejected = []
        for candidate in candidates:
            channel_id = str(candidate.get("channel_id", ""))
            row = catalog.get(channel_id)
            if row is None:
                rejected.append({"channel_id": channel_id, "reason": "missing_from_catalog"})
                continue
            if row.get("stream_type") != "live":
                rejected.append({"channel_id": channel_id, "reason": "not_live"})
                continue
            name = str(row.get("name", ""))
            if not family.search(name):
                rejected.append({"channel_id": channel_id, "reason": "wrong_family"})
                continue
            if channel_id in used_channels:
                raise ExportError("duplicate candidate across identities")
            used_channels.add(channel_id)
            tested.append({"channel_id": channel_id, "catalog_name": name,
                           "region": str(candidate.get("region", "other")),
                           "quality_label": str(candidate.get("quality_label", ""))})
        if not tested:
            raise ExportError(f"{identity} has no valid candidate")
        result.append({
            "identity": identity,
            "category": category,
            "filename": filename,
            "favorite_ids": favorite_ids,
            "tested": tested,
            "plan_rejected": rejected,
        })
    live_saved = {favorite for favorite in saved
                  if catalog.get(favorite, {}).get("stream_type") == "live"}
    covered = {favorite for identity in result for favorite in identity["favorite_ids"]}
    missing = sorted(live_saved - covered)
    if missing:
        raise ExportError(f"Live favorites not mapped to any identity: {missing}")
    return result


def record_stability(record: dict) -> tuple[float | None, int, bool, int]:
    """Continuity of the measurement; missing data is never treated as stable."""
    media = record.get("media") or {}
    duration = record.get("duration_s") or 0
    coverage = media.get("capture_coverage")
    media_duration = media.get("media_duration_s")
    if coverage is None and duration and media_duration is not None:
        coverage = media_duration / duration
    audio = media.get("audio") or {}
    gaps = audio.get("audio_pts_gaps_over_2000ms")
    if gaps is None:
        max_gap = audio.get("audio_pts_max_gap_ms")
        gaps = None if max_gap is None else (1 if max_gap > 2000 else 0)
    data_present = bool(media) and coverage is not None and gaps is not None
    # An independently reported coverage must agree with the measured duration;
    # a supplied percentage alone is not evidence of a 300-second capture.
    stable = (data_present and duration >= MIN_FINAL_SECONDS
              and media_duration is not None and media_duration > 0
              and abs(coverage - media_duration / duration) <= 0.001
              and coverage >= 0.97 and gaps == 0)
    windows = int(record.get("windows") or 1)
    second = record.get("window2") or {}
    if windows >= 2:
        second_coverage = second.get("coverage")
        second_gaps = second.get("audio_gaps_over_2s")
        second_duration = second.get("duration_s") or 0
        second_media_duration = second.get("media_duration_s")
        second_valid = (second.get("verdict") == "candidate_ok"
                        and second.get("width") == 1920
                        and second.get("height") == 1080
                        and second_duration >= MIN_FINAL_SECONDS
                        and second_media_duration is not None and second_media_duration > 0
                        and second_coverage is not None and second_coverage >= 0.97
                        and abs(second_coverage - second_media_duration / second_duration) <= 0.001
                        and second_gaps is not None and second_gaps == 0
                        )
        stable = stable and second_valid
    return coverage, gaps or 0, stable, windows


def pick_candidate(identity: dict, evidence: dict, outputs: dict | None = None,
                   decodes: dict | None = None) -> dict:
    ranked = []
    for candidate in identity["tested"]:
        channel_id = candidate["channel_id"]
        records = [evidence.get((channel_id, phase)) for phase in ("winner", "final", "triage")]
        records = [record for record in records if record]
        if not records:
            continue
        record = max(records, key=lambda item: item.get("duration_s", 0))
        media = record.get("media") or {}
        is_hd = media.get("width") == 1920 and media.get("height") == 1080
        qualified = record.get("phase") in ("final", "winner") and \
            record.get("duration_s", 0) >= MIN_FINAL_SECONDS and \
            (record.get("duration_s", 0) >= MIN_WINNER_SECONDS
             if record.get("phase") == "winner" else True)
        good = record.get("verdict") in ("candidate_ok", "not_1080p")
        _, _, stable, windows = record_stability(record)
        # Stable native 1080p wins even against a better quality label; a bad
        # source is never kept just because its label looked stronger. A
        # qualified (300 s) measurement dominates an unqualified triage sample.
        qualified_stable = qualified and stable
        rank = (0 if (is_hd and good and qualified_stable) else
                1 if (good and qualified_stable) else
                2 if (is_hd and good and qualified) else
                3 if (good and qualified) else
                4 if (is_hd and good and stable) else
                5 if (good and stable) else
                6 if (is_hd and good) else
                7 if good else
                8 if is_hd else 9)
        output_record = (outputs or {}).get((identity["identity"], channel_id)) or {}
        decode_record = (decodes or {}).get((identity["identity"], channel_id)) or {}
        # Only fully measured sources outrank the normal quality/region order.
        # During provisional evidence gathering, preserve the prior order so the
        # first candidate can still be selected for the missing probes.
        fully_approved = (rank == 0 and windows >= 2
                          and candidate["region"] in APPROVABLE_REGIONS
                          and output_record.get("identity") == identity["identity"]
                          and str(output_record.get("channel_id")) == channel_id
                          and valid_output_evidence(output_record)
                          and decode_record.get("identity") == identity["identity"]
                          and str(decode_record.get("channel_id")) == channel_id
                          and valid_decode_evidence(decode_record))
        ranked.append((
            rank,
            REGION_RANK.get(candidate["region"], 9),
            QUALITY_RANK.get(candidate["quality_label"], 9),
            -record.get("duration_s", 0),
            int(channel_id),
            candidate,
            record,
            good and qualified and candidate["region"] in APPROVABLE_REGIONS,
            fully_approved,
        ))
    if not ranked:
        raise ExportError(f"{identity['identity']} has no probe evidence")
    if outputs is not None and decodes is not None:
        ranked.sort(key=lambda item: (not item[8], *item[:5]))
    else:
        ranked.sort(key=lambda item: item[:5])
    best = ranked[0]
    other_hd = [item for item in ranked
                if (item[6].get("media") or {}).get("height") == 1080
                and item[5]["region"] not in APPROVABLE_REGIONS]
    return {"candidate": best[5], "record": best[6], "qualified": best[7],
            "native_1080": best[0] == 0,
            "is_1080": (best[6].get("media") or {}).get("height") == 1080,
            "other_region_1080": other_hd[0][5] if other_hd else None}


def build_export(plan: dict, catalog: dict, favorites: list[str], evidence: dict,
                 generation: int, outputs: dict | None = None,
                 decodes: dict | None = None) -> dict:
    identities = validate_plan(plan, catalog, favorites)
    live_favorite_ids = sorted({favorite for identity in identities
                                for favorite in identity["favorite_ids"]
                                if catalog.get(favorite, {}).get("stream_type") == "live"})
    entries = []
    pending = []
    for identity in identities:
        picked = pick_candidate(identity, evidence, outputs, decodes)
        candidate = picked["candidate"]
        record = picked["record"]
        media = record.get("media") or {}
        output_record = (outputs or {}).get((identity["identity"], candidate["channel_id"])) or {}
        output_ok = (output_record.get("identity") == identity["identity"]
                     and str(output_record.get("channel_id")) == candidate["channel_id"]
                     and valid_output_evidence(output_record))
        decode_record = (decodes or {}).get((identity["identity"], candidate["channel_id"])) or {}
        decode_ok = (decode_record.get("identity") == identity["identity"]
                     and str(decode_record.get("channel_id")) == candidate["channel_id"]
                     and valid_decode_evidence(decode_record))
        coverage, audio_gaps, source_stable, windows = record_stability(record)
        if picked["qualified"] and picked["native_1080"] and source_stable:
            if decodes is not None and not decode_ok:
                status, quality_status = "pending", "pending_decode_validation"
            elif outputs is not None and not output_ok:
                status, quality_status = "pending", "pending_output_validation"
            elif outputs is not None and windows < 2:
                status, quality_status = "pending", "pending_second_window"
            else:
                status, quality_status = "approved", "native_1080"
        elif picked["qualified"] and picked["is_1080"]:
            status, quality_status = "pending", "pending_source_stability"
        elif not picked["is_1080"] and record.get("verdict") in ("candidate_ok", "not_1080p"):
            # Playable alternative, but never native 1080p: the Sol decides.
            status, quality_status = "pending", "pending_no_native_1080"
        elif picked["qualified"]:
            # Best operational alternative, but never approved: the grade
            # requires a measured native 1920x1080 source.
            status, quality_status = "pending", "pending_no_native_1080"
        elif record.get("verdict") == "probe_error":
            # A local analysis failure is a pending measurement, never an
            # approval and never an unavailability claim.
            status, quality_status = "pending_measurement", None
        else:
            status, quality_status = "unavailable", None
        if status != "approved":
            reason = record.get("verdict") if status != "unavailable" else "no_qualified_source"
            if status == "pending" and quality_status == "pending_output_validation":
                reason = "output_" + ",".join(output_record.get("failed") or ["unvalidated"])
            if status == "pending" and quality_status == "pending_source_stability":
                reason = f"coverage={coverage},audio_gaps_over_2s={audio_gaps}"
            if status == "pending" and quality_status == "pending_second_window":
                reason = f"windows={windows}"
            if status == "pending" and quality_status == "pending_decode_validation":
                reason = f"decode={decode_record.get('verdict')}"
            if status == "pending" and quality_status == "pending_no_native_1080":
                gap = (media.get("audio") or {}).get("audio_pts_max_gap_ms")
                reason = f"{record.get('verdict')},audio_gap_ms={gap}"
            pending.append({
                "identity": identity["identity"],
                "best_channel_id": candidate["channel_id"],
                "best_catalog_name": candidate["catalog_name"],
                "best_region": candidate["region"],
                "best_native": f"{media.get('width')}x{media.get('height')}",
                "status": status,
                "quality_status": quality_status,
                "reason": reason,
                "source_audio_max_gap_ms": (media.get("audio") or {}).get("audio_pts_max_gap_ms"),
                "output_verdict": output_record.get("verdict"),
                "native_1080_in_other_region": picked["other_region_1080"],
            })
        rows = []
        for tested in identity["tested"]:
            probe = evidence.get((tested["channel_id"], "winner")) or \
                evidence.get((tested["channel_id"], "final")) or \
                evidence.get((tested["channel_id"], "triage"))
            entry = dict(tested)
            entry["phase"] = probe.get("phase") if probe else None
            entry["duration_s"] = probe.get("duration_s") if probe else None
            entry["verdict"] = probe.get("verdict") if probe else "not_tested"
            entry["width"] = (probe.get("media") or {}).get("width") if probe else None
            entry["height"] = (probe.get("media") or {}).get("height") if probe else None
            rows.append(entry)
        entries.append({
            "identity": identity["identity"],
            "category": identity["category"],
            "filename": identity["filename"],
            "favorite_ids": identity["favorite_ids"],
            "status": status,
            "quality_status": quality_status,
            "approved": status == "approved",
            "chosen": {
                "channel_id": candidate["channel_id"],
                "catalog_name": candidate["catalog_name"],
                "region": candidate["region"],
                "quality_label": candidate["quality_label"],
                "native_width": media.get("width"),
                "native_height": media.get("height"),
                "video_codec": media.get("video_codec"),
                "profile": media.get("profile"),
                "video_kbps": media.get("video_kbps"),
                "ts_kbps": media.get("ts_kbps_total"),
                "audio_codec": media.get("audio_codec"),
                "pcr_max_gap_ms": (media.get("pcr") or {}).get("pcr_max_gap_ms"),
                "evidence_phase": record.get("phase"),
                "evidence_duration_s": record.get("duration_s"),
                "source_audio_max_gap_ms": (media.get("audio") or {}).get("audio_pts_max_gap_ms"),
                "output_verdict": output_record.get("verdict"),
                "output_evidence": output_record,
                "output_failed": output_record.get("failed") or [],
                "decode_verdict": decode_record.get("verdict"),
                "decode_lines": (decode_record.get("decode") or {}).get("lines"),
                "decode_evidence": decode_record,
                "source_evidence": {
                    "phase": record.get("phase"),
                    "duration_s": record.get("duration_s"),
                    "verdict": record.get("verdict"),
                    "media": {key: media.get(key) for key in (
                        "width", "height", "capture_coverage", "media_duration_s", "audio")},
                    "windows": windows,
                    "window2": record.get("window2"),
                },
                "output_pcr_max_gap_ms": ((output_record.get("output") or {}).get("pcr") or {}).get(
                    "pcr_max_gap_ms"),
                "output_audio_gaps_500ms": ((output_record.get("output") or {}).get("audio") or {}).get(
                    "audio_pts_gaps_over_500ms"),
            },
            "candidates_tested": rows,
            "rejected": identity["plan_rejected"] + [
                {"channel_id": row["channel_id"],
                 "reason": row["verdict"] if row["verdict"] not in ("candidate_ok", None)
                 else "region_requires_explicit_approval"}
                for row in rows
                if row["verdict"] not in ("candidate_ok", None)
                or (row["verdict"] == "candidate_ok" and row["region"] not in APPROVABLE_REGIONS)
            ],
        })
    approved = [entry for entry in entries if entry["status"] == "approved"]
    pending_1080 = [entry for entry in entries
                    if entry["quality_status"] == "pending_no_native_1080"]
    pending_output = [entry for entry in entries
                      if entry["quality_status"] == "pending_output_validation"]
    pending_stability = [entry for entry in entries
                         if entry["quality_status"] == "pending_source_stability"]
    pending_second = [entry for entry in entries
                      if entry["quality_status"] == "pending_second_window"]
    pending_decode = [entry for entry in entries
                      if entry["quality_status"] == "pending_decode_validation"]
    unavailable = [entry for entry in entries if entry["status"] == "unavailable"]
    pending_measurement = [entry for entry in entries
                           if entry["status"] == "pending_measurement"]
    if not approved and not pending_1080 and not pending_measurement \
            and not pending_output and not pending_stability and not pending_second \
            and not pending_decode:
        raise ExportError("no measured source in the snapshot")
    return {
        "schema_version": 1,
        "generation": generation,
        "output_profile": OUTPUT_PROFILE,
        "provisional": outputs is None or decodes is None,
        "snapshot": {
            "favorites_total": len(favorites),
            "live_favorites": len(live_favorite_ids),
            "identities": len(entries),
            "approved": len(approved),
            "native_1080": len(approved),
            "pending_1080": len(pending_1080),
            "pending_output": len(pending_output),
            "pending_stability": len(pending_stability),
            "pending_second_window": len(pending_second),
            "pending_decode": len(pending_decode),
            "pending_measurement": len(pending_measurement),
            "unavailable": len(unavailable),
            "pending": len(pending),
            "output_validated": sum(1 for entry in approved
                                    if (entry.get("chosen") or {}).get("output_verdict") == "output_ok"),
            "catalog_rows": len(catalog),
        },
        "identities": entries,
        "pending": pending,
    }


def validate_config(config: dict, catalog: dict, favorites: list[str]) -> None:
    if config.get("schema_version") != 1:
        raise ExportError("unsupported export schema")
    if config.get("output_profile") != OUTPUT_PROFILE:
        raise ExportError("unsupported output profile")
    identities = config.get("identities")
    if not isinstance(identities, list) or not 1 <= len(identities) <= MAX_IDENTITIES:
        raise ExportError("identity count outside the supported limit")
    saved = set(favorites)
    seen = set()
    chosen_seen = set()
    paths = set()
    covered = set()
    for item in identities:
        identity = str(item.get("identity", ""))
        if identity in seen:
            raise ExportError("duplicate identity")
        seen.add(identity)
        status = item.get("status")
        quality = item.get("quality_status")
        if status not in ("approved", "pending", "pending_measurement", "unavailable"):
            raise ExportError("invalid status")
        if status == "approved" and quality != "native_1080":
            raise ExportError("approved identity is not native 1080p")
        if status == "approved" and not config.get("provisional"):
            chosen_output = (item.get("chosen") or {}).get("output_verdict")
            if chosen_output != "output_ok":
                raise ExportError("approved identity has no validated TV output")
            output_evidence = (item.get("chosen") or {}).get("output_evidence") or {}
            if (output_evidence.get("identity") != identity
                    or str(output_evidence.get("channel_id")) != str((item.get("chosen") or {}).get("channel_id"))
                    or not valid_output_evidence(output_evidence)):
                raise ExportError("approved identity has invalid TV output evidence")
            if (item.get("chosen") or {}).get("decode_verdict") != "decode_ok":
                raise ExportError("approved identity has no clean decode check")
            decode_evidence = (item.get("chosen") or {}).get("decode_evidence") or {}
            if (decode_evidence.get("identity") != identity
                    or str(decode_evidence.get("channel_id")) != str((item.get("chosen") or {}).get("channel_id"))
                    or not valid_decode_evidence(decode_evidence)):
                raise ExportError("approved identity has outdated or invalid decode evidence")
            source = (item.get("chosen") or {}).get("source_evidence") or {}
            media = source.get("media") or {}
            _, _, stable, windows = record_stability(source)
            if (source.get("phase") not in ("winner", "final")
                    or source.get("verdict") != "candidate_ok"
                    or (media.get("width"), media.get("height")) != (1920, 1080)
                    or windows < 2 or not stable):
                raise ExportError("approved identity lacks valid two-window source evidence")
        if status == "pending" and quality not in ("pending_no_native_1080",
                                                  "pending_output_validation",
                                                  "pending_source_stability",
                                                  "pending_second_window",
                                                  "pending_decode_validation"):
            raise ExportError("pending identity needs a pending quality status")
        if status == "pending_measurement" and quality is not None:
            raise ExportError("pending measurement cannot carry a quality status")
        if status == "unavailable" and quality is not None:
            raise ExportError("unavailable identity cannot carry a quality status")
        category = str(item.get("category", ""))
        filename = str(item.get("filename", ""))
        if category not in CATEGORIES:
            raise ExportError("unsupported root category")
        if ".." in filename or "/" in filename or "\\" in filename:
            raise ExportError("path traversal in filename")
        path = f"{category}/{filename}".lower()
        if path in paths:
            raise ExportError("duplicate NTFS path")
        paths.add(path)
        for favorite_id in item.get("favorite_ids", []):
            if str(favorite_id) not in saved:
                raise ExportError("export favorite is not personal")
            covered.add(str(favorite_id))
        chosen = item.get("chosen") or {}
        channel_id = str(chosen.get("channel_id", ""))
        if channel_id in chosen_seen:
            raise ExportError("duplicate chosen candidate")
        chosen_seen.add(channel_id)
        if status in ("approved", "pending"):
            row = catalog.get(channel_id)
            if row is None:
                raise ExportError("chosen candidate missing from catalog")
            if row.get("stream_type") != "live":
                raise ExportError("chosen candidate is not Live")
            tested = {str(entry.get("channel_id")) for entry in item.get("candidates_tested", [])}
            if channel_id not in tested:
                raise ExportError("chosen candidate was not tested")
            if not FAMILY_PATTERNS.get(identity) or not re.search(
                    FAMILY_PATTERNS[identity], str(row.get("name", "")), re.I):
                raise ExportError("chosen candidate belongs to another family")
    live_saved = {favorite for favorite in saved
                  if catalog.get(favorite, {}).get("stream_type") == "live"}
    if live_saved - covered:
        raise ExportError("export omits Live favorites")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--favorites", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output-evidence", type=Path,
                        help="prepared-output evidence directory (required for the final export)")
    parser.add_argument("--second-window", type=Path,
                        help="independent second 300 s window directory")
    parser.add_argument("--decode-evidence", type=Path,
                        help="clean-decode evidence directory (required for the final export)")
    parser.add_argument("--generation", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provisional", action="store_true",
                        help="write the resolver export used to run the output probes")
    parser.add_argument("--check", action="store_true",
                        help="validate an existing output instead of rewriting it")
    args = parser.parse_args()
    try:
        catalog = load_catalog(args.catalog)
        favorites = load_favorites(args.favorites)
        if args.check:
            config = load_json(args.output)
            validate_config(config, catalog, favorites)
            if config.get("provisional"):
                raise ExportError("provisional export is not reviewable")
            print("export config valid")
            return 0
        if not args.provisional and args.output_evidence is None:
            raise ExportError("final export requires --output-evidence (or --provisional)")
        if not args.provisional and args.decode_evidence is None:
            raise ExportError("final export requires --decode-evidence (or --provisional)")
        outputs = load_outputs(args.output_evidence) if args.output_evidence else None
        decodes = load_decodes(args.decode_evidence) if args.decode_evidence else None
        evidence = load_evidence(args.evidence)
        if args.second_window is not None:
            attached = attach_second_window(evidence, args.second_window)
            print(json.dumps({"second_window_attached": attached}), file=sys.stderr)
        config = build_export(load_json(args.plan), catalog, favorites,
                              evidence, args.generation, outputs, decodes)
        validate_config(config, catalog, favorites)
        args.output.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    except ExportError as error:
        parser.exit(2, f"export rejected: {error}\n")
    print(json.dumps({"identities": config["snapshot"]["identities"],
                      "approved": config["snapshot"]["approved"],
                      "pending": config["snapshot"]["pending"],
                      "generation": config["generation"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
