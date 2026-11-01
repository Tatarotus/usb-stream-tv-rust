#!/bin/sh
# =============================================================================
# live_batch_run.sh — lotes sequenciais e retomáveis de teste de fontes IPTV
#
# PARA RODAR NO HOST. Não toca VPS, container, manager, tablet, USB nem a TV.
# Uma conexão por vez (sequencial). Retomável: cada passo já medido é pulado.
#
# Autorização explícita é obrigatória:
#     LIVE_BATCH_CONFIRM=1 sh scripts/live_batch_run.sh
#   (a variável representa a confirmação do operador de que a TV está livre;
#    o script nunca decide sozinho que a janela está liberada)
#
# Parada imediata: criar o arquivo apontado por STOP_FILE (padrão
# scratch/live-batch/STOP). O runner para entre passos, sem deixar nada pela
# metade — nenhuma publicação, nenhum reinício.
#
# Uso:
#   LIVE_BATCH_CONFIRM=1 LIMIT=2 sh scripts/live_batch_run.sh
# =============================================================================
set -eu
cd "$(dirname "$0")/.." || exit 1

PYTHON_BIN=${PYTHON_BIN:-python3}
# Limites de impacto (host local): prioridade de CPU baixa e, se indicado, afinidade.
NICE_LEVEL=${NICE_LEVEL:-19}
CPUSET=${CPUSET:-}
# Evidência de rota/origem: quem mediu, de onde, com que carga. IP público fica
# registrado como HASH (não é credencial, mas não precisa circular em claro).
ORIGIN=${ORIGIN:-host-local}
ROUTE_LOG=${ROUTE_LOG:-scratch/live-batch/route.log}

QUEUE=${QUEUE:-scratch/live-batch/queue.json}
OUT=${OUT:-scratch/live-batch/evidence}
W2=${W2:-scratch/live-batch/evidence-w2}
DEC=${DEC:-scratch/live-batch/evidence-decode}
OUTP=${OUTP:-scratch/live-batch/evidence-output}
STOP_FILE=${STOP_FILE:-scratch/live-batch/STOP}
LIMIT=${LIMIT:-2}
PROGRESS=${PROGRESS:-scratch/live-batch/progress.json}
COST=${COST:-scratch/live-batch/cost.csv}
LOG=${LOG:-scratch/live-batch/run.log}

log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$LOG"; }

if [ "${LIVE_BATCH_CONFIRM:-0}" != "1" ]; then
    echo "recusando: rode com LIVE_BATCH_CONFIRM=1 (confirmacao explicita de que a TV esta livre)" >&2
    exit 2
fi
[ -f "$QUEUE" ] || { echo "fila ausente: $QUEUE" >&2; exit 2; }
# STOP nao e apagado por este script: um pedido de parada nao pode ser perdido.
# Para retomar, o operador remove o arquivo explicitamente.
if [ -f "$STOP_FILE" ]; then
    echo "STOP presente em $STOP_FILE: nada sera sondado. Remova-o explicitamente para retomar." >&2
    exit 3
fi

mkdir -p "$OUT" "$W2" "$DEC" "$OUTP" "$(dirname "$STOP_FILE")" "$(dirname "$COST")" "$(dirname "$ROUTE_LOG")"
# Rota/origem do lote: sem hostname de provedor, sem URL, sem credencial.
{
    printf 'batch_start=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'origin=%s\n' "$ORIGIN"
    printf 'host=%s\n' "$(hostname)"
    printf 'kernel=%s\n' "$(uname -sr)"
    printf 'iface=%s\n' "$(ip route get 1.1.1.1 2>/dev/null | awk '{print $5; exit}')"
    printf 'public_ip_sha256_12=%s\n' "$(curl -s -m 8 https://api.ipify.org 2>/dev/null | sha256sum | cut -c1-12)"
    printf 'nproc=%s\n' "$(nproc)"
    printf 'nice=%s cpuset=%s\n' "$NICE_LEVEL" "${CPUSET:-none}"
} >> "$ROUTE_LOG"
[ -f "$COST" ] || printf 'identity,channel_id,step,bytes,seconds\n' > "$COST"

should_stop() {
    [ -f "$STOP_FILE" ] && { log "STOP pedido: encerrando o lote com o trabalho feito"; return 0; }
    return 1
}

