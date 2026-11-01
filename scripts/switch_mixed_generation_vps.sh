#!/bin/sh
# =============================================================================
# switch_mixed_generation_vps.sh
#
# Reconfigure BOTH server-side consumers of a mixed-media generation (the Live
# favorites manager and the VOD range proxy) as one rollback-able set, *before*
# the tablet LUN is switched.
#
# Why this exists: a generation is not only the tablet image. The VOD proxy
# keeps an in-memory allowlist and the Live manager keeps the manifest id +
# session it accepts on OPEN. If only the tablet is switched, the new VOD task
# 404s on the old allowlist and the manager rejects OPEN for the new
# manifest/session. Both consumers must therefore be replaced first, with the
# previous session recorded so stop can roll the pair back.
#
# Usage:
#   switch_mixed_generation_vps.sh start   <GEN_DIR> <SESSION> [options]
#   switch_mixed_generation_vps.sh stop    <SESSION>
#   switch_mixed_generation_vps.sh status  [<SESSION>]
#
# start options:
#   --live-port N            loopback port for the Live manager (default 18190)
#   --vod-port N             loopback port for the VOD proxy   (default 18191)
#   --generation N           expected manifest generation (must match)
#   --upstream URL           VOD origin (default http://127.0.0.1:8080)
#   --app-dir DIR            deployed app dir (default $APP_DIR or
#                            /opt/containers/apps/usb-stream-tv)
#   --channel-id ID --live-file-id N --live-epoch N
#                            prove the Live byte path after the switch
#   --supersede SESSION      stop that session's own proven processes first
#                            (shared ports) and restore it if this start fails
#   --manager-cmd 'CMD'      override the manager invocation (default built
#                            from --app-dir, see _default_manager_cmd)
#
# start order (artifact validation is always first, nothing is created before
# it passes):
#   1. every GEN_DIR artifact must exist and the manifest/allowlist contract
#      must hold (mixed_generation_contract.validate_generation, imported by
#      python3 with the generation + manifest_id read from the manifest).
#   2. a private per-session dir (0700) under $SWITCH_ROOT/<SESSION> receives
#      0600 copies of the four artifacts, so the services read a stable path
#      and never a staging directory that may be rewritten under them.
#   3. an already-live session is a no-op ("already_active").
#   4. --supersede stops ONLY the processes that session's own state.json
#      records and whose /proc/<pid>/cmdline proves they belong to it, so the
#      candidate can bind the shared ports. The superseded session is recorded
#      as the previous one, so `stop <SESSION>` — and any failure below —
#      brings it back exactly one level deep. Superseding is refused unless it
#      is the session `current` actually points at.
#   5. the VOD proxy (start_mixed_vod_proxy_vps.sh) and the Live manager are
#      started in the background, each with its own log, each pid recorded.
#      No killall/pkill is ever used.
#   6. BOTH consumers must prove real bytes: GET /probe on the proxy (bearer
#      token from a 0600 header file, never on a command line) and, when the
#      live identities are given, probe_mixed_live_endpoint.py printing
#      "ok": true. The proxy is only trusted once its own argv shows it has
#      exec'd the current generation and the /probe answer names a task of the
#      new allowlist, so a listener still serving the previous generation can
#      never be mistaken for ours. On failure only the pids this run started
#      are stopped and the previous session state is left untouched.
#   7. $SWITCH_ROOT/<SESSION>/state.json (0600) is written and
#      $SWITCH_ROOT/current is repointed atomically to the new session.
#   8. one machine-readable line is printed:
#         switch ok generation=<g> session=<s> live_port=<p> vod_port=<p>
#
# stop: reads <SESSION>/state.json, terminates ONLY the recorded pids (TERM,
# then KILL after a bounded wait; each pid's /proc/<pid>/cmdline must prove it
# belongs to this session first, so a reused pid is never killed), removes the
# session lease, and restores the recorded previous session exactly one level
# deep. Safe to run twice (the lease is the one-shot guard).
#
# Notes:
#   * The proxy launcher is started as
#       sh start_mixed_vod_proxy_vps.sh "switch-session=<session dir>"
#     The launcher reads APP_DIR/PKG_DIR/ENV_FILE/PORT/UPSTREAM from the
#     environment and ignores positional arguments; the tag is what makes the
#     launcher's own argv identify the session while it is still validating the
#     origin (it is gone once the launcher execs the proxy).
#   * Default Live manager (used without --manager-cmd; requires
#     $APP_DIR/scripts/run_live_favorites_manager.py, $LIVE_CACHE_FILE and
#     $EXPORT_CONFIG_FILE):
#       python3 $APP_DIR/scripts/run_live_favorites_manager.py
#         --manifest <session dir>/favorites_manifest.json
#         --live-cache $APP_DIR/catalog_cache/live_streams.json
#         --export-config $APP_DIR/config/live_favorites_export.json
#         --token-file <session dir>/transport.token
#         --session <SESSION> --profile 1080p30 --port <live port>
#   * A custom --manager-cmd must make its own argv identify the session (the
#     private session directory, or run_live_favorites_manager.py --session
#     <SESSION>); otherwise stop refuses to signal it rather than risk a
#     reused pid.
#   * The VOD proxy port is shared, so the publisher must stop generation N
#     before starting N+1 (or use distinct ports). A stale proxy makes the new
#     one fail to bind; start detects that through the recorded pid and the
#     allowlist identity in the /probe answer.
#   * tokens and PINs are never echoed, never placed on a command line and
#     never written into state.json / the lease / the logs.
#
# Knobs (env): SWITCH_ROOT, SWITCH_PROBE_ATTEMPTS, SWITCH_PROBE_INTERVAL,
# SWITCH_STOP_WAIT, SWITCH_PROBE_FRAMES, PYTHON, CURL, APP_DIR, ENV_FILE,
# UPSTREAM, LIVE_CACHE_FILE, EXPORT_CONFIG_FILE.
# =============================================================================
set -eu
umask 077

case $0 in
    */*) SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd) ;;
    *) SCRIPT_DIR=$(pwd) ;;
esac
SELF_ABS=$SCRIPT_DIR/${0##*/}
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)

