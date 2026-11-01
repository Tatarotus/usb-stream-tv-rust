#!/usr/bin/env python3
"""Sanitized evidence export: JSON/CSV + markdown table for the review.

Only public catalog IDs/names and measurement numbers are emitted. Provider
URLs, credentials, tokens and raw stderr never leave the probe harness.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_live_favorites_export as exporter  # noqa: E402


def video_bitrate(media: dict) -> tuple[int | None, bool]:
    """Correct the video bitrate when the frame listing covered only a window."""
    value = media.get("video_kbps")
    if not value:
        return value, False
    if media.get("video_span_s"):
        return value, False
    frames = media.get("frames_decoded")
    duration = media.get("media_duration_s")
    rate = str(media.get("fps") or "")
    if frames and duration and "/" in rate:
        numerator, _, denominator = rate.partition("/")
        try:
            fps = float(numerator) / float(denominator or 1)
        except (ValueError, ZeroDivisionError):
            return value, False
        window = frames / fps if fps else 0
        if window and window < duration * 0.95:
            return round(value * window / duration), True
    return value, False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--output-evidence", type=Path,
                        help="directory with output-<identity>-<channel>.json records")
    parser.add_argument("--second-window", type=Path,
                        help="independent second 300 s window directory")
    parser.add_argument("--title", default="Resultados por canal")
    args = parser.parse_args()
    config = json.loads(args.export.read_text())
    evidence = exporter.load_evidence(args.evidence)
    if args.second_window is not None:
        exporter.attach_second_window(evidence, args.second_window)
    outputs = {}
    if args.output_evidence and args.output_evidence.is_dir():
        for path in sorted(args.output_evidence.glob("output-*.json")):
            try:
                record = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            outputs[(record.get("identity"), str(record.get("channel_id")))] = record
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in config["identities"]:
        chosen = item["chosen"]
        channel = chosen["channel_id"]
        record = evidence.get((channel, "winner")) or evidence.get((channel, "final")) \
            or evidence.get((channel, "triage")) or {}
        media = record.get("media") or {}
        measured = outputs.get((item["identity"], str(channel))) or {}
        output_media = measured.get("output") or {}
        video_value, adjusted = video_bitrate(media)
        rows.append({
            "identity": item["identity"],
            "favorite_ids": ",".join(item["favorite_ids"]),
            "chosen_channel_id": channel,
            "chosen_name": chosen["catalog_name"],
            "region": chosen["region"],
            "status": item["status"],
            "quality_status": item["quality_status"],
            "native_resolution": f"{chosen.get('native_width')}x{chosen.get('native_height')}",
            "video_codec": chosen.get("video_codec"),
            "profile": chosen.get("profile"),
            "video_kbps": video_value,
            "video_kbps_adjusted": adjusted,
            "ts_kbps": chosen.get("ts_kbps"),
            "phase": chosen.get("evidence_phase"),
            "duration_s": chosen.get("evidence_duration_s"),
            "audio_codec": chosen.get("audio_codec"),
            "audio_gaps_500ms": (media.get("audio") or {}).get("audio_pts_gaps_over_500ms"),
            "pcr_max_gap_ms": chosen.get("pcr_max_gap_ms"),
            "output_profile": config["output_profile"],
            "verdict": record.get("verdict"),
            "method": f"ffprobe copy, {media.get('media_duration_s')} s de vídeo",
            "output_verdict": measured.get("verdict"),
            "output_pcr_max_gap_ms": (output_media.get("pcr") or {}).get("pcr_max_gap_ms"),
            "output_audio_gaps_500ms": (output_media.get("audio") or {}).get(
                "audio_pts_gaps_over_500ms"),
            "output_ts_kbps": output_media.get("ts_kbps_total"),
            "output_failed": "; ".join(measured.get("failed") or []),
            "reason": ({
                "native_1080": "fonte nativa 1920x1080 + duas janelas + decode + saída validados",
                "pending_no_native_1080": "sem fonte nativa 1080p; melhor alternativa apresentada, não aprovada",
                "pending_source_stability": "fonte 1080p, mas cobertura de mídia/áudio insuficiente em janela",
                "pending_decode_validation": "fonte 1080p, mas o decode real registrou erros",
                "pending_output_validation": "fonte 1080p, mas a saída para TV não passou no gate",
                "pending_second_window": "fonte 1080p com apenas uma janela válida",
                None: "sem fonte qualificada",
            }.get(item.get("quality_status"), "pendência")
             + f"; janelas={record.get('windows', 1)}; {len(item.get('candidates_tested', []))} candidato(s) medido(s)"),
            "rejected": "; ".join(f"{entry['channel_id']}:{entry['reason']}"
                                  for entry in item.get("rejected", [])),
        })
    json_path = args.out_dir / "GRADE_FAVORITES_EVIDENCE.json"
    json_path.write_text(json.dumps({
        "snapshot": config["snapshot"],
        "output_profile": config["output_profile"],
        "generation": config["generation"],
        "rows": rows,
        "pending": config.get("pending", []),
    }, ensure_ascii=False, indent=2) + "\n")
    csv_path = args.out_dir / "GRADE_FAVORITES_EVIDENCE.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    lines = [
        f"# {args.title}",
        "",
        f"Perfil de saída: `{config['output_profile']}`. Snapshot: "
        f"{config['snapshot']['favorites_total']} favoritos, "
        f"{config['snapshot']['live_favorites']} Live, "
        f"{config['snapshot']['identities']} identidades, "
        f"{config['snapshot']['native_1080']} nativas 1080p, "
        f"{config['snapshot'].get('pending_1080', 0)} pendentes de 1080p.",
        "",
        "| Identidade | Favorito(s) | Escolhido | Região | Resolução nativa | Vídeo | TS | Áudio | Lacunas áudio >500 ms (fonte) | PCR fonte | Teste | Método | Saída 1080p | PCR saída | Áudio saída >500 ms | Status | Motivo |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['identity']} | {row['favorite_ids']} | {row['chosen_channel_id']} "
            f"{row['chosen_name']} | {row['region']} | {row['native_resolution']} | "
            f"{row['video_kbps']}{'*' if row['video_kbps_adjusted'] else ''} kbps | "
            f"{row['ts_kbps']} kbps | {row['audio_codec']} | "
            f"{row['audio_gaps_500ms']} | {row['pcr_max_gap_ms']} ms | "
            f"{row['phase']} {row['duration_s']} s | {row['method']} | "
            f"{row['output_verdict']} | {row['output_pcr_max_gap_ms']} ms | "
            f"{row['output_audio_gaps_500ms']} | {row['quality_status']} | "
            f"{row['reason']} {('Falhas saída: ' + row['output_failed']) if row['output_failed'] else ''} |")
    lines += [
        "",
        "\\* Bitrate de vídeo recalculado proporcionalmente: a listagem de quadros cobriu",
        "uma janela de ~150 s de uma amostra mais longa. O bitrate TS vem diretamente",
        "dos bytes capturados e da duração de mídia medida.",
    ]
    markdown_path = args.out_dir / "GRADE_FAVORITES_TABELA.md"
    markdown_path.write_text("\n".join(lines) + "\n")
    print(json.dumps({"rows": len(rows), "json": str(json_path), "csv": str(csv_path),
                      "markdown": str(markdown_path)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
