#!/bin/sh
# =============================================================================
# migrate_export_to_tv.sh — migração da grade Live, com prova e volta atrás
#
# Quatro pontos leem o export e todos precisam concordar:
#
#   1. unidade da bridge          --export-config <export>
#   2. unidade da bridge          Environment=EXPORT_CONFIG_FILE=<export>
#      (e o switch, que herda a variável do processo que o chama)
#   3. unidade do prepare/timer   --export-config <export>
#   4. o processo DA BRIDGE EM EXECUÇÃO — um daemon-reload não troca argv nem o
#      ambiente de um processo vivo, então a bridge precisa ser reiniciada.
#      Um publisher lançado por uma bridge velha constrói para o export antigo
#      enquanto o timer já constrói para o novo: hashes diferentes, publicação
#      recusada, Live fora do ar. Por isso o argv/env do PROCESSO REAL são lidos
#      de /proc e conferidos junto com os outros quatro leitores.
#
# Nada aqui publica geração, reinicia a TV, mexe no USB ou reconecta o pendrive.
# A grade nova só vale na publicação seguinte, pelo botão do painel.
#
# Uso:
#   migrate_export_to_tv.sh check     # somente leitura: onde cada leitor está
#   migrate_export_to_tv.sh apply     # migra (exige janela livre) e confere
#   migrate_export_to_tv.sh rollback  # volta as referências do último backup íntegro
#
# QUALQUER falha em apply desfaz TUDO o que o apply fez: unidades restauradas e
# conferidas, systemd recarregado, bridge reiniciada com o export da unidade
# restaurada e timer devolvido ao estado em que estava. Se a volta não puder ser
# comprovada, o script diz isso em vez de alegar sucesso.
#
# Knobs: TARGET PREVIOUS APP_DIR SWITCH_ROOT BRIDGE_UNIT PREPARE_UNIT
#        BRIDGE_SERVICE PREPARE_SERVICE PREPARE_TIMER SYSTEMCTL BACKUP_ROOT
#        MIXED_STATE_FILE MIGRATE_PREPARE_WAIT MIGRATE_ALLOW_UNKNOWN_JOURNAL
# =============================================================================
set -eu

TARGET=${TARGET:-/opt/usb-stream-tv/config/live_favorites_export_tv.json}
PREVIOUS=${PREVIOUS:-/opt/usb-stream-tv/config/generation_g2_export.json}
APP_DIR=${APP_DIR:-/opt/usb-stream-tv}
SWITCH_ROOT=${SWITCH_ROOT:-/opt/usb-stream-tv/switch}
BRIDGE_UNIT=${BRIDGE_UNIT:-/etc/systemd/system/usb-stream-tv-mixed-bridge.service}
PREPARE_UNIT=${PREPARE_UNIT:-/etc/systemd/system/usb-stream-tv-mixed-prepare.service}
BRIDGE_SERVICE=${BRIDGE_SERVICE:-usb-stream-tv-mixed-bridge.service}
PREPARE_SERVICE=${PREPARE_SERVICE:-usb-stream-tv-mixed-prepare.service}
PREPARE_TIMER=${PREPARE_TIMER:-usb-stream-tv-mixed-prepare.timer}
SYSTEMCTL=${SYSTEMCTL:-systemctl}
# Fora do APP_DIR: o dono da aplicacao nao deve poder plantar um backup que o
# root vai consumir no rollback.
BACKUP_ROOT=${BACKUP_ROOT:-/var/lib/usb-stream-tv-migrations}
PROC_ROOT=${PROC_ROOT:-/proc}
PYTHON=${PYTHON:-python3}
# Um pacote de 128 GiB leva minutos: a preparação em voo recebe a mesma ordem de
# grandeza do tempo limite do próprio publisher, e não 60 s.
MIGRATE_PREPARE_WAIT=${MIGRATE_PREPARE_WAIT:-900}
MIGRATE_ALLOW_UNKNOWN_JOURNAL=${MIGRATE_ALLOW_UNKNOWN_JOURNAL:-0}
# A janela e autorizada pelo operador, nunca inferida de telemetria: sem esta
# variavel o apply nao roda. Nada aqui olha "TV ociosa" para decidir.
MIGRATE_CONFIRM_WINDOW=${MIGRATE_CONFIRM_WINDOW:-0}
# Um spool que não pode ser localizado é estado DESCONHECIDO, não "provavelmente
# ocioso": o default é recusar.
MIGRATE_ALLOW_UNKNOWN_SPOOL=${MIGRATE_ALLOW_UNKNOWN_SPOOL:-0}
# Quanto esperar pela janela de coordenação. Espera curta atravessa o instante em
# que a bridge segura o lock para reivindicar um pedido; é curta de propósito,
# porque uma publicação (segundos a 900 s) tem de terminar em RECUSA, nunca em
# bridge parada.
MIGRATE_LOCK_WAIT=${MIGRATE_LOCK_WAIT:-5}
PUBLISHER_SCRIPT=${PUBLISHER_SCRIPT:-mixed_generation_service.py}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR=$BACKUP_ROOT/.backup-export-migrate-$STAMP
BACKUP_DIR_READY=""
TIMER_WAS_ACTIVE=""
JOURNAL_OK=""
LOCK_FILE=""
LOCK_HOLDER_PID=""
LOCK_HOLDER_READY=""

log() { echo "[migrate] $*" >&2; }
die() { log "$*"; exit 1; }

# A signal in the middle of the apply must not leave the bridge or the timer
# stopped: the same undo runs, and the script says what it could not confirm.
SIGNAL_SEEN=""
on_signal() {
    if [ -n "$SIGNAL_SEEN" ]; then
        # A second signal while the undo runs would kill the script half way and
        # leave the bridge or the timer stopped. The undo finishes; the operator
        # is told that the request was repeated.
        log "second signal received: finishing the undo before exiting (ignoring more)"
        return 0
    fi
    SIGNAL_SEEN=1
    # The handler STAYS armed during the recovery: a repeated signal re-enters
    # here and returns without terminating, so the undo is never cut in half.
    if [ -n "$BACKUP_DIR_READY" ]; then
        apply_failed "interrupted by a signal"
    fi
    die "interrupted by a signal before anything was changed"
}
apply_guard_signals() { trap 'on_signal' INT TERM HUP; }
apply_release_signals() { trap - INT TERM HUP; }