# Uma conexao por vez, GLOBAL: antes de cada canal e entre lotes, prova que nada
# esta consumindo a conexao de origem (host e manager/FFmpeg da VPS). Se nao for
# comprovavel, para e pede confirmacao — nunca assume a janela.
preflight_or_stop() { # <contexto>
    if "$PYTHON_BIN" scripts/live_batch_preflight.py > "$BATCH_PREFLIGHT_LOG" 2>&1; then
        log "pre-voo ok ($1): $(tail -1 "$BATCH_PREFLIGHT_LOG")"
        return 0
    fi
    log "PRE-VOO REPROVADO ($1): $(tail -1 "$BATCH_PREFLIGHT_LOG" 2>/dev/null)"
    log "nenhuma sondagem sera aberta; confirme a janela ao usuario (nao encerro servicos)"
    return 1
}
BATCH_PREFLIGHT_LOG=scratch/live-batch/preflight.log
touch "$BATCH_PREFLIGHT_LOG" 2>/dev/null || true

resources() {
    printf 'load=%s mem_livre=%sMB' "$(cut -d' ' -f1 /proc/loadavg)" \
        "$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)"
}

# Fila: primeiros LIMIT itens ainda não concluídos (2 janelas + decode + saída).
mapfile -t PENDENTES < <("$PYTHON_BIN" - "$QUEUE" "$OUT" "$W2" "$DEC" "$OUTP" "$LIMIT" <<'PY'
import json, pathlib, sys
queue_path, w1, w2, dec, outp, limit = sys.argv[1:7]
queue = json.loads(pathlib.Path(queue_path).read_text())["queue"]
done = 0
for item in queue:
    if done >= int(limit):
        break
    ident, cid = item["identity"], item["channel_id"]
    faltam = [p for p in (f"{w1}/winner-{ident}-{cid}.json",
                          f"{w2}/winner-{ident}-{cid}.json",
                          f"{dec}/decode-{ident}-{cid}.json",
                          f"{outp}/output-{ident}-{cid}.json") if not pathlib.Path(p).is_file()]
    if not faltam:
        continue
    print("%s|%s|%s" % (ident, cid, len(faltam)))
    done += 1
PY
)
preflight_or_stop "inicio do lote" || exit 3
log "=== lote iniciado | limite=$LIMIT | pendentes nesta chamada=${#PENDENTES[@]} | $(resources) ==="

