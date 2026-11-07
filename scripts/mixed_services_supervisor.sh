#!/bin/bash
# =============================================================================
# mixed_services_supervisor.sh — mantém manager + proxy da geração ativa vivos
#
# POR QUE ISTO EXISTE
#   Manager e proxy não são units systemd: são processos-filho criados pelo
#   switch_mixed_generation_vps.sh. Um reboot do VPS não os traz de volta, e o
#   journal continua dizendo phase=active — um arquivo de estado, não uma
#   verificação de vida. Medido em 02/11 após reboot: listeners 18190/18191
#   ausentes, PIDs registrados mortos, journal afirmando "active".
#
# O QUE FAZ
#   1. resolve a sessão ativa pelo journal (a fonte de verdade da publicação);
#   2. se os PIDs registrados estão vivos e pertencem à sessão, não faz nada;
#   3. caso contrário chama `switch start`, que é IDEMPOTENTE (um no-op quando
#      a sessão já está viva) e prova bytes em ambos os consumidores antes de
#      repontar `current`;
#   4. fica monitorando: se qualquer PID morrer, sai com 1 para o systemd
#      reiniciar o ciclo (Restart=always na unit).
#
# O QUE NÃO FAZ
#   Não troca de geração, não publica, não mexe no tablet, no USB nem na TV.
#   Se a sessão do journal não existir no disco, sai com 3 e não inventa nada.
# =============================================================================
set -uo pipefail

APP_DIR=${APP_DIR:-/opt/usb-stream-tv}
STATE_FILE=${STATE_FILE:-/opt/containers/apps/usb-stream-tv/vod_cache/.mixed_state.json}
ENV_FILE=${ENV_FILE:-$APP_DIR/private/bridge.env}
EXPORT_CONFIG_FILE=${EXPORT_CONFIG_FILE:-$APP_DIR/config/live_favorites_export_tv.json}
SWITCH_ROOT=${SWITCH_ROOT:-$APP_DIR/switch}
UPSTREAM=${UPSTREAM:-https://tv.smre.run.place}
LIVE_PORT=${LIVE_PORT:-18190}
VOD_PORT=${VOD_PORT:-18191}
SWITCH=$APP_DIR/scripts/switch_mixed_generation_vps.sh
POLL=${SUPERVISOR_POLL:-10}

log() { echo "[supervisor] $(date -u +%H:%M:%SZ) $*"; }
die() { log "ERRO: $*"; exit 1; }

[ -f "$SWITCH" ] || die "switch ausente: $SWITCH"
[ -f "$STATE_FILE" ] || die "journal ausente: $STATE_FILE"

# --- 1. sessão ativa pelo journal -------------------------------------------
read_session() {
    python3 - "$STATE_FILE" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print(""); raise SystemExit(0)
print(d.get("active_session") or "")
PY
}

# --- 2. os PIDs registrados vivos E pertencentes à sessão? -------------------
session_is_live() { # <session_dir> <session_name>
    local dir=$1 name=$2 state="$1/state.json"
    [ -f "$state" ] || return 1
    local proxy mgr pid cmd
    proxy=$(sed -n 's/.*"proxy_pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$state" | head -n1)
    mgr=$(sed -n 's/.*"manager_pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$state" | head -n1)
    local any=0
    for pid in "$proxy" "$mgr"; do
        [ -n "$pid" ] || continue
        kill -0 "$pid" 2>/dev/null || return 1
        cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null) || return 1
        [ -n "$cmd" ] || return 1
        # prova de posse, igual ao switch: caminho da sessão ou --session <nome>
        case "$cmd" in
            *"$dir"*) ;;
            *"--session $name"*) ;;
            *) return 1 ;;
        esac
        any=1
    done
    [ "$any" = 1 ]
}

# --- 3. garante a sessão -----------------------------------------------------
ensure_session() {
    local session dir
    session=$(read_session)
    [ -n "$session" ] || { log "journal sem active_session; nada a fazer"; return 3; }
    dir="$SWITCH_ROOT/$session"
    [ -d "$dir" ] || { log "sessão do journal não existe no disco: $dir"; return 3; }

    if session_is_live "$dir" "$session"; then
        log "sessão $session já está viva"
        return 0
    fi

    log "sessão $session NÃO está viva; retomando pelo switch"
    local generation
    generation=$(sed -n 's/.*"generation"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$dir/state.json" 2>/dev/null | head -n1)
    [ -n "$generation" ] || generation=$(python3 -c "
import json,sys
try: print(json.load(open('$STATE_FILE')).get('active_generation') or '')
except Exception: print('')")

    # O switch é idempotente e privilegiado (sudo -n -E preserva SWITCH_ROOT e
    # as credenciais); sem -E ele cairia em $HOME/.mixed-vod com o export errado.
    local out rc
    out=$(sudo -n -E "$SWITCH" start "$dir" "$session" \
            --live-port "$LIVE_PORT" --vod-port "$VOD_PORT" \
            --generation "$generation" \
            --app-dir "$APP_DIR" --upstream "$UPSTREAM" 2>&1)
    rc=$?
    printf '%s\n' "$out" | tail -n 5 | sed 's/^/    /'
    if [ $rc -ne 0 ]; then
        log "switch falhou (rc=$rc) para $session"
        return 1
    fi
    case "$out" in
        *"switch ok"*|*"already_active"*) ;;
        *) log "switch não confirmou sucesso para $session"; return 1 ;;
    esac
    session_is_live "$dir" "$session" || { log "switch disse ok, mas os PIDs não estão vivos"; return 1; }
    log "sessão $session retomada e provada"
    return 0
}

# --- 4. laço de supervisão ---------------------------------------------------
log "iniciando (poll=${POLL}s)"
ensure_session || exit $?

session=$(read_session)
dir="$SWITCH_ROOT/$session"

while :; do
    sleep "$POLL"
    if ! session_is_live "$dir" "$session"; then
        log "PID de $session morreu; saindo para o systemd reiniciar"
        exit 1
    fi
done
