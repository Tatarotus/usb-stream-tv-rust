#!/usr/bin/env python3
"""Plano de lotes para testar canais 1080p — offline, sem abrir conexão.

Lê a evidência de triagem já produzida, escolhe UM candidato por canal distinto e
emite:

  * ``queue.json``   — a fila de teste em lotes (identidade, fonte, região,
    categoria sugerida, nome NTFS, motivo da prioridade);
  * ``pending.json`` — catálogo dos pendentes, cada um com o motivo registrado
    (não some nada: o que não entrou está listado).

Regras aplicadas (todas verificáveis no plano emitido):

1. **Triagem não aprova.** Só entra na fila quem tem 1920x1080 medido *e*
   ``candidate_ok`` na triagem; a aprovação exige a cadeia completa.
2. **Diversidade**: um canal por família; nunca outra variante de família já
   aprovada (o canal aprovado já cobre aquela marca).
3. **Prioridade do usuário** (Globo, Telecine, Discovery, Band) vem primeiro, e
   dentro da família prefere-se região ``rj`` — origem boa em RJ ganha de
   ``national``/``sp``.
4. **Deduplicação** por ``channel_id``: um canal aparece uma única vez.
5. **Região vem do NOME do catálogo**, não de medição: é rótulo de origem, não
   prova. Regiões fora de ``rj/rj-interior/sp/national`` ficam marcadas
   ``region_not_approvable`` para o revisor decidir.
6. Nenhuma credencial, URL ou texto do provedor é lido ou escrito.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CATEGORIES = ("Abertos", "Noticias", "Filmes", "Documentarios", "Variedades", "Outros")
APPROVABLE_REGIONS = ("rj", "rj-interior", "sp", "national")
REGION_RANK = {"rj": 0, "rj-interior": 1, "sp": 2, "national": 3}

# Prioridade declarada pelo usuário (29-30/10): estas famílias primeiro.
PRIORITY = ("globo", "telecine", "discovery", "band")

CATEGORY_RULES = (
    ("Noticias", r"NEWS|NOTICIA|JORNAL|BLOOMBERG|CNN|BAND NEWS|GLOBO NEWS"),
    ("Documentarios", r"DISCOVERY|HISTORY|NAT GEO|NATGEO|ANIMAL|DOC|BIOGRAPHY|"
                      r"INVESTIGA|CRIME|PLANETA|CULTURA"),
    ("Filmes", r"TELECINE|HBO|CINEMAX|MEGAPIXE|TCM|WARNER|PARAMOUNT|STUDIO|"
               r"CINE|FILM|SPACE|TNT|AXN|AMC|SYFY|SONY"),
    ("Abertos", r"^SBT|^GLOBO|^BAND|^RECORD|^REDE ?TV|^RBS|GAZETA|^TV BRASIL|"
                r"CULTURA|^REDE MASSA"),
    ("Variedades", r"MULTISHOW|GNT|VIVA|MODA|FASHION|GASTRONOMIA|RECEITA|"
                   r"COMEDY|MTV|E!|DOG|VIAGEM|BEM SIMPLES|OFF|GLOOBINHO|"
                   r"CARTOON|NICK|DISNEY|TOONCAST"),
)
SPORT_RULES = r"SPORTV|ESPN|PREMIERE|COMBATE|FOX SPORTS|BANDSPORTS|CANAL BRASIL"


def family_of(name: str) -> str:
    """Marca do canal: primeiro token significativo do nome."""
    token = re.split(r"[\s\-]+", name.strip().upper())[0]
    token = re.sub(r"[^A-Z0-9!&]", "", token)
    return token or "?"


def category_of(name: str) -> str:
    up = name.upper()
    if re.search(SPORT_RULES, up):
        return "Outros"          # não existe categoria de esporte na grade
    for category, pattern in CATEGORY_RULES:
        if re.search(pattern, up):
            return category
    return "Variedades"


def region_of(name: str) -> str:
    """Origem declarada NO NOME (rótulo, não medição)."""
    up = name.upper()
    if re.search(r"\bRJ\b|RIO DE JANEIRO|RIO SUL|ALTO LITORAL|PLAN[IÍ]CIE", up):
        return "rj"
    if re.search(r"\bSP\b|SAO PAULO|SÃO PAULO", up):
        return "sp"
    if re.search(r"\bPR\b|PARANA|PARANÁ|MASSA", up):
        return "pr"
    return "national"


def ntfs_filename(name: str, region: str) -> str:
    label = re.sub(r"[^A-Za-z0-9 ._&()-]", " ", name)
    label = re.sub(r"\s+", " ", label).strip()
    if not re.search(r"1080", label):
        label = "%s 1080" % label
    if region in ("rj", "sp") and not re.search(region.upper(), label, re.I):
        label = "%s %s" % (label, region.upper())
    return "%s.trp" % label[:58].strip()


def load_triage(evidence_dir: Path) -> dict:
    """Um registro por channel_id, mantendo a melhor medição (maior resolução)."""
    best: dict[str, dict] = {}
    for path in sorted(evidence_dir.glob("triage-*.json")):
        try:
            record = json.loads(path.read_text())
        except Exception:
            continue
        channel_id = str(record.get("channel_id") or "")
        if not channel_id:
            continue
        media = record.get("media") or {}
        height = media.get("height")
        if not isinstance(height, int):
            continue
        current = best.get(channel_id)
        if current is None or height > (current.get("height") or 0):
            best[channel_id] = {
                "channel_id": channel_id,
                "catalog_name": str(record.get("catalog_name") or ""),
                "width": media.get("width"),
                "height": height,
                "video_kbps": media.get("video_kbps"),
                "audio_codec": media.get("audio_codec"),
                "verdict": record.get("verdict"),
                "artifact": str(path),
            }
    return best


def build(triage: dict, approved_families: set[str], max_per_family: int) -> tuple[list, list]:
    by_family: dict[str, list] = {}
    pending: list[dict] = []
    for row in triage.values():
        name = row["catalog_name"]
        family = family_of(name)
        if family.lower() in approved_families:
            pending.append({**row, "reason": "family_already_approved",
                            "detail": "canal desta marca ja esta aprovado: "
                                      "outra variante nao traz canal novo"})
            continue
        if row["height"] != 1080:
            pending.append({**row, "reason": "no_native_1080",
                            "detail": "triagem mediu %sx%s" % (row["width"], row["height"])})
            continue
        if row["verdict"] != "candidate_ok":
            pending.append({**row, "reason": "triage_not_ok",
                            "detail": "veredito de triagem: %s" % row["verdict"]})
            continue
        by_family.setdefault(family, []).append(row)

    queue: list[dict] = []
    for family, rows in sorted(by_family.items()):
        rows.sort(key=lambda r: (REGION_RANK.get(region_of(r["catalog_name"]), 9),
                                 -(r["video_kbps"] or 0)))
        for rank, row in enumerate(rows[:max_per_family]):
            name = row["catalog_name"]
            region = region_of(name)
            item = {
                "identity": family.lower(),
                "family": family,
                "channel_id": row["channel_id"],
                "catalog_name": name,
                "category": category_of(name),
                "filename": ntfs_filename(name, region),
                "region": region,
                "region_source": "nome do catalogo (rotulo, nao medicao)",
                "native_measured": "%sx%s" % (row["width"], row["height"]),
                "triage_kbps": row["video_kbps"],
                "triage_artifact": row["artifact"],
                "priority": family.lower() in PRIORITY,
                "variant_rank": rank,
            }
            if region not in APPROVABLE_REGIONS:
                item["note"] = ("region_not_approvable: %s fora de %s; exige decisao "
                                "do revisor" % (region, list(APPROVABLE_REGIONS)))
            queue.append(item)
        for row in rows[max_per_family:]:
            pending.append({**row, "reason": "lower_variant_in_family",
                            "detail": "variante pior da mesma marca; fica para depois"})

    queue.sort(key=lambda item: (0 if item["priority"] else 1,
                                 REGION_RANK.get(item["region"], 9),
                                 -(item["triage_kbps"] or 0)))
    return queue, pending


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--approved-ids", default="",
                        help="ids escolhidos de identidades ja aprovadas (csv)")
    parser.add_argument("--max-per-family", type=int, default=1)
    parser.add_argument("--exclude-ids", default="",
                        help="ids ja reprovados como amostra (csv): nao gastam conexao")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--pending", required=True, type=Path)
    args = parser.parse_args()

    triage = load_triage(args.evidence)
    excluded = {v.strip() for v in args.exclude_ids.split(",") if v.strip()}
    for channel_id in sorted(excluded):
        if channel_id in triage:
            # Fica registrado no plano: a amostra ja foi reprovada e nao deve
            # consumir uma conexao nova (a regra do projeto e zero erros de decode).
            triage[channel_id]["_excluded_rejected_sample"] = True
    triage = {k: v for k, v in triage.items() if k not in excluded}
    approved_ids = {v.strip() for v in args.approved_ids.split(",") if v.strip()}
    approved_families = {family_of(triage[i]["catalog_name"]).lower()
                         for i in approved_ids if i in triage}
    queue, pending = build(triage, approved_families, args.max_per_family)

    args.out.write_text(json.dumps({
        "schema_version": 1,
        "note": ("fila de teste em lotes; triagem NAO e aprovacao. Regra: um canal "
                 "por marca, prioridade Globo/Telecine/Discovery/Band, RJ preferido."),
        "queue": queue,
    }, indent=2, ensure_ascii=False) + "\n")
    args.pending.write_text(json.dumps({"pending": pending}, indent=2,
                                       ensure_ascii=False) + "\n")

    prio = [q for q in queue if q["priority"]]
    print("fila: %d canais (%d prioritarios) | pendentes registrados: %d"
          % (len(queue), len(prio), len(pending)))
    for item in queue[:12]:
        print("   %-12s %-9s %-34s %-8s %-14s %6s kbps"
              % (item["family"], item["channel_id"], item["catalog_name"][:34],
                 item["region"], item["category"], item["triage_kbps"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