ROOT=${SWITCH_ROOT:-$HOME/.mixed-vod}
LEASES=$ROOT/leases
PROXY_LAUNCHER=$SCRIPT_DIR/start_mixed_vod_proxy_vps.sh
LIVE_PROBE=$SCRIPT_DIR/probe_mixed_live_endpoint.py

PYTHON=${PYTHON:-python3}
CURL=${CURL:-curl}
DEFAULT_APP_DIR=/opt/containers/apps/usb-stream-tv
DEFAULT_UPSTREAM=http://127.0.0.1:8080
MANAGER_BASENAME=run_live_favorites_manager.py
PROXY_BASENAME=run_mixed_vod_proxy.py

PROBE_ATTEMPTS=${SWITCH_PROBE_ATTEMPTS:-160}
PROBE_INTERVAL=${SWITCH_PROBE_INTERVAL:-0.25}
STOP_WAIT=${SWITCH_STOP_WAIT:-10}
PROBE_FRAMES=${SWITCH_PROBE_FRAMES:-2}

log() { echo "[switch] $*" >&2; }
die() { log "$*"; exit 1; }
# Same line to the console and to the session's restore log, so the export a
# rollback chose can be audited after the fact instead of only being printed.
log_restore() { log "$@"; [ -n "${SESSION_DIR:-}" ] && printf '[switch] %s\n' "$*" >> "$SESSION_DIR/restore.log"; }

usage() {
    cat <<'EOF'
usage:
  switch_mixed_generation_vps.sh start   <GEN_DIR> <SESSION> [--live-port N] [--vod-port N]
                                         [--generation N] [--upstream URL] [--app-dir DIR]
                                         [--supersede PREV] [--channel-id ID]
                                         [--live-file-id N --live-epoch N]
                                         [--manager-cmd 'CMD']
  switch_mixed_generation_vps.sh stop    <SESSION>
  switch_mixed_generation_vps.sh status  [<SESSION>]

GEN_DIR must contain favorites_manifest.json, vod_allowlist.json,
transport.token and chisel.auth. The manifest/allowlist contract is verified
before anything is created. Both server-side consumers of the generation (VOD
proxy + Live manager) are started and proven to serve bytes before
<SWITCH_ROOT>/current is repointed at the new session.
EOF
}

# ------------------------------------------------------------------ small utils
_is_digits() {
    case ${1:-} in ''|*[!0-9]*) return 1 ;; esac
    return 0
}

_is_port() {
    _is_digits "$1" || return 1
    [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

_is_session() {
    case ${1:-} in
        ''|.|..) return 1 ;;
        -*) return 1 ;;
        *[!A-Za-z0-9_.-]*) return 1 ;;
    esac
    [ ${#1} -le 128 ]
}

_no_newline() {
    [ "$(printf '%s' "${1:-}" | tr -d '\n')" = "${1:-}" ]
}

_pid_alive() {
    case ${1:-} in ''|*[!0-9]*) return 1 ;; esac
    [ "$1" -gt 0 ] 2>/dev/null || return 1
    kill -0 "$1" 2>/dev/null
}

# Joined NUL-separated argv of a live pid; empty when the pid is gone, a zombie
# or unreadable (/proc not mounted, another user).
_pid_cmdline() {
    _pc_file=/proc/${1:-0}/cmdline
    [ -r "$_pc_file" ] || return 0
    { tr '\0' ' ' < "$_pc_file"; } 2>/dev/null || true
    return 0
}

# 0 only when a pid's cmdline proves the process belongs to this session:
# either it carries the private session directory (both default service
# invocations read their manifest/allowlist/token from there), or it is the
# documented Live manager started for exactly this session name.
# Optional 2nd/3rd arguments prove another session (used by --supersede).
_owns_pid_cmd() {
    _opc_dir=${2:-$SESSION_DIR}
    _opc_session=${3:-$SESSION}
    case "$1" in *"$_opc_dir"*) return 0 ;; esac
    case "$1" in *"$MANAGER_BASENAME"*"--session $_opc_session"*) return 0 ;; esac
    return 1
}

# True only once the background launcher has exec'd the real proxy: only then
# does its argv prove which allowlist/token file it serves. The launcher also
# carries the session tag while it validates, so it stays kill-able.
_proxy_post_exec() {
    _pp_cmd=$(_pid_cmdline "$PROXY_PID")
    case "$_pp_cmd" in *"$PROXY_BASENAME"*"$SESSION_DIR"*) return 0 ;; esac
    return 1
}

_json_str() {
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$1" | head -n 1
}

_json_num() {
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p" "$1" | head -n 1
}