# --------------------------------------------------------------- export checks
export_is_usable() { # <path> -> one line summary
    [ -f "$1" ] || return 1
    "$PYTHON" - "$1" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    config = json.loads(open(path).read())
except Exception as exc:
    print("unreadable: %s" % (exc,))
    raise SystemExit(1)
if not isinstance(config, dict) or not isinstance(config.get("identities"), list):
    print("not an export configuration")
    raise SystemExit(1)
approved = [i for i in config["identities"] if i.get("status") == "approved"]
print("schema=%s live_root=%r approved=%d pending=%d"
      % (config.get("schema_version"), config.get("live_root"), len(approved),
         len([p for p in (config.get("pending") or [])])))
PY
}

export_matches_manifest() { # <manifest> <export> -> 0 when they describe one set
    PYTHONPATH="$APP_DIR/scripts:$APP_DIR${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON" - "$1" "$2" <<'PY' >/dev/null 2>&1
import json
import sys
from pathlib import Path

from run_live_favorites_manager import manifest_matches_export

manifest = json.loads(Path(sys.argv[1]).read_text())
config = json.loads(Path(sys.argv[2]).read_text())
if not isinstance(config, dict) or "identities" not in config:
    sys.exit(1)
manifest_matches_export(manifest, Path(sys.argv[2]))
PY
}

# ---------------------------------------------------------------- reader sides
# A unit may name the export more than once (argv lines and Environment lines).
# Every value found is reported; a unit naming two different exports is refused
# instead of being rewritten only in part.
unit_export_values() { # <unit> -> distinct non-empty values, one per line
    [ -f "$1" ] || return 1
    {
        grep -o -- '--export-config [^ ]*' "$1" 2>/dev/null | cut -d' ' -f2
        grep -o 'EXPORT_CONFIG_FILE=[^ ]*' "$1" 2>/dev/null | cut -d= -f2
    } | sed '/^$/d' | sort -u
}

unit_export() { # <unit> -> the single export the unit names, or fail
    [ "$(unit_export_values "$1" | wc -l)" = 1 ] || return 1
    unit_export_values "$1"
}

unit_argv_export() { # <unit> -> export in an argv line
    [ -f "$1" ] || return 1
    grep -o -- '--export-config [^ ]*' "$1" 2>/dev/null | head -1 | cut -d' ' -f2
}

unit_env_export() { # <unit> -> export in an Environment line
    [ -f "$1" ] || return 1
    grep -o 'EXPORT_CONFIG_FILE=[^ ]*' "$1" 2>/dev/null | head -1 | cut -d= -f2
}

spool_path() { # from the unit, then the environment
    _sp_derived=$(grep -o -- '--spool [^ ]*' "$BRIDGE_UNIT" 2>/dev/null \
        | head -1 | cut -d' ' -f2 || true)
    [ -n "$_sp_derived" ] || _sp_derived=${MIXED_BRIDGE_SPOOL:-}
    [ -n "$_sp_derived" ] && [ -d "$_sp_derived" ] || return 1
    printf '%s\n' "$_sp_derived"
}

spool_in_flight() { # prints pending request names, or nothing when unknown/idle
    _si_path=$(spool_path || true)
    [ -n "$_si_path" ] || return 0
    for _si_dir in requests processing; do
        [ -d "$_si_path/$_si_dir" ] || continue
        for _si_file in "$_si_path/$_si_dir"/*.json; do
            [ -e "$_si_file" ] || continue
            printf '%s/%s\n' "$_si_dir" "$(basename "$_si_file")"
        done
    done
}

publisher_processes() { # read-only scan: nobody is signalled
    # A publisher is only the argv the bridge builds (--apply/--refresh/--verify),
    # and its executable must BE the publisher script: matching the mere mention of
    # the file name would flag the operator's own shell, a test runner or an
    # editor holding the file open, and then nothing could ever migrate.
    for _pp_dir in "$PROC_ROOT"/[0-9]*; do
        _pp_pid=${_pp_dir##*/}
        [ "$_pp_pid" = "$$" ] && continue
        [ -r "$_pp_dir/cmdline" ] || continue
        # A PID can vanish between the glob and the read; the subshell keeps that
        # harmless race out of the operator's log.
        _pp_args=$( (tr '\0' '\n' < "$_pp_dir/cmdline") 2>/dev/null ) || continue
        # argv[1] must end with the publisher script name.
        _pp_exec=$(printf '%s\n' "$_pp_args" | sed -n '2p' | sed 's|.*/||')
        [ "$_pp_exec" = "$PUBLISHER_SCRIPT" ] || continue
        case "$_pp_args" in
            *--apply*|*--refresh*|*--verify*) ;;
            *) continue ;;
        esac
        printf '%s %s\n' "$_pp_pid" "$(printf '%s' "$_pp_args" | tr '\n' ' ')"
    done
    return 0
}

# ----------------------------------------------------- janela de coordenação
# A bridge, o publicador e este script compartilham UM arquivo de flock: o
# publicador e a bridge o seguram COMPARTILHADO enquanto uma operação roda, e
# este script o toma EXCLUSIVO pela janela inteira. É isso que torna a exclusão
# atômica — conferir o spool antes e depois do `stop` só DETECTA a corrida depois
# que a publicação já podia ter sido morta por KillMode=control-group.
#
# O arquivo é resolvido do mesmo jeito pelos três (ver publication_lock.py):
#   $MIXED_PUBLICATION_LOCK > <dirname(--state-file)>/publication.lock > <spool>/publication.lock
# e a prova de que bridge e publicador chegam ao MESMO arquivo é feita aqui: se
# as duas derivações divergirem, a migração é RECUSADA (fail-closed), em vez de
# fingir uma exclusão que não existe.
unit_state_file() { # <unit> -> the --state-file that unit names
    [ -f "$1" ] || return 1
    grep -o -- '--state-file [^ ]*' "$1" 2>/dev/null | head -1 | cut -d' ' -f2
}

lock_env_override() { printf '%s' "${MIXED_PUBLICATION_LOCK:-}"; }