# Plano com apenas os itens deste lote (os probes leem o plano, não a fila).
for entry in "${PENDENTES[@]:-}"; do
    [ -z "$entry" ] && continue
    ident=${entry%%|*}; rest=${entry#*|}; cid=${rest%%|*}
    should_stop && break
    plan=scratch/live-batch/plan-$ident-$cid.json
    "$PYTHON_BIN" - "$QUEUE" "$plan" "$cid" <<'PY'
import json, pathlib, sys
queue = json.loads(pathlib.Path(sys.argv[1]).read_text())["queue"]
item = next(i for i in queue if i["channel_id"] == sys.argv[3])
plan = {"schema_version": 1,
        "snapshot": {"note": "lote sequencial do host; triagem nao e aprovacao",
                     "generated": "batch"},
        "identities": [{"identity": item["identity"], "category": item["category"],
                        "filename": item["filename"],
                        "favorite_ids": [item["channel_id"]],
                        "candidates": [{"channel_id": item["channel_id"],
                                        "catalog_name": item["catalog_name"],
                                        "region": item["region"],
                                        "quality_label": "FHD"}]}]}
pathlib.Path(sys.argv[2]).write_text(json.dumps(plan, indent=2, ensure_ascii=False))
PY

    preflight_or_stop "antes de $ident/$cid" || break
    log "--- $ident / $cid | $(resources)"
    for stage in w1 w2; do
        should_stop && break
        preflight_or_stop "antes do passo $stage de $ident/$cid" || break
        dir=$([ "$stage" = w1 ] && echo "$OUT" || echo "$W2")
        [ -f "$dir/winner-$ident-$cid.json" ] && { log "  $stage ja medido, pulando"; continue; }
        t0=$(date +%s)
        ${CPUSET:+taskset -c "$CPUSET"} nice -n "$NICE_LEVEL" \
            "$PYTHON_BIN" scripts/probe_live_favorites_sources.py "$plan" --phase winner \
            --out-dir "$dir" --resume >> "$LOG" 2>&1 || log "  $stage terminou com erro"
        t1=$(date +%s)
        bytes=$( "$PYTHON_BIN" -c "
import json,sys
try:
    r=json.load(open('$dir/winner-$ident-$cid.json')); print((r.get('capture') or {}).get('bytes') or 0)
except Exception: print(0)" )
        printf '%s,%s,%s,%s,%s\n' "$ident" "$cid" "$stage" "$bytes" "$((t1-t0))" >> "$COST"
        log "  $stage: $((t1-t0))s | $(resources)"
    done

    should_stop && break
    # Export provisório apenas com o que já tem evidência, para alimentar decode/saída.
    prov=scratch/live-batch/provisorio-$ident-$cid.json
    printf '["%s"]\n' "$cid" > scratch/live-batch/favorites-$ident-$cid.json
    if [ -f "$OUT/winner-$ident-$cid.json" ]; then
        "$PYTHON_BIN" - "$plan" "$prov" "$cid" <<'PY' >> "$LOG" 2>&1 || log "  (provisorio nao construido)"
import json, pathlib, sys
plan = json.loads(pathlib.Path(sys.argv[1]).read_text())
pathlib.Path(sys.argv[2]).write_text(json.dumps({
    "schema_version": 1, "generation": 2,
    "identities": [{"identity": i["identity"], "status": "pending",
                    "quality_status": "pending_measurement",
                    "category": i["category"], "filename": i["filename"],
                    "chosen": {"channel_id": i["candidates"][0]["channel_id"],
                               "catalog_name": i["candidates"][0]["catalog_name"],
                               "region": i["candidates"][0]["region"]}} for i in plan["identities"]]},
    indent=2, ensure_ascii=False))
PY
        for kind in decode output; do
            should_stop && break
            preflight_or_stop "antes do passo $kind de $ident/$cid" || break
            dir=$([ "$kind" = decode ] && echo "$DEC" || echo "$OUTP")
            [ -f "$dir/$kind-$ident-$cid.json" ] && { log "  $kind ja medido, pulando"; continue; }
            t0=$(date +%s)
            if [ "$kind" = decode ]; then
                ${CPUSET:+taskset -c "$CPUSET"} nice -n "$NICE_LEVEL" \
                    "$PYTHON_BIN" scripts/probe_live_favorites_decode.py --export "$prov" \
                    --out-dir "$dir" --duration 60 >> "$LOG" 2>&1 || log "  decode com erro"
            else
                ${CPUSET:+taskset -c "$CPUSET"} nice -n "$NICE_LEVEL" \
                    "$PYTHON_BIN" scripts/probe_live_favorites_output.py --export "$prov" \
                    --out-dir "$dir" --duration 25 >> "$LOG" 2>&1 || log "  saida com erro"
            fi
            t1=$(date +%s)
            printf '%s,%s,%s,%s,%s\n' "$ident" "$cid" "$kind" 0 "$((t1-t0))" >> "$COST"
            log "  $kind: $((t1-t0))s | $(resources)"
        done
    fi
done

"$PYTHON_BIN" - "$QUEUE" "$OUT" "$W2" "$DEC" "$OUTP" "$PROGRESS" <<'PY'
import json, pathlib, sys
queue_path, w1, w2, dec, outp, progress = sys.argv[1:7]
queue = json.loads(pathlib.Path(queue_path).read_text())["queue"]
state = {"total": len(queue), "done": 0, "partial": [], "pending": []}
for item in queue:
    ident, cid = item["identity"], item["channel_id"]
    paths = [f"{w1}/winner-{ident}-{cid}.json", f"{w2}/winner-{ident}-{cid}.json",
             f"{dec}/decode-{ident}-{cid}.json", f"{outp}/output-{ident}-{cid}.json"]
    present = [p for p in paths if pathlib.Path(p).is_file()]
    if len(present) == 4:
        state["done"] += 1
    elif present:
        state["partial"].append({"identity": ident, "channel_id": cid,
                                 "steps": [pathlib.Path(p).name for p in present]})
    else:
        state["pending"].append({"identity": ident, "channel_id": cid})
pathlib.Path(progress).write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")
print("progresso: %d/%d completos | %d parciais | %d nao iniciados"
      % (state["done"], state["total"], len(state["partial"]), len(state["pending"])))
PY

preflight_or_stop "encerramento do lote" || log "  (encerrando com o pre-voo reprovado: conexao pode estar em uso)"
log "=== lote encerrado | $(resources) ==="