_jesc() {
    printf '%s' "${1:-}" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

_read_current_dir() {
    if [ -L "$ROOT/current" ]; then
        readlink "$ROOT/current" || true
    elif [ -f "$ROOT/current" ]; then
        head -n 1 "$ROOT/current" || true
    fi
    return 0
}

# --------------------------------------------------------- contract validation
# Prints exactly one line: contract generation=<g> manifest_id=<id> live=<n> vod=<n>
# Never prints a token or a PIN.
_validate_contract() {
    PYTHONPATH="$SCRIPT_DIR:$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON" - "$1" "$2" <<'PYCONTRACT'
import json
import sys
from pathlib import Path

from mixed_generation_contract import validate_generation

manifest_path = Path(sys.argv[1])
allowlist_path = Path(sys.argv[2])
try:
    manifest = json.loads(manifest_path.read_text())
except (OSError, ValueError) as exc:
    print("unreadable manifest: %s" % (exc,), file=sys.stderr)
    sys.exit(1)
generation = manifest.get("generation")
manifest_id = manifest.get("manifest_id")
if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
    print("manifest generation is missing or invalid", file=sys.stderr)
    sys.exit(1)
if not isinstance(manifest_id, str) or not manifest_id:
    print("manifest manifest_id is missing or invalid", file=sys.stderr)
    sys.exit(1)
try:
    info = validate_generation(manifest_path, allowlist_path, generation, manifest_id)
except (OSError, ValueError, KeyError, TypeError) as exc:
    print("generation contract invalid: %s" % (exc,), file=sys.stderr)
    sys.exit(1)
print("contract generation=%d manifest_id=%s live=%d vod=%d"
      % (info["generation"], info["manifest_id"], info["live_count"], info["vod_count"]))
PYCONTRACT
}

# ----------------------------------------------------- export of a session
# Every generation is built for ONE Live export. The lease records it, but a
# lease written by an older revision does not carry the field: inheriting the
# caller's export would start the restored manager with an export that does not
# match the session's own manifest, and the manager would refuse to serve Live.
# The export is therefore PROVEN by matching the session manifest against the
# candidate exports; only when nothing can be proven does the caller's value get
# used, and then it is called out as unproven instead of being trusted silently.
export_matches_manifest() { # <manifest> <export> -> 0 when they describe the same set
    PYTHONPATH="$SCRIPT_DIR:$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON" - "$1" "$2" <<'PYEXPORT' >/dev/null 2>&1
import json
import sys
from pathlib import Path

from run_live_favorites_manager import manifest_matches_export

manifest = json.loads(Path(sys.argv[1]).read_text())
config = json.loads(Path(sys.argv[2]).read_text())
if not isinstance(config, dict) or "identities" not in config:
    sys.exit(1)
manifest_matches_export(manifest, Path(sys.argv[2]))
PYEXPORT
}

resolve_session_export() { # <session_dir> [lease_export] -> echoes the export
    _rsx_dir=$1
    _rsx_lease=${2:-}
    if [ -n "$_rsx_lease" ] && [ -f "$_rsx_lease" ]; then
        printf '%s\n' "$_rsx_lease"
        return 0
    fi
    [ -f "$_rsx_dir/favorites_manifest.json" ] || return 1
    for _rsx_cand in ${EXPORT_CONFIG_FILE:-} "$APP_DIR"/config/*.json; do
        [ -f "$_rsx_cand" ] || continue
        if export_matches_manifest "$_rsx_dir/favorites_manifest.json" "$_rsx_cand"; then
            printf '%s\n' "$_rsx_cand"
            return 0
        fi
    done
    return 1
}

# ------------------------------------------------------------------- processes
# Terminate one recorded pid, but only after its cmdline proves ownership.
_stop_pid() {
    _sp_pid=${1:-}
    _pid_alive "$_sp_pid" || return 0
    _sp_cmd=$(_pid_cmdline "$_sp_pid")
    # An empty cmdline is a zombie (nothing left to signal, never a live
    # service), so it is not reported as a refusal.
    [ -n "$_sp_cmd" ] || return 0
    if ! _owns_pid_cmd "$_sp_cmd"; then
        log "refusing to signal pid $_sp_pid: /proc/$_sp_pid/cmdline is not this session"
        return 0
    fi
    kill -TERM "$_sp_pid" 2>/dev/null || true
    _sp_i=0
    while [ "$_sp_i" -lt "$STOP_WAIT" ]; do
        _pid_alive "$_sp_pid" || return 0
        [ -n "$(_pid_cmdline "$_sp_pid")" ] || return 0
        sleep 0.2
        _sp_i=$((_sp_i + 1))
    done
    kill -KILL "$_sp_pid" 2>/dev/null || true
    _sp_i=0
    while [ "$_sp_i" -lt 5 ]; do
        _pid_alive "$_sp_pid" || return 0
        [ -n "$(_pid_cmdline "$_sp_pid")" ] || return 0
        sleep 0.2
        _sp_i=$((_sp_i + 1))
    done
    log "pid $_sp_pid did not exit after SIGKILL"
    return 0
}

# Only ever called for the two pids this run started (never for another
# session's processes).
_stop_started() {
    _stop_pid "${MANAGER_PID:-}"
    _stop_pid "${PROXY_PID:-}"
    return 0
}

# Stop a pid after proving it belongs to an explicitly named session. Used to
# supersede the previous generation while the globals point at the new one.
_stop_pid_of() { # <session_dir> <session_name> <pid>
    _spin_dir=$SESSION_DIR
    _spin_session=$SESSION
    SESSION_DIR=$1
    SESSION=$2
    _stop_pid "$3"
    SESSION_DIR=$_spin_dir
    SESSION=$_spin_session
    return 0
}

_probe_cleanup() {
    [ -n "${PROBE_HDR:-}" ] && rm -f "$PROBE_HDR"
    [ -n "${PROBE_BODY:-}" ] && rm -f "$PROBE_BODY"
    return 0
}

# Abort without touching the previous generation: stop what this run started,
# drop the credential header file, never write state.json / current. If this run
# had already superseded the previous generation, bring it back: a failed start
# must not leave the server-side consumers of the working generation down.
# The verdict line tells the publisher the truth about that restore.
_fail() {
    _stop_started
    _probe_cleanup
    _f_restored=none
    if [ "${SUPERSEDED:-0}" = 1 ] && [ -n "${PREV_SESSION:-}" ]; then
        log "start failed after superseding $PREV_SESSION; restoring it"
        if _restore_previous; then
            _f_restored=yes
        else
            _f_restored=no
            log "warning: could not restore superseded session $PREV_SESSION"
        fi
    fi
    echo "switch failed previous_restored=$_f_restored reason=$*"
    log "$*"
    exit 1
}

# --------------------------------------------------------------- default mgr
_default_manager_cmd() {
    printf '%s\n' "$PYTHON $APP_DIR/scripts/$MANAGER_BASENAME --manifest $SESSION_DIR/favorites_manifest.json --live-cache $LIVE_CACHE_FILE --export-config '$EXPORT_CONFIG_FILE' --token-file $SESSION_DIR/transport.token --session $SESSION --profile 1080p30 --port $LIVE_PORT"
}

# ------------------------------------------------------------------ artifacts
_copy_artifacts() {
    for _ca_name in favorites_manifest.json vod_allowlist.json transport.token chisel.auth; do
        _ca_src=$GEN_DIR/$_ca_name
        _ca_dst=$SESSION_DIR/$_ca_name
        # Restoring a previous session uses its own session directory as the
        # generation source, so source and destination can be the same file.
        if [ "$_ca_src" != "$_ca_dst" ] && ! [ "$_ca_src" -ef "$_ca_dst" ]; then
            cp -f "$_ca_src" "$_ca_dst"
        fi
        chmod 600 "$_ca_dst"
    done
    return 0
}

_write_state() {
    _ws_tmp=$SESSION_DIR/.state.json.$$.tmp
    {
        printf '{\n'
        printf '  "session": "%s",\n' "$(_jesc "$SESSION")"
        printf '  "generation": %s,\n' "$GENERATION"
        printf '  "previous_session": "%s",\n' "$(_jesc "${PREV_SESSION:-}")"
        printf '  "previous_dir": "%s",\n' "$(_jesc "${PREV_DIR:-}")"
        printf '  "proxy_pid": %s,\n' "$PROXY_PID"
        printf '  "manager_pid": %s,\n' "$MANAGER_PID"
        printf '  "ports": {"live": %s, "vod": %s},\n' "$LIVE_PORT" "$VOD_PORT"
        printf '  "started_at": %s\n' "$STARTED_AT"
        printf '}\n'
    } > "$_ws_tmp"
    chmod 600 "$_ws_tmp"
    mv -f "$_ws_tmp" "$SESSION_DIR/state.json"
}

# The lease is the private runtime record (superset of state.json, never a
# secret): it lets stop restore the previous session with the same ports,
# upstream, app dir, live identities and manager command.
_write_lease() {
    _wl_tmp=$LEASES/.$SESSION.lease.$$.tmp
    {
        printf 'session=%s\n' "$SESSION"
        printf 'generation=%s\n' "$GENERATION"
        printf 'app_dir=%s\n' "$APP_DIR"
        printf 'env_file=%s\n' "$ENV_FILE"
        printf 'upstream=%s\n' "$UPSTREAM"
        # The Live export is per-generation data: restoring a session with the
        # wrong export makes the manager reject its own manifest.
        printf 'export_config=%s\n' "$EXPORT_CONFIG_FILE"
        printf 'live_port=%s\n' "$LIVE_PORT"
        printf 'vod_port=%s\n' "$VOD_PORT"
        printf 'manager_cmd=%s\n' "$MANAGER_CMD"
        printf 'channel_id=%s\n' "$CHANNEL_ID"
        printf 'live_file_id=%s\n' "$LIVE_FILE_ID"
        printf 'live_epoch=%s\n' "$LIVE_EPOCH"
        printf 'proxy_pid=%s\n' "$PROXY_PID"
        printf 'manager_pid=%s\n' "$MANAGER_PID"
        printf 'started_at=%s\n' "$STARTED_AT"
    } > "$_wl_tmp"
    chmod 600 "$_wl_tmp"
    mv -f "$_wl_tmp" "$LEASES/$SESSION.lease"
}

_set_current() {
    _sc_tmp=$ROOT/.current.$$.tmp
    rm -f "$_sc_tmp"
    ln -s "$SESSION_DIR" "$_sc_tmp"
    # -T so an existing symlink is replaced instead of being followed as a dir.
    if ! mv -T "$_sc_tmp" "$ROOT/current" 2>/dev/null; then
        rm -f "$ROOT/current"
        mv -f "$_sc_tmp" "$ROOT/current"
    fi
    return 0
}

# --------------------------------------------------------------- verification
_verify_proxy() {
    PROBE_HDR=$SESSION_DIR/.probe.headers
    PROBE_BODY=$SESSION_DIR/.probe.body
    # The bearer token goes into a 0600 file the curl process reads through
    # -H "@file"; it is never an argument of any process.
    {
        printf 'Authorization: Bearer '
        tr -d '\r\n' < "$SESSION_DIR/transport.token"
        printf '\n'
    } > "$PROBE_HDR"
    chmod 600 "$PROBE_HDR"

    _vp_i=0
    _vp_ok=0
    _vp_code=none
    while [ "$_vp_i" -lt "$PROBE_ATTEMPTS" ]; do
        _vp_i=$((_vp_i + 1))
        # A stale service on the port could answer while ours died failing to
        # bind, so the recorded pid must still be our live process.
        _pid_alive "$PROXY_PID" \
            || _fail "the started VOD proxy exited before serving /probe (see $SESSION_DIR/proxy.log)"
        # While the launcher is still proving the origin, its argv cannot show
        # which generation it will serve: wait for the post-exec command line
        # instead of trusting whoever answers the port.
        if ! _proxy_post_exec; then
            sleep "$PROBE_INTERVAL"
            continue
        fi
        rm -f "$PROBE_BODY"
        _vp_code=$("$CURL" -sS -o "$PROBE_BODY" -w '%{http_code}' --max-time 10 \
            -H "@$PROBE_HDR" "http://127.0.0.1:$VOD_PORT/probe" \
            2>>"$SESSION_DIR/proxy.log" || true)
        if [ "$_vp_code" = "000" ] || [ -z "$_vp_code" ]; then
            # Nothing listening yet.
            sleep "$PROBE_INTERVAL"
            continue
        fi
        if [ "$_vp_code" != "200" ]; then
            _probe_cleanup
            _fail "VOD proxy rejected the /probe read (HTTP $_vp_code) on 127.0.0.1:$VOD_PORT (see $SESSION_DIR/proxy.log)"
        fi
        # /probe answers "OK <task_id> <size>" for the first allowlisted task:
        # that task must be one of ours, so a listener still serving the
        # previous generation can never be accepted.
        _vp_task=$(head -n 1 "$PROBE_BODY" 2>/dev/null \
            | sed -n 's/^OK \([0-9a-f][0-9a-f]*\) [0-9][0-9]*$/\1/p' || true)
        if [ -n "$_vp_task" ] \
            && grep -q "\"task_id\"[[:space:]]*:[[:space:]]*\"$_vp_task\"" "$SESSION_DIR/vod_allowlist.json"; then
            _vp_ok=1
            break
        fi
        sleep "$PROBE_INTERVAL"
    done
    _probe_cleanup
    [ "$_vp_ok" = 1 ] \
        || _fail "VOD proxy did not serve a real /probe read on 127.0.0.1:$VOD_PORT (see $SESSION_DIR/proxy.log)"
    return 0
}

_verify_live() {
    _vl_log=$SESSION_DIR/live-probe.log
    _vl_i=0
    _vl_ok=0
    while [ "$_vl_i" -lt "$PROBE_ATTEMPTS" ]; do
        _vl_i=$((_vl_i + 1))
        _pid_alive "$MANAGER_PID" \
            || _fail "the started Live manager exited (see $SESSION_DIR/manager.log)"
        _vl_rc=0
        "$PYTHON" "$LIVE_PROBE" \
            --host 127.0.0.1 --port "$LIVE_PORT" \
            --session "$SESSION" --manifest-id "$MANIFEST_ID" \
            --channel-id "$CHANNEL_ID" --file-id "$LIVE_FILE_ID" \
            --generation "$GENERATION" --epoch "$LIVE_EPOCH" \
            --token-file "$SESSION_DIR/transport.token" --frames "$PROBE_FRAMES" \
            > "$_vl_log" 2>&1 || _vl_rc=$?
        chmod 600 "$_vl_log" 2>/dev/null || true
        if [ "$_vl_rc" -eq 0 ] && grep -q '"ok": true' "$_vl_log"; then
            _vl_ok=1
            break
        fi
        sleep "$PROBE_INTERVAL"
    done
    [ "$_vl_ok" = 1 ] \
        || _fail "Live path did not serve real bytes (probe_mixed_live_endpoint.py, see $_vl_log)"
    return 0
}

# -------------------------------------------------------------------- start
cmd_start() {
    [ $# -ge 2 ] || { usage >&2; exit 2; }
    GEN_DIR=$1
    SESSION=$2
    shift 2

    LIVE_PORT=18190
    VOD_PORT=18191
    GENERATION=""
    UPSTREAM=${UPSTREAM:-$DEFAULT_UPSTREAM}
    APP_DIR=${APP_DIR:-$DEFAULT_APP_DIR}
    ENV_FILE=${ENV_FILE:-}
    MANAGER_CMD=""
    CHANNEL_ID=""
    LIVE_FILE_ID=""
    LIVE_EPOCH=""
    SUPERSEDE_SESSION=""

    while [ $# -gt 0 ]; do
        _opt=$1
        case $_opt in
            --live-port|--vod-port|--generation|--upstream|--app-dir|--supersede|--channel-id|--live-file-id|--live-epoch|--manager-cmd) ;;
            --) shift; continue ;;
            *) usage >&2; die "unknown option: $_opt" ;;
        esac
        [ $# -ge 2 ] || { usage >&2; die "missing value for $_opt"; }
        _val=$2
        case $_opt in
            --live-port) LIVE_PORT=$_val ;;
            --vod-port) VOD_PORT=$_val ;;
            --generation) GENERATION=$_val ;;
            --upstream) UPSTREAM=$_val ;;
            --app-dir) APP_DIR=$_val ;;
            --supersede) SUPERSEDE_SESSION=$_val ;;
            --channel-id) CHANNEL_ID=$_val ;;
            --live-file-id) LIVE_FILE_ID=$_val ;;
            --live-epoch) LIVE_EPOCH=$_val ;;
            --manager-cmd) MANAGER_CMD=$_val ;;
        esac
        shift 2
    done

    _is_session "$SESSION" || die "invalid session name: $SESSION"
    _is_port "$LIVE_PORT" || die "invalid --live-port: $LIVE_PORT"
    _is_port "$VOD_PORT" || die "invalid --vod-port: $VOD_PORT"
    _no_newline "$UPSTREAM" && _no_newline "$APP_DIR" && _no_newline "$ENV_FILE" \
        && _no_newline "$MANAGER_CMD" && _no_newline "$CHANNEL_ID" \
        || die "option values must not contain newlines"
    if [ -n "$SUPERSEDE_SESSION" ]; then
        _is_session "$SUPERSEDE_SESSION" || die "invalid --supersede session: $SUPERSEDE_SESSION"
    fi
    if [ -n "$GENERATION" ]; then
        _is_digits "$GENERATION" || die "invalid --generation: $GENERATION"
    fi
    if [ -n "$CHANNEL_ID" ] && { [ -z "$LIVE_FILE_ID" ] || [ -z "$LIVE_EPOCH" ]; }; then
        die "--channel-id requires --live-file-id and --live-epoch"
    fi
    if [ -n "$LIVE_FILE_ID" ] && ! _is_digits "$LIVE_FILE_ID"; then
        die "invalid --live-file-id: $LIVE_FILE_ID"
    fi
    if [ -n "$LIVE_EPOCH" ] && ! _is_digits "$LIVE_EPOCH"; then
        die "invalid --live-epoch: $LIVE_EPOCH"
    fi

    SESSION_DIR=$ROOT/$SESSION
    [ -z "$ENV_FILE" ] && ENV_FILE=$APP_DIR/.env
    LIVE_CACHE_FILE=${LIVE_CACHE_FILE:-$APP_DIR/catalog_cache/live_streams.json}
    EXPORT_CONFIG_FILE=${EXPORT_CONFIG_FILE:-$APP_DIR/config/live_favorites_export.json}
    PROXY_PID=""
    MANAGER_PID=""
    PREV_SESSION=""
    PREV_DIR=""
    SUPERSEDED=0

    # --- 1a. artifacts must exist (nothing is created before this passes)
    [ -d "$GEN_DIR" ] || die "generation directory not found: $GEN_DIR"
    [ -f "$GEN_DIR/favorites_manifest.json" ] \
        || die "missing generation artifact: $GEN_DIR/favorites_manifest.json"
    [ -f "$GEN_DIR/vod_allowlist.json" ] \
        || die "missing generation artifact: $GEN_DIR/vod_allowlist.json"
    [ -f "$GEN_DIR/transport.token" ] \
        || die "missing generation artifact: $GEN_DIR/transport.token"
    [ -f "$GEN_DIR/chisel.auth" ] \
        || die "missing generation artifact: $GEN_DIR/chisel.auth"

    command -v "$PYTHON" >/dev/null 2>&1 || die "$PYTHON not found"
    command -v "$CURL" >/dev/null 2>&1 || die "$CURL not found"
    [ -f "$PROXY_LAUNCHER" ] || die "VOD proxy launcher not found: $PROXY_LAUNCHER"
    [ -f "$APP_DIR/scripts/$PROXY_BASENAME" ] \
        || die "VOD proxy script not found: $APP_DIR/scripts/$PROXY_BASENAME"
    [ -f "$ENV_FILE" ] || die "env file not found: $ENV_FILE"
    # The manager and the VOD proxy read their credentials (XTREAM_UPSTREAM,
    # XTREAM_USER, XTREAM_PASS) from the environment. The file is named here but
    # was never loaded, so anything that did not inherit systemd's
    # EnvironmentFile -- a manual run, sudo, env -i -- started the manager with
    # no credentials and it exited with "invalid manifest/catalog/credentials".
    # Measured: the same command succeeds with the file loaded and fails without.
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    # ``EXPORT_CONFIG_FILE`` is resolved above (argument or default) but was never
    # exported, so the manager inherited no value at all and fell back to its own
    # default export. With the reviewed TV/ layout the manifest then no longer
    # matched that export and the manager refused to start. Exporting the value
    # this script already decided on keeps the consumer and the manifest in sync.
    [ -n "$EXPORT_CONFIG_FILE" ] && EXPORT_CONFIG_FILE=$EXPORT_CONFIG_FILE && export EXPORT_CONFIG_FILE
    export APP_DIR
    [ -f "$LIVE_PROBE" ] || die "Live probe not found: $LIVE_PROBE"
    if [ -z "$MANAGER_CMD" ]; then
        [ -f "$APP_DIR/scripts/$MANAGER_BASENAME" ] \
            || die "Live manager not found: $APP_DIR/scripts/$MANAGER_BASENAME"
        [ -f "$LIVE_CACHE_FILE" ] || die "Live cache not found: $LIVE_CACHE_FILE"
        [ -f "$EXPORT_CONFIG_FILE" ] \
            || die "Live export config not found: $EXPORT_CONFIG_FILE"
    fi
    _token_bytes=$(wc -c < "$GEN_DIR/transport.token" | tr -d ' ') || _token_bytes=0
    [ "$_token_bytes" -ge 32 ] || die "transport token is shorter than 32 bytes: $GEN_DIR/transport.token"

    # --- 1b. manifest/allowlist contract, with the identities read from the
    #         manifest itself. A mismatch aborts here.
    _contract=""
    _contract=$(_validate_contract "$GEN_DIR/favorites_manifest.json" "$GEN_DIR/vod_allowlist.json") \
        || _fail "generation contract invalid for $GEN_DIR (see the message above)"
    _c_gen=""
    _c_id=""
    for _c_tok in $_contract; do
        case $_c_tok in
            generation=*) _c_gen=${_c_tok#generation=} ;;
            manifest_id=*) _c_id=${_c_tok#manifest_id=} ;;
        esac
    done
    [ -n "$_c_gen" ] && [ -n "$_c_id" ] \
        || _fail "generation contract could not be verified for $GEN_DIR"
    if [ -n "$GENERATION" ] && [ "$GENERATION" != "$_c_gen" ]; then
        _fail "declared generation $GENERATION does not match the manifest generation $_c_gen"
    fi
    GENERATION=$_c_gen
    MANIFEST_ID=$_c_id

    # --- 2. private per-session directory (0700)
    mkdir -p "$ROOT"
    chmod 700 "$ROOT"
    mkdir -p "$SESSION_DIR"
    chmod 700 "$SESSION_DIR"

    # --- 3. idempotent: an already-live session is a no-op. Checked before the
    #        artifact copies so a running session's stable inputs are never
    #        rewritten under it.
    if _state_is_live "$SESSION_DIR/state.json"; then
        echo "already_active session=$SESSION"
        return 0
    fi

    _copy_artifacts

    # --- 3b. previous session, then an explicit supersede. Both generations
    #         share the live/vod ports, so the candidate can only bind once the
    #         previous session's own proven processes are gone. The superseded
    #         session is recorded as the previous one, so `stop <SESSION>` and
    #         any failure below bring it back.
    _cur_dir=$(_read_current_dir)
    if [ -n "$_cur_dir" ]; then
        case $_cur_dir in
            "$ROOT"/*) _prev_name=${_cur_dir##*/}
                if [ "$_prev_name" != "$SESSION" ] && _is_session "$_prev_name" \
                    && [ -f "$_cur_dir/state.json" ]; then
                    PREV_SESSION=$_prev_name
                    PREV_DIR=$_cur_dir
                fi ;;
        esac
    fi
    if [ -n "$SUPERSEDE_SESSION" ] && [ "$SUPERSEDE_SESSION" != "$SESSION" ]; then
        [ -n "$PREV_SESSION" ] \
            || _fail "--supersede $SUPERSEDE_SESSION but no session is current"
        [ "$SUPERSEDE_SESSION" = "$PREV_SESSION" ] \
            || _fail "--supersede $SUPERSEDE_SESSION is not the current session ($PREV_SESSION)"
        _ss_proxy=$(_json_num "$PREV_DIR/state.json" proxy_pid)
        _ss_mgr=$(_json_num "$PREV_DIR/state.json" manager_pid)
        log "superseding $SUPERSEDE_SESSION: stopping only its proven processes"
        _stop_pid_of "$PREV_DIR" "$SUPERSEDE_SESSION" "$_ss_mgr"
        _stop_pid_of "$PREV_DIR" "$SUPERSEDE_SESSION" "$_ss_proxy"
        SUPERSEDED=1
    fi

    # --- 4. both consumers, background, own logs, recorded pids
    # The session tag after the launcher path keeps the launcher's own argv
    # identifiable (and kill-able) while it validates the origin; the launcher
    # ignores positional arguments and replaces them when it execs the proxy.
    env APP_DIR="$APP_DIR" PKG_DIR="$SESSION_DIR" ENV_FILE="$ENV_FILE" \
        PORT="$VOD_PORT" UPSTREAM="$UPSTREAM" \
        sh "$PROXY_LAUNCHER" "switch-session=$SESSION_DIR" \
        >> "$SESSION_DIR/proxy.log" 2>&1 &
    PROXY_PID=$!

    if [ -z "$MANAGER_CMD" ]; then
        MANAGER_CMD=$(_default_manager_cmd)
    fi
    # ``exec`` keeps the recorded pid equal to the manager process itself, so
    # /proc/<pid>/cmdline stays usable as the ownership proof at stop.
    # One single interpretation of the recorded command: shlex + execvp. Running
    # it through `sh -c` let the shell re-parse the string, so a path carrying a
    # metacharacter (space, &, ;, ') was re-split and the manager got a different
    # argument than the one recorded. execvp keeps the same PID, so the recorded
    # pid remains the ownership proof at stop.
    "$PYTHON" -c 'import os, shlex, sys; argv = shlex.split(sys.argv[1], posix=True); os.execvp(argv[0], argv)' \
        "$MANAGER_CMD" >> "$SESSION_DIR/manager.log" 2>&1 &
    MANAGER_PID=$!

    # --- 5. prove BOTH consumers really serve bytes
    _verify_proxy
    if [ -n "$CHANNEL_ID" ]; then
        _verify_live
    else
        _pid_alive "$MANAGER_PID" \
            || _fail "the started Live manager exited (see $SESSION_DIR/manager.log)"
    fi

    # --- 6. publish the state and repoint current
    STARTED_AT=$(date +%s)

    mkdir -p "$LEASES"
    chmod 700 "$LEASES"
    _write_state
    _write_lease
    _set_current
    _probe_cleanup

    # --- 7. machine-readable verdict
    echo "switch ok generation=$GENERATION session=$SESSION live_port=$LIVE_PORT vod_port=$VOD_PORT"
    return 0
}