bridge_lock_path() { # what the bridge resolves (env > state > spool)
    _bl_env=$(lock_env_override)
    [ -n "$_bl_env" ] && { printf '%s\n' "$_bl_env"; return 0; }
    _bl_state=$(unit_state_file "$BRIDGE_UNIT" || true)
    if [ -n "$_bl_state" ]; then
        printf '%s/publication.lock\n' "$(dirname "$_bl_state")"
        return 0
    fi
    _bl_spool=$(spool_path || true)
    [ -n "$_bl_spool" ] && { printf '%s/publication.lock\n' "$_bl_spool"; return 0; }
    return 1
}

publisher_lock_path() { # what the publisher resolves (env > its --state-file)
    _pl_env=$(lock_env_override)
    [ -n "$_pl_env" ] && { printf '%s\n' "$_pl_env"; return 0; }
    _pl_state=$(unit_state_file "$BRIDGE_UNIT" || true)
    [ -n "$_pl_state" ] || _pl_state=$(unit_state_file "$PREPARE_UNIT" || true)
    [ -n "$_pl_state" ] || return 1
    printf '%s/publication.lock\n' "$(dirname "$_pl_state")"
}

publication_lock_path() { # the file, only when the two derivations agree
    _pq_env=$(lock_env_override)
    if [ -n "$_pq_env" ]; then
        case "$_pq_env" in /*) ;; *) return 1 ;; esac
        printf '%s\n' "$_pq_env"
        return 0
    fi
    _pq_pub=$(publisher_lock_path || true)
    _pq_bridge=$(bridge_lock_path || true)
    [ -n "$_pq_pub" ] || return 1
    [ "$_pq_pub" = "$_pq_bridge" ] || return 1
    # A relative --state-file resolves against each process's own working
    # directory, so the same string could name two different files for the
    # bridge and for this script: refuse instead of pretending they are shared.
    case "$_pq_pub" in /*) ;; *) return 1 ;; esac
    printf '%s\n' "$_pq_pub"
}

acquire_publication_lock() {
    LOCK_FILE=$(publication_lock_path || true)
    if [ -z "$LOCK_FILE" ]; then
        die "refusing: the quiescence lock cannot be proven shared with the publisher and the bridge (set an absolute MIXED_PUBLICATION_LOCK, or give the bridge unit an absolute --state-file); a publication could not be excluded"
    fi
    LOCK_HOLDER_READY=$(mktemp "${TMPDIR:-/tmp}/migrate-lock.XXXXXX") \
        || die "refusing: no temporary file for the lock handshake"
    "$PYTHON" "$APP_DIR/scripts/publication_lock.py" hold --lock "$LOCK_FILE" \
        --exclusive --timeout "$MIGRATE_LOCK_WAIT" --kind migration \
        --ready-file "$LOCK_HOLDER_READY" >/dev/null 2>&1 &
    LOCK_HOLDER_PID=$!
    _al_i=0
    while [ "$_al_i" -lt 400 ]; do
        [ -s "$LOCK_HOLDER_READY" ] && break
        kill -0 "$LOCK_HOLDER_PID" 2>/dev/null || break
        sleep 0.05
        _al_i=$((_al_i + 1))
    done
    _al_msg=$(head -1 "$LOCK_HOLDER_READY" 2>/dev/null || true)
    rm -f "$LOCK_HOLDER_READY"
    LOCK_HOLDER_READY=""
    case "$_al_msg" in
        locked*)
            log "quiescence window held ($LOCK_FILE)"
            return 0 ;;
        busy*)
            kill "$LOCK_HOLDER_PID" 2>/dev/null || true
            wait "$LOCK_HOLDER_PID" 2>/dev/null || true
            LOCK_HOLDER_PID=""
            die "refusing: an operation is active (${_al_msg#busy }); nothing was changed"
            ;;
        *)
            kill "$LOCK_HOLDER_PID" 2>/dev/null || true
            wait "$LOCK_HOLDER_PID" 2>/dev/null || true
            LOCK_HOLDER_PID=""
            die "refusing: the quiescence lock could not be taken at $LOCK_FILE"
            ;;
    esac
}

release_publication_lock() {
    [ -n "$LOCK_HOLDER_PID" ] || return 0
    kill "$LOCK_HOLDER_PID" 2>/dev/null || true
    wait "$LOCK_HOLDER_PID" 2>/dev/null || true
    LOCK_HOLDER_PID=""
}

lock_is_held() {
    [ -n "$LOCK_HOLDER_PID" ] || return 1
    kill -0 "$LOCK_HOLDER_PID" 2>/dev/null
}

require_lock() {
    lock_is_held || die "the quiescence window was lost ($LOCK_FILE): refusing to touch the units"
}

processing_in_flight() { # only the claimed set: a queued request cannot run
    _pi_path=$(spool_path || true)
    [ -n "$_pi_path" ] || return 0
    for _pi_file in "$_pi_path"/processing/*.json; do
        [ -e "$_pi_file" ] || continue
        printf 'processing/%s\n' "$(basename "$_pi_file")"
    done
}

require_no_active_publication() {
    _rnp_claimed=$(processing_in_flight)
    if [ -n "$_rnp_claimed" ]; then
        printf '%s\n' "$_rnp_claimed" >&2
        die "refusing: a request was already claimed (the window should have excluded it)"
    fi
    _rnp_live=$(publisher_processes)
    if [ -n "$_rnp_live" ]; then
        printf '%s\n' "$_rnp_live" >&2
        # Same condition the stop check reports, with the extra detail: this one
        # also means the publisher is using another lock file (foreign
        # --state-file), so the window cannot exclude it.
        die "refusing to stop the bridge: a publisher is already running (outside the shared lock: foreign --state-file?); reconcile before migrating"
    fi
    return 0
}

bridge_pid() {
    "$SYSTEMCTL" show -p MainPID --value "$BRIDGE_SERVICE" 2>/dev/null | head -1
}

process_export() { # <pid> -> export in the running argv
    case "${1:-}" in ''|0) return 1 ;; esac
    [ -r "$PROC_ROOT/$1/cmdline" ] || return 1
    tr '\0' ' ' < "$PROC_ROOT/$1/cmdline" | sed -n 's/.*--export-config \([^ ]*\).*/\1/p'
}

process_env_export() { # <pid> -> EXPORT_CONFIG_FILE of the running process
    case "${1:-}" in ''|0) return 1 ;; esac
    [ -r "$PROC_ROOT/$1/environ" ] || return 1
    tr '\0' '\n' < "$PROC_ROOT/$1/environ" | sed -n 's/^EXPORT_CONFIG_FILE=//p' | head -1
}