# True only when a state file exists and every pid it records is still the
# session's own live process.
_state_is_live() {
    _sil_state=$1
    [ -f "$_sil_state" ] || return 1
    _sil_proxy=$(_json_num "$_sil_state" proxy_pid)
    _sil_mgr=$(_json_num "$_sil_state" manager_pid)
    _sil_any=0
    for _sil_pid in "$_sil_proxy" "$_sil_mgr"; do
        [ -n "$_sil_pid" ] || continue
        _pid_alive "$_sil_pid" || return 1
        _sil_cmd=$(_pid_cmdline "$_sil_pid")
        [ -n "$_sil_cmd" ] || return 1
        _owns_pid_cmd "$_sil_cmd" || return 1
        _sil_any=1
    done
    [ "$_sil_any" = 1 ]
}

# ---------------------------------------------------------------- restore
# One level deep: called once by stop for the recorded previous session, and
# the nested start never calls back into stop.
_restore_previous() {
    _rp_lease=$LEASES/$PREV_SESSION.lease
    _rp_app=""
    _rp_env=""
    _rp_up=""
    _rp_mgr=""
    _rp_ch=""
    _rp_file=""
    _rp_epoch=""
    _rp_live=${PREV_LIVE_PORT:-}
    _rp_vod=${PREV_VOD_PORT:-}
    _rp_gen=${PREV_GEN:-}
    _rp_export=""
    _rp_lease_export=""
    if [ -f "$_rp_lease" ]; then
        while IFS='=' read -r _rp_k _rp_v; do
            case $_rp_k in
                app_dir) _rp_app=$_rp_v ;;
                env_file) _rp_env=$_rp_v ;;
                upstream) _rp_up=$_rp_v ;;
                manager_cmd) _rp_mgr=$_rp_v ;;
                channel_id) _rp_ch=$_rp_v ;;
                live_file_id) _rp_file=$_rp_v ;;
                live_epoch) _rp_epoch=$_rp_v ;;
                live_port) _rp_live=$_rp_v ;;
                vod_port) _rp_vod=$_rp_v ;;
                generation) _rp_gen=$_rp_v ;;
                export_config) _rp_lease_export=$_rp_v ;;
            esac
        done < "$_rp_lease"
    fi

    set -- start "$PREV_DIR" "$PREV_SESSION"
    [ -z "$_rp_gen" ] || set -- "$@" --generation "$_rp_gen"
    [ -z "$_rp_live" ] || set -- "$@" --live-port "$_rp_live"
    [ -z "$_rp_vod" ] || set -- "$@" --vod-port "$_rp_vod"
    [ -z "$_rp_up" ] || set -- "$@" --upstream "$_rp_up"
    [ -z "$_rp_app" ] || set -- "$@" --app-dir "$_rp_app"
    if [ -n "$_rp_ch" ] && [ -n "$_rp_file" ] && [ -n "$_rp_epoch" ]; then
        set -- "$@" --channel-id "$_rp_ch" --live-file-id "$_rp_file" --live-epoch "$_rp_epoch"
    fi

    log "restoring previous session $PREV_SESSION ($PREV_DIR)"
    # The export belongs to the session being restored, never to the caller:
    # a rollback that switched exports would leave the manager refusing its own
    # manifest. A legacy lease has no field, so the export is proven instead.
    if _rp_export=$(resolve_session_export "$PREV_DIR" "$_rp_lease_export"); then
        log_restore "restoring with proven export $_rp_export"
    else
        # Nothing could be matched: fall back to the caller's export, which is
        # what a pre-fix rollback did, but say so instead of implying a proof.
        _rp_export=${EXPORT_CONFIG_FILE:-}
        # A path that is not usable is the same as none: launching the manager
        # with a missing export is exactly the failure being prevented here.
        if [ -z "$_rp_export" ] || [ ! -f "$_rp_export" ]; then
            log_restore "refusing to restore $PREV_SESSION: export $_rp_export is unusable and none was proven"
            return 1
        fi
        log_restore "warning: export of $PREV_SESSION could not be proven; using $_rp_export"
    fi
    # The recorded manager command embeds the export it was launched with, so it
    # must agree with the resolved one; otherwise it would silently override the
    # proof. A command with no export token is rebuilt from the default template.
    if [ -n "$_rp_mgr" ]; then
        case " $_rp_mgr " in
            *" --export-config "*)
                # Literal rewrite, in Python: in a sed replacement an ampersand
                # means "the whole match", and export paths are not guaranteed to
                # be free of it.
                _rp_mgr=$("$PYTHON" - "$_rp_mgr" "$_rp_export" <<'PYMGR'
import re
import sys

command, export = sys.argv[1], sys.argv[2]
# Both the quoted form written by this revision and the bare form older leases
# carry are matched; the result is always quoted, because the command is later
# run through ``sh -c``.
pattern = r"--export-config ('[^']*'|[^ ][^ ]*)"
print(re.sub(pattern, lambda _m: "--export-config '" + export.replace("'", "'\\''") + "'",
             command, count=1), end="")
PYMGR
                )
                ;;
            *)
                log_restore "session manager command carries no export; rebuilding it"
                _rp_mgr=""
                ;;
        esac
    fi
    [ -z "$_rp_mgr" ] || set -- "$@" --manager-cmd "$_rp_mgr"
    EXPORT_CONFIG_FILE="$_rp_export" ENV_FILE="$_rp_env" \
        sh "$SELF_ABS" "$@" >> "$SESSION_DIR/restore.log" 2>&1
}

# -------------------------------------------------------------------- stop
cmd_stop() {
    [ $# -ge 1 ] || { usage >&2; exit 2; }
    SESSION=$1
    shift
    [ $# -eq 0 ] || { usage >&2; die "too many arguments for stop"; }
    _is_session "$SESSION" || die "invalid session name: $SESSION"
    SESSION_DIR=$ROOT/$SESSION
    _st_state=$SESSION_DIR/state.json
    if [ ! -f "$_st_state" ]; then
        echo "switch stop session=$SESSION restored=none"
        return 0
    fi

    _st_proxy=$(_json_num "$_st_state" proxy_pid)
    _st_mgr=$(_json_num "$_st_state" manager_pid)
    PREV_SESSION=$(_json_str "$_st_state" previous_session)
    PREV_DIR=$(_json_str "$_st_state" previous_dir)
    # The restore parameters belong to the *previous* session, not to the one
    # being stopped (it may have used different ports or generation).
    PREV_GEN=""
    PREV_LIVE_PORT=""
    PREV_VOD_PORT=""
    if [ -n "$PREV_DIR" ] && [ -f "$PREV_DIR/state.json" ]; then
        PREV_GEN=$(_json_num "$PREV_DIR/state.json" generation)
        PREV_LIVE_PORT=$(_json_num "$PREV_DIR/state.json" live)
        PREV_VOD_PORT=$(_json_num "$PREV_DIR/state.json" vod)
    fi

    _st_lease=$LEASES/$SESSION.lease
    _st_had_lease=0
    if [ -f "$_st_lease" ]; then
        _st_had_lease=1
    fi

    _stop_pid "$_st_mgr"
    _stop_pid "$_st_proxy"
    rm -f "$_st_lease"

    _st_restored=none
    if [ "$_st_had_lease" = 1 ] && [ -n "$PREV_SESSION" ] && [ -n "$PREV_DIR" ] \
        && [ -d "$PREV_DIR" ] && [ -f "$PREV_DIR/state.json" ]; then
        if _restore_previous; then
            _st_restored=$PREV_SESSION
        else
            log "warning: could not restore previous session $PREV_SESSION (see $SESSION_DIR/restore.log)"
        fi
    fi

    echo "switch stop session=$SESSION restored=$_st_restored"
    return 0
}

# ------------------------------------------------------------------ status
_status_one() {
    _so_dir=$1
    _so_session=${_so_dir##*/}
    _so_state=$_so_dir/state.json
    _so_pid=$(_json_num "$_so_state" proxy_pid)
    _so_mgr=$(_json_num "$_so_state" manager_pid)
    _so_gen=$(_json_num "$_so_state" generation)
    _so_proxy=dead
    _so_manager=dead
    _pid_alive "$_so_pid" && _so_proxy=alive
    _pid_alive "$_so_mgr" && _so_manager=alive
    _so_current=no
    [ "$(_read_current_dir)" = "$_so_dir" ] && _so_current=yes
    echo "session=$_so_session generation=${_so_gen:-unknown} proxy_pid=${_so_pid:--} proxy=$_so_proxy manager_pid=${_so_mgr:--} manager=$_so_manager current=$_so_current"
    return 0
}

cmd_status() {
    if [ $# -ge 1 ]; then
        SESSION=$1
        shift
        [ $# -eq 0 ] || { usage >&2; die "too many arguments for status"; }
        _is_session "$SESSION" || die "invalid session name: $SESSION"
        SESSION_DIR=$ROOT/$SESSION
        if [ ! -f "$SESSION_DIR/state.json" ]; then
            echo "session=$SESSION state=missing"
            return 0
        fi
        _status_one "$SESSION_DIR"
        return 0
    fi
    _st_count=0
    for _st_dir in "$ROOT"/*; do
        [ -d "$_st_dir" ] || continue
        [ -f "$_st_dir/state.json" ] || continue
        _status_one "$_st_dir"
        _st_count=$((_st_count + 1))
    done
    if [ "$_st_count" -eq 0 ]; then
        echo "sessions=0"
    fi
    return 0
}

# -------------------------------------------------------------------- main
[ $# -ge 1 ] || { usage >&2; exit 2; }
_cmd=$1
shift
case $_cmd in
    start) cmd_start "$@" ;;
    stop) cmd_stop "$@" ;;
    status) cmd_status "$@" ;;
    help|-h|--help) usage ;;
    *) usage >&2; die "unknown command: $_cmd" ;;
esac