timer_is_active() {
    "$SYSTEMCTL" is-active --quiet "$PREPARE_TIMER" 2>/dev/null
}

prepare_is_active() {
    "$SYSTEMCTL" is-active --quiet "$PREPARE_SERVICE" 2>/dev/null
}

# The five readers: the four file references plus the live process, which is the
# one a daemon-reload does not move. Quiet: prints a verdict, never noise.
readers_of() { # <export> -> "ok" or "bad <names>"
    _rd_bad=""
    _rd_pid=$(bridge_pid || true)
    for _rd_pair in \
        "bridge-unit(argv):$(unit_argv_export "$BRIDGE_UNIT" || true)" \
        "bridge-unit(env):$(unit_env_export "$BRIDGE_UNIT" || true)" \
        "prepare-unit(argv):$(unit_argv_export "$PREPARE_UNIT" || true)" \
        "bridge-process(argv):$(process_export "${_rd_pid:-}" || true)" \
        "bridge-process(env):$(process_env_export "${_rd_pid:-}" || true)"
    do
        _rd_name=${_rd_pair%%:*}
        _rd_value=${_rd_pair#*:}
        [ "$_rd_value" = "$1" ] || _rd_bad="$_rd_bad $_rd_name"
    done
    if [ -n "$_rd_bad" ]; then
        printf 'bad%s\n' "$_rd_bad"
        return 1
    fi
    printf 'ok\n'
}

report_references() {
    _rr_pid=$(bridge_pid || true)
    log "1. bridge unit --export-config        : $(unit_argv_export "$BRIDGE_UNIT" || echo '<none>')"
    log "2. bridge unit EXPORT_CONFIG_FILE     : $(unit_env_export "$BRIDGE_UNIT" || echo '<none>')"
    log "3. prepare unit --export-config       : $(unit_argv_export "$PREPARE_UNIT" || echo '<none>')"
    log "4. bridge RUNNING pid ${_rr_pid:-<none>} argv    : $(process_export "${_rr_pid:-}" || echo '<none>')"
    log "4b. bridge RUNNING env                : $(process_env_export "${_rr_pid:-}" || echo '<none>')"
}

# ------------------------------------------------------------- journal (state)
# The state file path comes from the unit that runs the publisher, so the script
# reads the journal the services really use instead of a guess.
state_file_path() {
    if [ -n "${MIXED_STATE_FILE:-}" ] && [ -f "$MIXED_STATE_FILE" ]; then
        printf '%s\n' "$MIXED_STATE_FILE"
        return 0
    fi
    _sf_from_unit=$(grep -o -- '--state-file [^ ]*' "$BRIDGE_UNIT" 2>/dev/null \
        | head -1 | cut -d' ' -f2 || true)
    if [ -z "$_sf_from_unit" ]; then
        _sf_from_unit=$(grep -o -- '--state-file [^ ]*' "$PREPARE_UNIT" 2>/dev/null \
            | head -1 | cut -d' ' -f2 || true)
    fi
    [ -n "$_sf_from_unit" ] && [ -f "$_sf_from_unit" ] || return 1
    printf '%s\n' "$_sf_from_unit"
}

journal_report() { # <export> -> 0 when the journalled generation could be read
    _jr_state=$(state_file_path || true)
    if [ -z "$_jr_state" ]; then
        JOURNAL_OK=""
        log "pending: JOURNAL NOT READABLE (no --state-file in the units and no"
        log "         readable MIXED_STATE_FILE): it is impossible to say whether"
        log "         the journalled generation matches $1"
        return 1
    fi
    log "pending: journal read from $_jr_state"
    APP_SCRIPTS=$APP_DIR/scripts \
        PYTHONPATH="$APP_DIR/scripts:$APP_DIR${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON" - "$_jr_state" "$1" <<'PY'
import json
import os
import sys

try:
    state = json.loads(open(sys.argv[1]).read())
except Exception as exc:
    print("pending layout: NOT COMPARABLE (journal unreadable: %s)" % (exc,))
    raise SystemExit(3)
export_path = sys.argv[2]
try:
    staging = state.get("pending_staging_dir")
except AttributeError:
    print("pending layout: NOT COMPARABLE (journal is not an object)")
    raise SystemExit(3)
if not staging:
    # Nothing prepared is a legitimate state (a publication consumed the pending,
    # or a fresh installation): there is nothing to compare, and that is not an
    # inconsistency.
    print("no pending package journalled; nothing to compare")
    raise SystemExit(0)
manifest = None
if staging:
    try:
        manifest = json.load(open(os.path.join(staging, "favorites_manifest.json")))
    except Exception:
        manifest = None
live = []
if manifest:
    live = sorted("%s/%s" % ("/".join(e["folder"]), e["filename"])
                  for e in manifest["entries"] if e.get("kind") == "live")
print("pending=%s active=%s session=%s live=%s"
      % (state.get("pending_generation"), state.get("active_generation"),
         state.get("pending_session"), live))
if manifest is None:
    print("pending layout: UNREADABLE, cannot say whether it matches %s" % export_path)
    raise SystemExit(3)
try:
    from build_mixed_media_package import process_live_identities
    config = json.load(open(export_path))
    expected = sorted("%s/%s" % ("/".join(e["folder"]), e["filename"])
                      for e in process_live_identities(config))
except Exception as exc:
    print("pending layout: NOT COMPARABLE with %s (%s)" % (export_path, exc))
    raise SystemExit(3)
if expected == live:
    print("pending layout already matches %s: it can be published as it is" % export_path)
else:
    print("pending layout STALE for %s: expected %s, journalled %s"
          % (export_path, expected, live))
    print("do NOT publish this package: let the next preparation rebuild it "
          "(the dashboard button does that; --publish would transfer it as is)")
PY
    _jr_rc=$?
    # Every non-zero outcome blocks, not just the code this script mints: a
    # traceback (corrupted JSON, unexpected shape) is exactly the case where the
    # comparison did not happen.
    if [ "$_jr_rc" -ne 0 ]; then
        # Comparable is not optional: without it the apply cannot say whether the
        # journalled package belongs to the old export. Fail closed.
        JOURNAL_OK=""
        log "pending: the journalled package CANNOT BE COMPARED with $1 (rc=$_jr_rc)"
        return 1
    fi
    JOURNAL_OK=1
    return 0
}

# ------------------------------------------- active session (rollback preflight)
# A migration is only reversible when the session running right now can be proven
# to belong to an export that still exists on disk: that is what the switch does
# when it restores a generation. Without that proof the documented rollback would
# start the old manager with the NEW export and Live would go down.
active_session_dir() {
    [ -e "$SWITCH_ROOT/current" ] || return 1
    _as_dir=$(readlink "$SWITCH_ROOT/current" 2>/dev/null || cat "$SWITCH_ROOT/current")
    [ -n "$_as_dir" ] || return 1
    case "$_as_dir" in
        /*) printf '%s\n' "$_as_dir" ;;
        *) printf '%s\n' "$SWITCH_ROOT/$_as_dir" ;;
    esac
}

# The same candidate order the switch uses when it resolves a session's export
# (caller's variable first, then the config glob), so the migration proves the
# restore with the export the switch would actually pick.
active_session_proof() {
    _ap_dir=$(active_session_dir || true)
    if [ -z "$_ap_dir" ] || [ ! -f "$_ap_dir/favorites_manifest.json" ]; then
        log "active session: not found under $SWITCH_ROOT (no readable current)"
        return 1
    fi
    for _ap_cand in ${EXPORT_CONFIG_FILE:-} "$APP_DIR"/config/*.json; do
        [ -f "$_ap_cand" ] || continue
        if export_matches_manifest "$_ap_dir/favorites_manifest.json" "$_ap_cand"; then
            ACTIVE_SESSION_EXPORT=$_ap_cand
            log "active session $(basename "$_ap_dir") restores with $_ap_cand"
            log "  (registered in the backup as active_session_export)"
            return 0
        fi
    done
    log "active session $(basename "$_ap_dir") matches NO export on disk:"
    log "  restoring it after a migration could not be proven -> refusing"
    return 1
}

# --------------------------------------------------------------------- backup
backup_units() {
    _bu_tmp=$BACKUP_DIR.tmp
    rm -rf "$_bu_tmp"
    # The root is created 0700 and must not be a symlink: a backup the rollback is
    # about to trust as root must not be reachable by another account.
    [ -L "$BACKUP_ROOT" ] && { log "$BACKUP_ROOT is a symlink"; return 1; }
    for _bu_src in "$BRIDGE_UNIT" "$PREPARE_UNIT"; do
        [ -L "$_bu_src" ] && { log "$_bu_src is a symlink: refusing to back it up"; return 1; }
    done
    mkdir -p "$BACKUP_ROOT" || return 1
    chmod 700 "$BACKUP_ROOT" || return 1
    mkdir -p "$_bu_tmp" || return 1
    chmod 700 "$_bu_tmp" || return 1
    cp -a "$BRIDGE_UNIT" "$_bu_tmp/bridge.service" || return 1
    cp -a "$PREPARE_UNIT" "$_bu_tmp/prepare.service" || return 1
    [ -s "$_bu_tmp/bridge.service" ] && [ -s "$_bu_tmp/prepare.service" ] || return 1
    [ -n "$ACTIVE_SESSION_EXPORT" ] \
        && printf '%s\n' "$ACTIVE_SESSION_EXPORT" > "$_bu_tmp/active_session_export"
    report_references > "$_bu_tmp/references.before" 2>&1 || true
    # The marker is written last and the directory is renamed in one step: a
    # half-written backup can never be picked up by a rollback.
    printf 'complete %s\n' "$STAMP" > "$_bu_tmp/COMPLETE" || return 1
    mv "$_bu_tmp" "$BACKUP_DIR" || return 1
    log "backup in $BACKUP_DIR"
}

complete_backup_dirs() { # newest first, only complete and trustworthy ones
    for _cb_dir in $(ls -dt "$BACKUP_ROOT"/.backup-export-migrate-* 2>/dev/null); do
        [ -L "$_cb_dir" ] && continue
        [ -f "$_cb_dir/COMPLETE" ] || continue
        [ -s "$_cb_dir/bridge.service" ] || continue
        [ -s "$_cb_dir/prepare.service" ] || continue
        # Regular files, not symlinks, owned by the unit's own account (or root):
        # this content is written into the unit directory by the rollback, so a
        # backup planted by another account must never be accepted.
        _cb_ok=1
        for _cb_file in "$_cb_dir/bridge.service" "$_cb_dir/prepare.service"; do
            [ -L "$_cb_file" ] && { _cb_ok=""; break; }
            _cb_target=$BRIDGE_UNIT
            case "$_cb_file" in *prepare.service) _cb_target=$PREPARE_UNIT ;; esac
            _cb_owner=$(stat -c '%u' "$_cb_file" 2>/dev/null)
            _cb_allowed=$(stat -c '%u' "$_cb_target" 2>/dev/null)
            if [ "$_cb_owner" != "0" ] && [ "$_cb_owner" != "$_cb_allowed" ]; then
                _cb_ok=""
                break
            fi
        done
        [ -n "$_cb_ok" ] || continue
        printf '%s\n' "$_cb_dir"
    done
}

restore_units() { # <backup dir>: every step checked, never claims partial success
    _ru_dir=$1
    [ -f "$_ru_dir/bridge.service" ] || { log "backup has no bridge.service"; return 1; }
    [ -f "$_ru_dir/prepare.service" ] || { log "backup has no prepare.service"; return 1; }
    for _ru_file in "$_ru_dir/bridge.service" "$_ru_dir/prepare.service"; do
        [ -L "$_ru_file" ] && { log "$_ru_file is a symlink"; return 1; }
    done
    cp -a "$_ru_dir/bridge.service" "$BRIDGE_UNIT" || { log "could not restore the bridge unit"; return 1; }
    cp -a "$_ru_dir/prepare.service" "$PREPARE_UNIT" || { log "could not restore the prepare unit"; return 1; }
    "$SYSTEMCTL" daemon-reload || { log "systemd refused to reload after the restore"; return 1; }
    _ru_expected=$(unit_export "$BRIDGE_UNIT" || true)
    [ -n "$_ru_expected" ] || { log "restored bridge unit names no single export"; return 1; }
    log "units restored from $_ru_dir and systemd reloaded"
    return 0
}

# Stopping the bridge must not cut a publication in half. The bridge renames a
# request into processing/ before spawning the publisher (child process), and the
# unit's default KillMode=control-group kills that child too. So the stop is only
# accepted when, right before it, nothing is in flight, and after it the spool is
# empty AND no publisher process survives: otherwise the publication state is
# unknown and the migration must not proceed.
stop_bridge_verified() {
    _sb_pre=$(spool_in_flight)
    if [ -n "$_sb_pre" ]; then
        log "refusing to stop the bridge: work in flight in the spool:"
        printf '%s\n' "$_sb_pre" >&2
        return 1
    fi
    _sb_live=$(publisher_processes)
    if [ -n "$_sb_live" ]; then
        log "refusing to stop the bridge: a publisher is already running:"
        printf '%s\n' "$_sb_live" >&2
        return 1
    fi
    "$SYSTEMCTL" stop "$BRIDGE_SERVICE" >/dev/null 2>&1 || {
        log "the bridge stop failed"
        return 1
    }
    _sb_wait=0
    while [ "$_sb_wait" -lt 30 ]; do
        _sb_active=$("$SYSTEMCTL" is-active --quiet "$BRIDGE_SERVICE" 2>/dev/null && echo yes || echo no)
        [ "$_sb_active" = "no" ] && break
        _sb_wait=$((_sb_wait + 1))
        sleep 1
    done
    _sb_after=$(spool_in_flight)
    if [ -n "$_sb_after" ]; then
        log "the spool still holds a claimed request after the stop:"
        printf '%s\n' "$_sb_after" >&2
        log "a publication may have been interrupted; reconcile before publishing"
        return 1
    fi
    _sb_live_after=$(publisher_processes)
    if [ -n "$_sb_live_after" ]; then
        log "a publisher survived the bridge stop:"
        printf '%s\n' "$_sb_live_after" >&2
        return 1
    fi
    log "bridge stopped with the spool idle and no publisher running"
    return 0
}

restart_bridge() { # <export the running process must end up with>
    _rb_export=$1
    "$SYSTEMCTL" restart "$BRIDGE_SERVICE" >/dev/null 2>&1 || return 1
    _rb_pid=$(bridge_pid || true)
    _rb_proc=$(process_export "${_rb_pid:-}" || true)
    [ "$_rb_proc" = "$_rb_export" ] || {
        log "bridge restarted but its argv says '${_rb_proc:-<none>}'"
        return 1
    }
    _rb_env=$(process_env_export "${_rb_pid:-}" || true)
    [ "$_rb_env" = "$_rb_export" ] || {
        log "bridge restarted but its environment says '${_rb_env:-<none>}'"
        return 1
    }
    log "bridge pid ${_rb_pid} now runs with $_rb_export"
}

# Every mutation the apply performs is undone here, including the timer.
undo_migration() {
    _um_failed=""
    if [ -n "$BACKUP_DIR_READY" ]; then
        restore_units "$BACKUP_DIR" || _um_failed="restore"
    else
        _um_failed="no-backup"
    fi
    _um_expected=""
    [ -z "$_um_failed" ] && _um_expected=$(unit_export "$BRIDGE_UNIT" || true)
    [ -n "$_um_expected" ] || _um_expected=$PREVIOUS
    if ! restart_bridge "$_um_expected"; then
        _um_failed="$_um_failed bridge"
    fi
    # The timer was stopped by the apply: put it back exactly as it was.
    if [ -n "$TIMER_WAS_ACTIVE" ]; then
        if "$SYSTEMCTL" start "$PREPARE_TIMER" >/dev/null 2>&1 && timer_is_active; then
            log "timer $PREPARE_TIMER restarted"
        else
            log "TIMER $PREPARE_TIMER COULD NOT BE RESTARTED (it was active before)"
            _um_failed="$_um_failed timer"
        fi
    fi
    # "Verified" may only be claimed when the whole set agrees. The state before
    # the apply could already have been inconsistent (the old D8 left exactly
    # that); restoring it is not the same as making it coherent, and saying so
    # would be a lie the operator cannot see.
    _um_readers=$(readers_of "$_um_expected" || true)
    if [ "$_um_readers" != "ok" ]; then
        log "after the undo the readers still disagree:$_um_readers"
        log "  the state was like this before the apply; this script did not create it"
        _um_failed="$_um_failed readers"
    fi
    [ -z "$_um_failed" ] && return 0
    log "undo incomplete:$_um_failed"
    return 1
}

apply_failed() {
    _af_reason=$*
    # Signals stay trapped until the process exits: releasing them here is what
    # let a second signal kill the script in the middle of the undo.
    log "apply failed: $_af_reason"
    if undo_migration; then
        release_publication_lock
        log "everything the apply changed was undone and verified"
        die "migration reverted; the previous configuration is in place"
    fi
    release_publication_lock
    log "RESTORE FAILED: the previous state could not be fully restored"
    log "the state may need a human: read the lines above before publishing"
    die "migration reverted with errors reported above"
}

# ---------------------------------------------------------------------- check
cmd_check() {
    log "target  : $TARGET"
    if _ck_sum=$(export_is_usable "$TARGET"); then
        log "target ok: $_ck_sum"
    else
        log "target NOT usable: ${_ck_sum:-missing file} (apply would refuse)"
    fi
    if export_is_usable "$PREVIOUS" >/dev/null 2>&1; then
        log "previous: $PREVIOUS (usable, kept for rollback)"
    else
        log "previous: $PREVIOUS (MISSING: apply would refuse)"
    fi
    echo
    report_references
    echo
    journal_report "$TARGET" || true
    echo
    active_session_proof || true
    echo
    _ck_lock=$(publication_lock_path || true)
    if [ -n "$_ck_lock" ]; then
        _ck_state=$("$PYTHON" "$APP_DIR/scripts/publication_lock.py" state --lock "$_ck_lock" 2>/dev/null || true)
        log "quiescence lock : $_ck_lock (${_ck_state:-unknown})"
    else
        log "quiescence lock : NOT PROVABLE (no MIXED_PUBLICATION_LOCK and no"
        log "                  --state-file in the units): apply would refuse"
    fi
    echo
    if [ "$(readers_of "$TARGET" || true)" = "ok" ]; then
        log "verdict: every reader already uses the target export"
    elif [ "$(readers_of "$PREVIOUS" || true)" = "ok" ]; then
        log "verdict: consistent on the previous export; migration is pending"
    else
        log "verdict: INCONSISTENT -- readers disagree, do not publish"
        log "  target   $(readers_of "$TARGET" || true)"
        log "  previous $(readers_of "$PREVIOUS" || true)"
        return 1
    fi
    if [ -z "$JOURNAL_OK" ]; then
        log "verdict: journal unreadable -- an apply would refuse without"
        log "         MIGRATE_ALLOW_UNKNOWN_JOURNAL=1"
    fi
}

# ---------------------------------------------------------------------- apply
rewrite_units() { # literal rewrite; refuses a unit naming several exports
    PYTHONPATH="$APP_DIR/scripts:$APP_DIR${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON" - "$BRIDGE_UNIT" "$PREPARE_UNIT" "$PREVIOUS" "$TARGET" <<'PY'
import os
import pathlib
import stat
import sys

bridge, prepare, previous, target = sys.argv[1:5]
for path in (bridge, prepare):
    text = pathlib.Path(path).read_text()
    values = set()
    for line in text.splitlines():
        if "--export-config " in line:
            values.add(line.split("--export-config ", 1)[1].split()[0])
        if "EXPORT_CONFIG_FILE=" in line:
            values.add(line.split("EXPORT_CONFIG_FILE=", 1)[1].split()[0])
    values.discard("")
    if len(values) > 1:
        print("unit names several exports: %s" % sorted(values), file=sys.stderr)
        raise SystemExit(1)
    if not values:
        print("unit names no export: %s" % path, file=sys.stderr)
        raise SystemExit(1)
    if previous not in text and target not in text:
        print("unit names neither %s nor %s: %s" % (previous, target, path),
              file=sys.stderr)
        raise SystemExit(1)
    # Literal replacement: paths contain dots and & would be a sed backreference.
    updated = text.replace("--export-config %s" % previous,
                           "--export-config %s" % target)
    updated = updated.replace("EXPORT_CONFIG_FILE=%s" % previous,
                              "EXPORT_CONFIG_FILE=%s" % target)
    if updated == text:
        print("%s already named %s" % (pathlib.Path(path).name, target))
        continue
    unit = pathlib.Path(path)
    info = unit.stat()
    # A unit is read by systemd from a live path: a truncated write would leave a
    # broken service behind. Same directory, same mode, replaced in one step.
    tmp = unit.with_name("%s.migrate-tmp.%d" % (unit.name, os.getpid()))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                 stat.S_IMODE(info.st_mode))
    try:
        os.write(fd, updated.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chown(str(tmp), info.st_uid, info.st_gid)
    except PermissionError:
        pass
    try:
        os.replace(str(tmp), str(unit))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    dir_fd = os.open(str(unit.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)          # the rename itself must survive a crash
    finally:
        os.close(dir_fd)
    print("%s -> %s (atomic, mode %04o preserved)"
          % (unit.name, target, stat.S_IMODE(unit.stat().st_mode)))
PY
}

cmd_apply() {
    [ "$(id -u)" = 0 ] || die "run as root"
    [ -f "$TARGET" ] || die "target export missing: $TARGET"
    [ -f "$PREVIOUS" ] || die "previous export missing: $PREVIOUS (rollback would be impossible)"
    _ap_sum=$(export_is_usable "$TARGET") || die "target export is not usable: $_ap_sum"
    log "target ok: $_ap_sum"
    if [ "$(readers_of "$TARGET" || true)" = "ok" ]; then
        log "already migrated; nothing to do"
        return 0
    fi
    if ! export_is_usable "$PREVIOUS" >/dev/null 2>&1; then
        die "previous export is not usable: $PREVIOUS"
    fi
    # The window is the operator's call: this script never decides that the TV is
    # free, and no telemetry is consulted to guess it.
    if [ "$MIGRATE_CONFIRM_WINDOW" != 1 ]; then
        die "refusing: run with MIGRATE_CONFIRM_WINDOW=1 to confirm the TV window is free"
    fi
    # Nothing may be in flight: restarting the bridge while a publisher request is
    # being processed would cut a publication in half.
    _ap_in_flight=$(spool_in_flight)
    if [ -n "$_ap_in_flight" ]; then
        log "refusing: the bridge spool has work in flight:"
        printf '%s\n' "$_ap_in_flight" >&2
        die "wait for it to finish (or check the bridge) and try again"
    fi
    _ap_spool=$(spool_path || true)
    if [ -n "$_ap_spool" ]; then
        log "spool idle ($_ap_spool)"
    elif [ "$MIGRATE_ALLOW_UNKNOWN_SPOOL" = 1 ]; then
        log "warning: no spool path found and MIGRATE_ALLOW_UNKNOWN_SPOOL=1:"
        log "         an in-flight publication is NOT ruled out"
    else
        die "refusing: the bridge spool could not be located (no --spool in the unit, no MIXED_BRIDGE_SPOOL); override only with MIGRATE_ALLOW_UNKNOWN_SPOOL=1"
    fi
    # Preflight: the migration is only worth doing if it can be undone. The
    # session running now must be provable by the switch against an export that
    # still exists, otherwise a rollback would hand the old manifest to a manager
    # started with the new export.
    if ! active_session_proof; then
        die "refusing: the active session's export cannot be proven (rollback would not be safe)"
    fi
    # Preflight: without a readable journal the script cannot tell whether the
    # journalled package was built for the old export. Fail closed.
    # Not silenced: the operator has to see WHY it could not be established
    # (unreadable or not comparable), not just that the script refused.
    if ! journal_report "$TARGET"; then
        [ "$MIGRATE_ALLOW_UNKNOWN_JOURNAL" = 1 ] \
            || die "refusing: the journalled package could not be established (see above); override only with MIGRATE_ALLOW_UNKNOWN_JOURNAL=1"
    fi
    # A janela de coordenação vem ANTES de qualquer mutação e é o que fecha a
    # corrida residual: enquanto ela está aberta nenhum pedido pode ser
    # REIVINDICADO (a bridge não pega o lock compartilhado), então não existe
    # publicação em voo para o `stop` matar. Se algo já estiver publicando, o
    # pedido de janela falha e esta migração recusa.
    acquire_publication_lock
    require_no_active_publication

    backup_units || die "could not write a complete backup; nothing was changed"
    BACKUP_DIR_READY=1
    apply_guard_signals

    # Quiesce before touching anything: a prepare run in flight could build with
    # a half-migrated configuration, and the bridge would publish with old args.
    if timer_is_active; then
        TIMER_WAS_ACTIVE=1
        "$SYSTEMCTL" stop "$PREPARE_TIMER" >/dev/null 2>&1 || log "timer already stopped"
    else
        log "timer $PREPARE_TIMER was not active; it will be left as it is"
    fi
    _ap_wait=0
    while prepare_is_active; do
        _ap_wait=$((_ap_wait + 1))
        if [ "$_ap_wait" -ge "$MIGRATE_PREPARE_WAIT" ]; then
            apply_failed "a prepare run is still active after ${MIGRATE_PREPARE_WAIT}s"
        fi
        sleep 1
    done
    # Re-checked here, immediately before the stop: a request may have arrived
    # while the timer was quiescing. The window must still be ours.
    require_lock
    stop_bridge_verified \
        || apply_failed "the bridge could not be stopped safely (publication in flight?)"

    require_lock
    rewrite_units || apply_failed "could not rewrite the units"
    for _ap_unit in "$BRIDGE_UNIT" "$PREPARE_UNIT"; do
        [ "$(unit_export "$_ap_unit" || true)" = "$TARGET" ] \
            || apply_failed "$(basename "$_ap_unit") does not name exactly $TARGET after the rewrite"
    done
    "$SYSTEMCTL" daemon-reload || apply_failed "systemd refuses to reload"

    restart_bridge "$TARGET" || apply_failed "bridge did not come up with the new export"
    _ap_readers=$(readers_of "$TARGET" || true)
    [ "$_ap_readers" = "ok" ] || apply_failed "readers disagree after the change:$_ap_readers"

    if [ -n "$TIMER_WAS_ACTIVE" ]; then
        "$SYSTEMCTL" start "$PREPARE_TIMER" >/dev/null 2>&1 \
            || apply_failed "timer did not start"
        timer_is_active || apply_failed "timer started but is not active"
    fi

    apply_release_signals
    release_publication_lock
    log "migrated: units, running process, environment and timer agree on $TARGET"
    echo
    journal_report "$TARGET" || true
    echo
    log "publish through the dashboard button: it prepares again before publishing."
    log "do NOT use --publish: that would transfer the journalled package as it is,"
    log "and a package built for the previous export would leave Live down."
}

# ------------------------------------------------------------------- rollback
cmd_rollback() {
    [ "$(id -u)" = 0 ] || die "run as root"
    _ro_dir=$(complete_backup_dirs | head -1)
    [ -n "$_ro_dir" ] || die "no complete migration backup under $BACKUP_ROOT"
    log "reverting from $_ro_dir"
    # A volta também reescreve unidades e reinicia a bridge: a mesma janela
    # exclusiva vale aqui, pelo mesmo motivo.
    acquire_publication_lock
    require_no_active_publication
    # The export that this backup proved for the active session is the one the
    # switch must use. Recorded for the operator AND verified here: a rollback
    # with an export that no longer matches the session would take Live down.
    _ro_proven=""
    if [ -f "$_ro_dir/active_session_export" ]; then
        _ro_proven=$(cat "$_ro_dir/active_session_export")
        if [ ! -f "$_ro_proven" ]; then
            die "the export recorded for the active session is gone: $_ro_proven (refusing to roll back)"
        fi
        _ro_dir_active=$(active_session_dir || true)
        if [ -n "$_ro_dir_active" ] && [ -f "$_ro_dir_active/favorites_manifest.json" ]; then
            export_matches_manifest "$_ro_dir_active/favorites_manifest.json" "$_ro_proven" \
                || die "the recorded export no longer matches the active session manifest: $_ro_proven"
            log "active session still matches the recorded export"
        else
            log "warning: the active session directory is not readable; the recorded export was kept as is"
        fi
    else
        log "warning: this backup recorded no active-session export (older backup)"
    fi
    restore_units "$_ro_dir" || die "could not restore from $_ro_dir"
    _ro_expected=$(unit_export "$BRIDGE_UNIT" || true)
    [ -n "$_ro_expected" ] || die "restored bridge unit names no single export"
    if restart_bridge "$_ro_expected"; then
        log "bridge is back on the export the restored unit names"
    else
        log "WARNING: the bridge did not restart with the export of the restored unit"
    fi
    if [ -n "$(complete_backup_dirs | tail -n +2 | head -1)" ]; then
        log "an older complete backup still exists if this one must be undone"
    fi
    report_references
    _ro_readers=$(readers_of "$_ro_expected" || true)
    [ "$_ro_readers" = "ok" ] || die "after the rollback the readers disagree:$_ro_readers"
    # Leaving the preparation timer stopped would silently freeze new generations.
    if timer_is_active; then
        log "timer $PREPARE_TIMER was already active"
    else
        if "$SYSTEMCTL" start "$PREPARE_TIMER" >/dev/null 2>&1 && timer_is_active; then
            log "timer $PREPARE_TIMER restarted after the rollback"
        else
            log "TIMER $PREPARE_TIMER IS STOPPED and could not be started: fix it by hand"
        fi
    fi
    if [ -n "$_ro_proven" ]; then
        log "for the generation switch, use the export this backup proved:"
        log "  EXPORT_CONFIG_FILE=$_ro_proven sh $APP_DIR/scripts/switch_mixed_generation_vps.sh stop <session>"
    fi
    log "rollback done: publish again for the previous export to take effect"
    exit 0
}

# A janela de coordenação fecha em TODA saída, inclusive no `die` de um handler
# de sinal: o processo que a segura é nosso e morre com este script. Instalado
# só aqui, depois de todas as funções acima e antes de qualquer despacho, para
# que uma saída precoce (`die` de uso, alvo ausente) não encontre o handler
# indefinido e não troque o código de saída do script.
trap 'release_publication_lock' EXIT

case "${1:-check}" in
    check) cmd_check ;;
    apply) cmd_apply ;;
    rollback) cmd_rollback ;;
    *) die "usage: $0 check|apply|rollback" ;;
esac
