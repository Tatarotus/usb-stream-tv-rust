#!/bin/bash
# =============================================================================
# test_migrate_export.sh — migração do export Live (nada real é tocado)
#
# Prova, com systemd e processos simulados:
#   1. daemon-reload sozinho NÃO troca o export do processo em execução;
#   2. `apply` reinicia a bridge e confere argv/env do PID real;
#   3. QUALQUER falha desfaz tudo: unidades, systemd, bridge e TIMER;
#   4. `apply` recusa quando não pode provar que a volta é possível;
#   5. `rollback` volta as referências e sai com 0;
#   6. os cinco leitores são conferidos um por um;
#   7. backup incompleto nunca é usado.
#
# Processos simulados: cada um roda em sessão própria e grava seu PID em
# $TMP/pids. O cleanup mata SOMENTE esses PIDs, depois de conferir UID e que a
# linha de comando referencia este $TMP. Nenhum kill por nome/padrão.
# =============================================================================
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/test-migrate.XXXXXX")
MY_UID=$(id -u)

PIDS_FILE=$TMP/pids
: > "$PIDS_FILE"

kill_owned() { # <pid>
    _ko_pid=$1
    case "$_ko_pid" in ''|*[!0-9]*) return 0 ;; esac
    [ -r "/proc/$_ko_pid/cmdline" ] || return 0
    _ko_uid=$(awk '/^Uid:/{print $2}' "/proc/$_ko_pid/status" 2>/dev/null)
    [ "$_ko_uid" = "$MY_UID" ] || return 0
    _ko_cmd=$(tr '\0' ' ' < "/proc/$_ko_pid/cmdline" 2>/dev/null)
    case "$_ko_cmd" in
        *"$TMP"*) : ;;
        *) return 0 ;;
    esac
    kill -TERM "-$_ko_pid" 2>/dev/null || kill -TERM "$_ko_pid" 2>/dev/null || true
}

cleanup() {
    trap - EXIT INT TERM
    [ -f "$TMP/bridge.pid" ] && cat "$TMP/bridge.pid" >> "$PIDS_FILE" 2>/dev/null || true
    for _c_pid in $(sort -u "$PIDS_FILE" 2>/dev/null); do kill_owned "$_c_pid"; done
    sleep 0.2
    for _c_pid in $(sort -u "$PIDS_FILE" 2>/dev/null); do kill_owned "$_c_pid"; done
    rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

BIN=$TMP/bin
mkdir -p "$BIN" "$TMP/units" "$TMP/backup-root" "$TMP/app/config" "$TMP/state"
ln -s "$REPO/scripts" "$TMP/app/scripts"

printf '#!/bin/sh\n[ "$1" = -u ] && { echo 0; exit 0; }\nexec /usr/bin/id "$@"\n' > "$BIN/id"
chmod +x "$BIN/id"

# --------------------------------------------------------------- systemd shim
# Models units, the running bridge and the timer state, and records every call.
cat > "$BIN/systemctl" <<'SH'
#!/bin/bash
printf '%s\n' "$*" >> "$SR_SHIM_LOG"
pid_file=$SR_STATE/bridge.pid
timer_marker=$SR_STATE/state/timer.active
prepare_marker=$SR_STATE/state/prepare.active

fail_once() { # <name> <enabled 0/1>
    [ "${2:-}" = 1 ] || return 1
    [ -f "$SR_STATE/$1.failed" ] && return 1
    : > "$SR_STATE/$1.failed"
    return 0
}

kill_owned_pid() {
    [ -f "$pid_file" ] || return 0
    pid=$(cat "$pid_file")
    if [ -r "/proc/$pid/cmdline" ]; then
        cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
        case "$cmd" in
            *"$SR_STATE"*) kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true ;;
            *) : ;;
        esac
    fi
    for _ in $(seq 1 50); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.02
    done
    rm -f "$pid_file"
}

case "$1" in
    show)
        [ -f "$pid_file" ] && cat "$pid_file" || echo 0
        ;;
    restart)
        if fail_once restart "${SR_FAIL_RESTART:-}"; then exit 1; fi
        kill_owned_pid
        exec_line=$(sed -n 's/^ExecStart=//p' "$SR_UNITS/$2")
        export_line=$(sed -n 's/^Environment=EXPORT_CONFIG_FILE=//p' "$SR_UNITS/$2")
        [ -z "$export_line" ] || export EXPORT_CONFIG_FILE="$export_line"
        rm -f "$pid_file"
        FAKE_PID_FILE=$pid_file setsid sh -c "exec $exec_line" </dev/null >/dev/null 2>&1 &
        for _ in $(seq 1 200); do [ -s "$pid_file" ] && break; sleep 0.02; done
        ;;
    stop)
        case "$2" in
            *prepare.timer) rm -f "$timer_marker" ;;
            *prepare.service) rm -f "$prepare_marker" ;;
            *)
                # SR_INFLIGHT_ON_STOP models a request claimed exactly while the
                # bridge is being stopped: the TOCTOU the migration must survive.
                if [ "${SR_INFLIGHT_ON_STOP:-}" = 1 ] && [ -n "${SR_SPOOL:-}" ]; then
                    : > "$SR_SPOOL/processing/ffffffffffffffffffffffffffffffff.json"
                fi
                kill_owned_pid
                ;;
        esac
        ;;
    start)
        if fail_once start "${SR_FAIL_START:-}"; then exit 1; fi
        case "$2" in
            *prepare.timer)
                # SR_START_TIMER_NOOP models systemd accepting the command but the
                # timer not actually becoming active.
                [ "${SR_START_TIMER_NOOP:-}" = 1 ] || : > "$timer_marker"
                ;;
        esac
        ;;
    is-active)
        case "$3" in
            *prepare.timer) [ -f "$timer_marker" ] && exit 0 || exit 3 ;;
            *prepare.service) [ -f "$prepare_marker" ] && exit 0 || exit 3 ;;
            *) exit 3 ;;
        esac
        ;;
    daemon-reload)
        fail_once reload "${SR_FAIL_RELOAD:-}" && exit 1
        [ -n "${SR_SLEEP_RELOAD:-}" ] && sleep "$SR_SLEEP_RELOAD"
        ;;
esac
exit 0
SH
chmod +x "$BIN/systemctl"

export SR_STATE=$TMP SR_UNITS=$TMP/units SR_SHIM_LOG=$TMP/systemctl.log
export SYSTEMCTL=$BIN/systemctl BACKUP_ROOT=$TMP/backup-root
export APP_DIR=$TMP/app SWITCH_ROOT=$TMP/switch
export BRIDGE_UNIT=$TMP/units/usb-stream-tv-mixed-bridge.service
export PREPARE_UNIT=$TMP/units/usb-stream-tv-mixed-prepare.service
export BRIDGE_SERVICE=usb-stream-tv-mixed-bridge.service
export PREPARE_SERVICE=usb-stream-tv-mixed-prepare.service
export PREPARE_TIMER=usb-stream-tv-mixed-prepare.timer
unset MIXED_STATE_FILE

OLD=$APP_DIR/config/generation_g2_export.json
NEW=$APP_DIR/config/live_favorites_export_tv.json
export TARGET=$NEW PREVIOUS=$OLD
printf '%s\n' '{"schema_version":1,"identities":[{"identity":"a","status":"approved","category":"Canais","filename":"A","chosen":{"channel_id":"1001"}}]}' > "$OLD"
printf '%s\n' '{"schema_version":1,"live_root":"TV","identities":[{"identity":"b","status":"approved","category":"Abertos","filename":"B 1080","chosen":{"channel_id":"26823618"}}],"pending":[{"identity":"c"}]}' > "$NEW"

# A daemon that keeps every argument in its own argv and never holds the caller's
# pipes open (a background child with the inherited stdout would hang the runner).
cat > "$TMP/fake-bridge" <<'FB'
#!/bin/sh
exec >/dev/null 2>&1
[ -z "${FAKE_PID_FILE:-}" ] || echo $$ > "$FAKE_PID_FILE"
trap 'kill 0 2>/dev/null; exit 0' TERM INT
while :; do sleep 3600 & wait $!; done
FB
chmod +x "$TMP/fake-bridge"

STATE_FILE=$TMP/state/state.json
write_units() { # <export> [state-file]
    cat > "$BRIDGE_UNIT" <<EOF
[Unit]
Description=bridge
[Service]
Environment=EXPORT_CONFIG_FILE=$1
ExecStart=$TMP/fake-bridge --export-config $1 --state-file ${2:-$STATE_FILE}
EOF
    cat > "$PREPARE_UNIT" <<EOF
[Service]
ExecStart=/bin/true --export-config $1
EOF
}

# The active session of the switch: its manifest must be provable, otherwise the
# migration has to refuse (a rollback could not restore it).
WRITE_ACTIVE_MANIFEST="1001"
write_active_session() { # <channel id approved by the app export>
    mkdir -p "$TMP/switch/active-session"
    "$PKG_PY" - "$TMP/switch/active-session" "$1" <<'PY'
import json
import os
import sys

sys.path.insert(0, os.environ["REPO_SCRIPTS"])
from run_live_favorites_manager import file_id_for, manifest_digest

out, channel = sys.argv[1], sys.argv[2]
fid = file_id_for(channel)
entries = [{"file_id": fid, "namespace": "live", "channel_id": channel, "kind": "live",
            "folder": ["Canais"], "filename": "Canal.trp", "logical_size": 0,
            "bootstrap_id": "bootstrap-%08x" % fid},
           {"file_id": 2, "namespace": "vod", "channel_id": "a1b2c3d4", "kind": "vod",
            "folder": ["Filmes"], "filename": "F.mp4", "logical_size": 4096,
            "bootstrap_id": "", "content_revision": "md5:" + "a" * 32}]
manifest = {"schema_version": 1, "layout_version": 1, "generation": 6, "entries": entries}
manifest["manifest_id"] = manifest_digest(manifest)
json.dump(manifest, open(os.path.join(out, "favorites_manifest.json"), "w"), indent=2)
PY
    # The active session's export is the app default (channel 1001, Canais).
    printf '%s\n' '{"schema_version":1,"identities":[{"identity":"canal","status":"approved","category":"Canais","filename":"Canal","chosen":{"channel_id":"1001"}}]}' > "$APP_DIR/config/live_favorites_export.json"
    ln -sfn active-session "$TMP/switch/current"
}
PKG_PY=$REPO/../../usr/bin/python3
REPO_SCRIPTS=$REPO/scripts
[ -x "$PKG_PY" ] || PKG_PY=$(command -v python3)
export REPO_SCRIPTS

write_journal() { # <pending session name or "">
    "$PKG_PY" - "$STATE_FILE" "$TMP/app/staging" "$1" <<'PY'
import json
import os
import sys

sys.path.insert(0, os.environ["REPO_SCRIPTS"])
from run_live_favorites_manager import file_id_for, manifest_digest

state_path, staging_base, session = sys.argv[1], sys.argv[2], sys.argv[3]
state = {"active_generation": 6, "pending_generation": 7, "pending_session": session}
if session:
    state["pending_staging_dir"] = "%s/%s" % (staging_base, session)
    # A comparable pending package: its Live set is the one the previous export
    # approves, so the migration can compare it before it mutates anything.
    staging = "%s/%s" % (staging_base, session)
    os.makedirs(staging, exist_ok=True)
    fid = file_id_for("1001")
    entries = [{"file_id": fid, "namespace": "live", "channel_id": "1001",
                "kind": "live", "folder": ["Canais"], "filename": "Canal.trp",
                "logical_size": 0, "bootstrap_id": "bootstrap-%08x" % fid},
               {"file_id": 2, "namespace": "vod", "channel_id": "a1b2c3d4",
                "kind": "vod", "folder": ["Filmes"], "filename": "F.mp4",
                "logical_size": 4096, "bootstrap_id": "",
                "content_revision": "md5:" + "a" * 32}]
    manifest = {"schema_version": 1, "layout_version": 1, "generation": 7,
                "entries": entries}
    manifest["manifest_id"] = manifest_digest(manifest)
    json.dump(manifest, open(os.path.join(staging, "favorites_manifest.json"), "w"),
              indent=2)
json.dump(state, open(state_path, "w"))
PY
}

write_stale_pending() {
    mkdir -p "$TMP/app/staging/mixed-gen7-deadbeef"
    cat > "$TMP/app/staging/mixed-gen7-deadbeef/favorites_manifest.json" <<'JSON'
{"schema_version": 1, "generation": 7, "entries": [
  {"kind": "live", "folder": ["Abertos"], "filename": "SBT.trp"},
  {"kind": "vod", "folder": ["Filmes"], "filename": "F.mp4"}]}
JSON
    write_journal "mixed-gen7-deadbeef"
}

start_fake_bridge() { # <export> <pidfile> -> echoes the pid
    rm -f "$2"
    FAKE_PID_FILE=$2 setsid "$TMP/fake-bridge" --export-config "$1" \
        </dev/null >/dev/null 2>&1 &
    for _ in $(seq 1 200); do [ -s "$2" ] && break; sleep 0.02; done
    if [ ! -s "$2" ]; then echo "fake bridge never started" >&2; return 1; fi
    _sf_pid=$(cat "$2")
    printf '%s\n' "$_sf_pid" >> "$PIDS_FILE"
    echo "$_sf_pid"
}

SCRIPT=$REPO/scripts/migrate_export_to_tv.sh
SPOOL=$TMP/spool
mkdir -p "$SPOOL/requests" "$SPOOL/processing" "$SPOOL/responses" "$SPOOL/done"
# MIGRATE_CONFIRM_WINDOW=1 é a autorização explícita do operador; nos testes ela
# vem do harness, nunca de telemetria.
run() {
    env PATH="$BIN:$PATH" MIGRATE_CONFIRM_WINDOW="${MIGRATE_CONFIRM_WINDOW:-1}" \
        MIXED_BRIDGE_SPOOL="$SPOOL" sh "$SCRIPT" "$@"
}
fail() { echo "FAIL: $*" >&2; rc=1; }
bridge_argv_is() { # <pid> <export>
    tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null | grep -qF -- "--export-config $2"
}
shim_called() { grep -q -- "$1" "$TMP/systemctl.log"; }

# --- fixture: old units, running bridge, active timer, provable active session,
#     journal pointing at a still-current pending generation.
write_units "$OLD"
write_active_session "$WRITE_ACTIVE_MANIFEST"
write_journal "mixed-gen1-aaaaaaaa"
: > "$TMP/state/timer.active"
export EXPORT_CONFIG_FILE="$OLD"
BRIDGE=$(start_fake_bridge "$OLD" "$TMP/bridge.pid")
sleep 0.2

rc=0

echo "=== Test 1: check is read-only and reports the five readers ==="
out=$(run check 2>&1) || fail "check should pass on a consistent old configuration"
grep -q "bridge RUNNING pid" <<<"$out" || fail "check does not report the running process"
grep -q "consistent on the previous export" <<<"$out" || fail "check missed the pending migration"
grep -q "active session active-session restores with" <<<"$out" \
    || fail "check did not prove the active session"
grep -q "these readers do not point at" <<<"$out" \
    && fail "check logged reader noise on the happy path"
[ "$(sed -n 's/^ExecStart=.*--export-config //p' "$BRIDGE_UNIT" | cut -d' ' -f1)" = "$OLD" ] \
    || fail "check wrote to the units"
echo "OK"

echo "=== Test 2: daemon-reload alone does NOT change the running process (D8) ==="
sed -i "s|$OLD|$NEW|g" "$BRIDGE_UNIT"
"$SYSTEMCTL" daemon-reload
grep -q -- "--export-config $NEW" "$BRIDGE_UNIT" || fail "unit was not rewritten"
bridge_argv_is "$BRIDGE" "$OLD" \
    || fail "the running process unexpectedly changed its argv"
set +e
out=$(run check 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "check must FAIL when units and process disagree"
grep -q "INCONSISTENT" <<<"$out" || fail "check did not call the state inconsistent"
grep -q "bridge-process(argv)" <<<"$out" || fail "check did not name the stale process"
echo "OK (reload moves the unit, not the process)"

echo "=== Test 3: every single reader is checked, not a majority (D8) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
# Move the process' environ only: argv still says OLD, so the env reader is the
# only one disagreeing. A count-based check would call this good.
sed -i "s|EXPORT_CONFIG_FILE=$OLD|EXPORT_CONFIG_FILE=$TARGET|" "$BRIDGE_UNIT"
out=$(run check 2>&1) || true
grep -q "bridge-unit(env)" <<<"$out" || fail "the env reader was not singled out: $out"
sed -i "s|EXPORT_CONFIG_FILE=$TARGET|EXPORT_CONFIG_FILE=$OLD|" "$BRIDGE_UNIT"
echo "OK (5 readers, each verified individually)"

echo "=== Test 4: apply migrates, restarts the bridge and proves argv + env ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
: > "$TMP/systemctl.log"
out=$(run apply 2>&1) || fail "apply failed: $out"
PID=$(cat "$TMP/bridge.pid")
[ "$PID" != "$BRIDGE" ] || fail "the bridge was not restarted"
bridge_argv_is "$PID" "$NEW" || fail "restarted bridge still runs the old export"
tr '\0' '\n' < "/proc/$PID/environ" | grep -qx "EXPORT_CONFIG_FILE=$NEW" \
    || fail "restarted bridge kept the old environment"
grep -q "restart usb-stream-tv-mixed-bridge.service" "$TMP/systemctl.log" \
    || fail "apply did not restart the bridge"
grep -q "daemon-reload" "$TMP/systemctl.log" || fail "apply did not reload systemd"
grep -q -- "--export-config $NEW" "$PREPARE_UNIT" || fail "prepare unit not migrated"
grep -q "bridge pid $PID now runs with $NEW" <<<"$out" || fail "apply hid the proof"
[ -f "$TMP/state/timer.active" ] || fail "the timer was not brought back"
grep -q "do NOT use --publish" <<<"$out" || fail "apply did not warn about --publish (D9)"
out=$(run check 2>&1) || fail "check must pass after a successful apply"
grep -q "every reader already uses the target export" <<<"$out" || fail "post-apply check"
echo "OK"

echo "=== Test 5: partial failure reverts units, bridge AND timer (D1) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
rm -f "$TMP/restart.failed"
set +e
out=$(SR_FAIL_RESTART=1 run apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "apply claimed success although the restart failed"
grep -q -- "--export-config $OLD" "$BRIDGE_UNIT" || fail "bridge unit was not reverted"
grep -q -- "--export-config $OLD" "$PREPARE_UNIT" || fail "prepare unit was not reverted"
grep -q "rolled back\|everything the apply changed was undone" <<<"$out" \
    || fail "revert was not reported"
grep -q "timer usb-stream-tv-mixed-prepare.timer restarted" <<<"$out" \
    || { fail "D1: the timer was not restarted after the failure"; echo "$out" >&2; }
[ -f "$TMP/state/timer.active" ] || fail "D1: the timer is left stopped after a failure"
PID=$(cat "$TMP/bridge.pid")
bridge_argv_is "$PID" "$OLD" || fail "bridge left on the new export after a failure"
grep -q "daemon-reload" <(tail -6 "$TMP/systemctl.log") || fail "revert did not reload systemd"
echo "OK"

echo "=== Test 6: a prepare still active does not leave the timer stopped (D2) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
: > "$TMP/state/prepare.active"
set +e
out=$(MIGRATE_PREPARE_WAIT=1 run apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "apply proceeded while a prepare was running"
grep -q "a prepare run is still active" <<<"$out" || fail "the reason was not reported"
[ -f "$TMP/state/timer.active" ] || fail "D2: the timer was left stopped"
grep -q "timer usb-stream-tv-mixed-prepare.timer restarted" <<<"$out" \
    || fail "D2: restart of the timer was not reported"
grep -q -- "--export-config $OLD" "$BRIDGE_UNIT" || fail "units were touched before quiescing"
rm -f "$TMP/state/prepare.active"
echo "OK"

echo "=== Test 7: a timer that was NOT active is not started by the revert (D2) ==="
write_units "$OLD"
rm -f "$TMP/state/timer.active"
rm -f "$TMP/restart.failed"          # let the induced failure happen again
set +e
out=$(SR_FAIL_RESTART=1 run apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "apply claimed success although the restart failed"
[ ! -f "$TMP/state/timer.active" ] \
    || fail "the revert started a timer that was not running before"
rm -f "$TMP/restart.failed"
: > "$TMP/state/timer.active"
echo "OK"

echo "=== Test 8: incomplete backup is never claimed and never used (D3/D11) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
# A backup directory that looks newer but has no COMPLETE marker and is missing
# the prepare unit: it must be ignored by rollback. An isolated root keeps this
# test from seeing the complete backups the previous tests created.
ISOLATED_ROOT=$TMP/backup-incomplete
mkdir -p "$ISOLATED_ROOT/.backup-export-migrate-29990101T000000Z"
cp "$BRIDGE_UNIT" "$ISOLATED_ROOT/.backup-export-migrate-29990101T000000Z/bridge.service"
set +e
out=$(BACKUP_ROOT=$ISOLATED_ROOT run rollback 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "D3/D11: an incomplete backup must not be used"
grep -q "no complete migration backup" <<<"$out" \
    || { grep -q "backup has no\|could not restore" <<<"$out" \
         || { fail "an incomplete backup was not rejected clearly"; echo "$out" >&2; }; }
echo "OK"

echo "=== Test 9: rollback succeeds with exit 0 and the unit's own export (D4/D5) ==="
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"    # running on OLD
set +e
out=$(SR_FAIL_RESTART=1 run apply 2>&1)   # creates a complete backup, then fails
status=$?
set -e
[ "$status" -ne 0 ] || fail "the induced failure did not happen"
rm -f "$TMP/restart.failed"
# PREVIOUS is deliberately WRONG: the expected export must come from the unit.
set +e
out=$(PREVIOUS=$TMP/nonexistent-export.json run rollback 2>&1)
status=$?
set -e
[ "$status" -eq 0 ] || { fail "D4: a successful rollback must exit 0 (got $status)"; }
PID=$(cat "$TMP/bridge.pid")
bridge_argv_is "$PID" "$OLD" \
    || { fail "D5: rollback used PREVIOUS instead of the restored unit's export"; }
grep -q "rollback done" <<<"$out" || fail "rollback did not report completion"
echo "OK"

echo "=== Test 10: apply refuses when the active session cannot be proven (D7) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
ln -sfn active-session "$TMP/switch/current"
printf '%s\n' '{"schema_version":1,"identities":[{"identity":"x","status":"approved","category":"Canais","filename":"Outro","chosen":{"channel_id":"9999"}}]}' \
    > "$APP_DIR/config/live_favorites_export.json"
set +e
out=$(run apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "D7: apply migrated although a rollback could not be proven"
grep -q "refusing: the active session's export cannot be proven" <<<"$out" \
    || fail "D7: the refusal was not explained: $out"
grep -q -- "--export-config $OLD" "$BRIDGE_UNIT" || fail "D7: units were touched"
[ ! -d "$TMP/backup-root/.backup-export-migrate-$(date -u +%Y%m%d)" ] || true
write_active_session "$WRITE_ACTIVE_MANIFEST"
echo "OK"

echo "=== Test 11: an unreadable journal fails closed (D6) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
# Units that name no --state-file and no MIXED_STATE_FILE: the journal cannot be
# located, so the migration must refuse instead of assuming it is fine.
cat > "$BRIDGE_UNIT" <<EOF
[Unit]
Description=bridge
[Service]
Environment=EXPORT_CONFIG_FILE=$OLD
ExecStart=$TMP/fake-bridge --export-config $OLD
EOF
set +e
out=$(run apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "D6: apply proceeded without a readable journal"
grep -q "JOURNAL NOT READABLE" <<<"$out" \
    || fail "D6: the refusal was not explicit: $out"
grep -q -- "--export-config $OLD" "$PREPARE_UNIT" || fail "D6: units were touched"
write_units "$OLD"
echo "OK"

echo "=== Test 12: an obsoleto pending package is reported, never published ==="
write_stale_pending
out=$(run check 2>&1) || true
grep -q "pending layout STALE for" <<<"$out" || fail "the stale pending layout was not reported"
grep -q "do NOT publish this package" <<<"$out" || fail "the stale package was not called out"
grep -q "pending=7 active=6" <<<"$out" || fail "journal state was not reported"
write_journal "mixed-gen1-aaaaaaaa"
echo "OK"

echo "=== Test 13: a unit naming two different exports is refused ==="
write_units "$OLD"
cat > "$PREPARE_UNIT" <<EOF
[Service]
ExecStart=/bin/true --export-config $OLD
ExecStart=/bin/true --export-config $NEW
EOF
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
set +e
out=$(run apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "apply rewrote a unit that names two exports"
grep -q "several exports\|does not name exactly" <<<"$out" \
    || fail "the ambiguity was not reported: $out"
write_units "$OLD"
echo "OK"

echo "=== Test 14: apply refuses a missing/unusable target and touches nothing ==="
set +e
out=$(TARGET=$TMP/gone.json run apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "apply accepted a missing target"
grep -q "target export missing" <<<"$out" || fail "unexpected message: $out"
printf 'not json\n' > "$TMP/broken.json"
set +e
out=$(TARGET=$TMP/broken.json run apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "apply accepted an unusable target"
grep -q "not usable" <<<"$out" || fail "unexpected message: $out"
grep -q -- "--export-config $OLD" "$BRIDGE_UNIT" || fail "the refusals touched the units"
echo "OK"

echo "=== Test 15: apply requires the operator's window, never assumes it (N5) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
set +e
out=$(MIGRATE_CONFIRM_WINDOW=0 run apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "N5: apply ran without an authorised window"
grep -q "MIGRATE_CONFIRM_WINDOW=1" <<<"$out" || fail "N5: the requirement was not explained"
grep -q -- "--export-config $OLD" "$BRIDGE_UNIT" || fail "N5: the refusal touched the units"
echo "OK"

echo "=== Test 16: a publication in flight blocks the apply (N5) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
touch "$SPOOL/processing/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json"
set +e
out=$(run apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "N5: apply ran while a request was being processed"
grep -q "work in flight" <<<"$out" || fail "N5: in-flight work was not reported: $out"
rm -f "$SPOOL/processing/"*.json
set +e
out=$(run apply 2>&1)
status=$?
set -e
[ "$status" -eq 0 ] || fail "N5: apply should proceed once the spool is idle: $out"
out=$(MIGRATE_CONFIRM_WINDOW=1 EXPORT_CONFIG_FILE="$OLD" run check 2>&1) || true
echo "OK"

echo "=== Test 17: an incomparable pending package blocks before mutating (N2) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
# The journal is readable, but its staging directory lost its manifest: the
# migration cannot tell which export that package belongs to.
write_journal "mixed-gen7-broken"
rm -f "$TMP/app/staging/mixed-gen7-broken/favorites_manifest.json"
set +e
out=$(run apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "N2: apply ran with an incomparable pending package"
grep -q "CANNOT BE COMPARED\|UNREADABLE" <<<"$out" || fail "N2: not explained: $out"
grep -q "refusing: the journalled package could not be established" <<<"$out" \
    || fail "N2: the refusal did not name the journal"
grep -q -- "--export-config $OLD" "$BRIDGE_UNIT" || fail "N2: the refusal touched the units"
write_journal "mixed-gen1-aaaaaaaa"
echo "OK"

echo "=== Test 18: the undo never claims 'verified' over an inconsistent pre-state (N1) ==="
# Exactly the state the old D8 bug produced: prepare unit already on the new
# export while the bridge runs the old one. The apply must fail, restore what it
# can, and SAY that the readers still disagree.
write_units "$OLD"
sed -i "s|--export-config $OLD|--export-config $NEW|" "$PREPARE_UNIT"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
rm -f "$TMP/restart.failed"
set +e
out=$(SR_FAIL_RESTART=1 run apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "the induced failure did not happen"
grep -q "still disagree" <<<"$out" || fail "N1: the inconsistency was not reported: $out"
grep -q "everything the apply changed was undone and verified" <<<"$out" \
    && fail "N1: 'verified' was claimed over an inconsistent state"
grep -q "RESTORE FAILED" <<<"$out" || fail "N1: the partial undo was not flagged"
[ -f "$TMP/state/timer.active" ] || fail "N1: the timer was left stopped"
write_units "$OLD"
rm -f "$TMP/restart.failed"
echo "OK"

echo "=== Test 19: the unit rewrite is atomic and preserves mode (N3) ==="
write_units "$OLD"
chmod 640 "$BRIDGE_UNIT"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
out=$(run apply 2>&1) || fail "apply failed: $out"
[ "$(stat -c '%a' "$BRIDGE_UNIT")" = "640" ] \
    || fail "N3: the mode changed to $(stat -c '%a' "$BRIDGE_UNIT")"
grep -q "atomic, mode 0640 preserved" <<<"$out" || fail "N3: the atomic write was not reported"
[ "$(ls -1 "$TMP/units"/*.migrate-tmp.* 2>/dev/null | wc -l)" = "0" ] \
    || fail "N3: a temporary unit file was left behind"
grep -q -- "--export-config $NEW" "$BRIDGE_UNIT" || fail "N3: the unit was not rewritten"
grep -q "do NOT use --publish" <<<"$out" || fail "the --publish warning is missing"
echo "OK"

echo "=== Test 20: the backup records which export restores the active session (N9) ==="
BACKUP_DIR_LATEST=$(ls -dt "$TMP/backup-root"/.backup-export-migrate-* | head -1)
[ -f "$BACKUP_DIR_LATEST/active_session_export" ] \
    || fail "N9: the proven export was not recorded in the backup"
grep -q "live_favorites_export.json" "$BACKUP_DIR_LATEST/active_session_export" \
    || fail "N9: the recorded export is not the proven one"
grep -q "restores with" "$TMP/systemctl.log" 2>/dev/null || true
echo "OK"

echo "=== Test 21: a signal during the apply leaves nothing stopped (N4) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
rm -f "$SR_STATE"/reload.failed
# Make daemon-reload slow so the signal lands in the middle of the mutation.
SR_SLEEP_RELOAD=3 SR_STATE="$TMP" PATH="$BIN:$PATH" MIGRATE_CONFIRM_WINDOW=1 \
    MIXED_BRIDGE_SPOOL="$SPOOL" sh "$SCRIPT" apply > "$TMP/signal.log" 2>&1 &
APPLY_PID=$!
for _ in $(seq 1 100); do
    grep -q "backup in" "$TMP/signal.log" 2>/dev/null && break
    sleep 0.05
done
kill -TERM "$APPLY_PID" 2>/dev/null || true
wait "$APPLY_PID" 2>/dev/null || true
[ -f "$TMP/state/timer.active" ] \
    || fail "N4: the timer was left stopped after a signal"
grep -q "interrupted by a signal" "$TMP/signal.log" \
    || fail "N4: the interruption was not reported: $(tail -5 "$TMP/signal.log")"
grep -q -- "--export-config $OLD" "$BRIDGE_UNIT" || fail "N4: the units were left mutated"
echo "OK"

echo "=== Test 22: a corrupted journal blocks the apply (F1) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
write_journal "mixed-gen1-aaaaaaaa"
cp "$STATE_FILE" "$TMP/state.json.good"
printf 'nao-e-json{\n' > "$STATE_FILE"
set +e
out=$(run apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "F1: apply migrated with a corrupted journal"
grep -q "NOT COMPARABLE" <<<"$out" || fail "F1: the corruption was not reported: $out"
grep -q "refusing: the journalled package could not be established" <<<"$out" \
    || fail "F1: the refusal was not explicit"
grep -q -- "--export-config $OLD" "$BRIDGE_UNIT" || fail "F1: the refusal touched the units"
cp "$TMP/state.json.good" "$STATE_FILE"
echo "OK"

echo "=== Test 23: a valid journal with no pending is not corruption (F4) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
"$PKG_PY" - "$STATE_FILE" <<'PY'
import json
import sys

json.dump({"active_generation": 6, "pending_generation": None,
           "pending_session": None}, open(sys.argv[1], "w"))
PY
set +e
out=$(run apply 2>&1)
status=$?
set -e
[ "$status" -eq 0 ] || { fail "F4: a legitimate journal without pending blocked the apply"; echo "$out" >&2; }
grep -q "no pending package journalled" <<<"$out" || fail "F4: the absence was not reported"
write_journal "mixed-gen1-aaaaaaaa"
echo "OK"

echo "=== Test 24: an unknown spool is unknown state and blocks (F2) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
# A unit without --spool and no MIXED_BRIDGE_SPOOL: the publisher cannot be ruled
# out from here, so the migration must refuse instead of warning.
set +e
out=$(env PATH="$BIN:$PATH" MIGRATE_CONFIRM_WINDOW=1 sh "$SCRIPT" apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "F2: apply proceeded without locating the spool"
grep -q "spool could not be located" <<<"$out" || fail "F2: the refusal was not explicit: $out"
grep -q -- "--export-config $OLD" "$BRIDGE_UNIT" || fail "F2: the refusal touched the units"
echo "OK"

echo "=== Test 25: work claimed while stopping is detected after the stop (F2) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
rm -f "$TMP/restart.failed"
set +e
out=$(SR_INFLIGHT_ON_STOP=1 SR_SPOOL="$SPOOL" run apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "F2: a request claimed during the stop was not caught"
grep -q "still holds a claimed request after the stop" <<<"$out" \
    || { fail "F2: the interrupted publication was not reported"; echo "$out" >&2; }
grep -q "could not be stopped safely\|reconcile before publishing" <<<"$out" \
    || fail "F2: the failure was not explained"
[ -f "$TMP/state/timer.active" ] || fail "F2: the timer was left stopped"
rm -f "$SPOOL/processing/"*.json
echo "OK"

echo "=== Test 26: a live publisher blocks the bridge stop (F2) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
mkdir -p "$TMP/fake-pub"
printf '#!/bin/sh\nsleep 300\n' > "$TMP/fake-pub/mixed_generation_service.py"
chmod +x "$TMP/fake-pub/mixed_generation_service.py"
# The same argv the bridge builds for a publication, so the detection is tested
# against the real shape and not against a bare file name.
setsid sh "$TMP/fake-pub/mixed_generation_service.py" --apply --task-id a1b2c3d4 --json \
    </dev/null >/dev/null 2>&1 &
PUB_PID=$!
printf '%s\n' "$PUB_PID" >> "$PIDS_FILE"
sleep 0.3
set +e
out=$(run apply 2>&1)
status=$?
set -e
kill -TERM "-$PUB_PID" 2>/dev/null || kill -TERM "$PUB_PID" 2>/dev/null || true
[ "$status" -ne 0 ] || fail "F2: the bridge was stopped while a publisher was running"
grep -q "a publisher is already running" <<<"$out" \
    || { fail "F2: the live publisher was not reported"; echo "$out" >&2; }
grep -q -- "--export-config $OLD" "$BRIDGE_UNIT" || fail "F2: the refusal touched the units"
echo "OK"

echo "=== Test 27: a second signal does not halve the undo (F3) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
rm -f "$TMP/restart.failed" "$TMP/reload.failed"
SR_SLEEP_RELOAD=2 SR_STATE="$TMP" PATH="$BIN:$PATH" MIGRATE_CONFIRM_WINDOW=1 \
    MIXED_BRIDGE_SPOOL="$SPOOL" sh "$SCRIPT" apply > "$TMP/twosig.log" 2>&1 &
APPLY_PID=$!
for _ in $(seq 1 100); do
    grep -q "backup in" "$TMP/twosig.log" 2>/dev/null && break
    sleep 0.05
done
kill -TERM "$APPLY_PID" 2>/dev/null || true
sleep 0.2
kill -TERM "$APPLY_PID" 2>/dev/null || true
wait "$APPLY_PID" 2>/dev/null || true
grep -q -- "--export-config $OLD" "$BRIDGE_UNIT" || fail "F3: the units were left mutated"
[ -f "$TMP/state/timer.active" ] || fail "F3: the timer was left stopped after two signals"
PID=$(cat "$TMP/bridge.pid" 2>/dev/null)
[ -n "$PID" ] && kill -0 "$PID" 2>/dev/null \
    || fail "F3: the bridge was left stopped after two signals"
# The shell defers a signal while the handler runs, so a repeated signal cannot
# cut the undo: what matters is that the script reached its end (the final line)
# instead of dying with 143/130 as it did before the fix.
grep -q "migration reverted" "$TMP/twosig.log" \
    || { fail "F3: the script did not reach the end of the undo"; }
if grep -q "Killed\|Terminated" "$TMP/twosig.log"; then
    fail "F3: the script was terminated by the repeated signal"
fi
wait_status=0
wait "$APPLY_PID" 2>/dev/null || wait_status=$?
[ "$wait_status" -ne 143 ] && [ "$wait_status" -ne 130 ] \
    || fail "F3: the script died from the repeated signal (status $wait_status)"
echo "OK"

echo "=== Test 28: the rollback demands the export it proved (F5) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
rm -f "$TMP/restart.failed"
SR_FAIL_RESTART=1 run apply >/dev/null 2>&1 || true
rm -f "$TMP/restart.failed"
LATEST=$(ls -dt "$TMP/backup-root"/.backup-export-migrate-* | head -1)
grep -q "live_favorites_export.json" "$LATEST/active_session_export" \
    || fail "F5: the backup recorded no proven export"
printf '%s\n' "$TMP/nonexistent-proven.json" > "$LATEST/active_session_export"
set +e
out=$(run rollback 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "F5: the rollback accepted a recorded export that is gone"
grep -q "recorded for the active session is gone" <<<"$out" \
    || fail "F5: the reason was not reported: $out"
printf '%s\n' "$APP_DIR/config/live_favorites_export.json" > "$LATEST/active_session_export"
set +e
out=$(run rollback 2>&1)
status=$?
set -e
[ "$status" -eq 0 ] || fail "F5: the rollback failed with the proven export: $out"
grep -q "active session still matches the recorded export" <<<"$out" \
    || fail "F5: the proven export was not verified"
grep -q "EXPORT_CONFIG_FILE=.*live_favorites_export.json sh" <<<"$out" \
    || fail "F5: the switch command with the proven export was not printed"
echo "OK"

echo "=== Test 29: the rollback leaves the timer active (F7) ==="
rm -f "$TMP/state/timer.active"
set +e
out=$(run rollback 2>&1)
status=$?
set -e
[ "$status" -eq 0 ] || fail "F7: the rollback failed: $out"
[ -f "$TMP/state/timer.active" ] || fail "F7: the timer was left stopped after a rollback"
grep -q "timer usb-stream-tv-mixed-prepare.timer restarted after the rollback" <<<"$out" \
    || fail "F7: the timer restart was not reported"
echo "OK"

echo "=== Test 30: a symlinked unit is never backed up (F9) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
mkdir -p "$TMP/masked"
printf '[Service]\nEnvironment=EXPORT_CONFIG_FILE=%s\nExecStart=/bin/true --export-config %s --state-file %s\n' \
    "$OLD" "$OLD" "$STATE_FILE" > "$TMP/masked/bridge.service"
cp "$BRIDGE_UNIT" "$TMP/units/bridge.real"
ln -sfn "$TMP/masked/bridge.service" "$BRIDGE_UNIT"
set +e
out=$(run apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "F9: a symlinked unit was accepted"
grep -q "is a symlink: refusing to back it up" <<<"$out" \
    || { fail "F9: the symlink was not reported"; echo "$out" >&2; }
rm -f "$BRIDGE_UNIT"
cp "$TMP/units/bridge.real" "$BRIDGE_UNIT"
echo "OK"

echo "=== Test 31: a live publication blocks the migration before any stop (barrier) ==="
# A publication in flight holds the coordination lock SHARED (that is what the
# publisher and the bridge do). The migrator must refuse *and never stop the
# bridge*: with a real KillMode=control-group, ordering the stop wrongly is what
# kills the publisher.
write_units "$OLD"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
LOCK_UNDER_TEST=$(dirname "$STATE_FILE")/publication.lock
"$PKG_PY" "$REPO/scripts/publication_lock.py" hold --lock "$LOCK_UNDER_TEST" \
    --timeout 0 --kind publisher --ready-file "$TMP/pub.ready" \
    </dev/null >/dev/null 2>&1 &
PUB_HOLDER=$!
printf '%s\n' "$PUB_HOLDER" >> "$PIDS_FILE"
for _ in $(seq 1 200); do [ -s "$TMP/pub.ready" ] && break; sleep 0.05; done
grep -q "^locked" "$TMP/pub.ready" \
    || fail "the simulated publication did not take the shared lock"
: > "$TMP/systemctl.log"
set +e
out=$(run apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "the migration ran while a publication was in flight"
grep -q "an operation is active" <<<"$out" || fail "the refusal was not explained: $out"
grep -q "publisher" <<<"$out" || fail "the holder was not named: $out"
kill -0 "$PUB_HOLDER" 2>/dev/null \
    || fail "the publication holder was killed by the migration"
! grep -q "stop $BRIDGE_SERVICE" "$TMP/systemctl.log" \
    || fail "the bridge was stopped despite the live publication"
grep -q -- "--export-config $OLD" "$BRIDGE_UNIT" || fail "the refusal touched the units"
kill "$PUB_HOLDER" 2>/dev/null || true
wait "$PUB_HOLDER" 2>/dev/null || true
echo "OK"

echo "=== Test 32: the window is released and leaves no holder behind (N5) ==="
write_units "$OLD"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
LOCK_UNDER_TEST=$(dirname "$STATE_FILE")/publication.lock
out=$(run apply 2>&1) || fail "apply failed: $out"
grep -q "quiescence window held" <<<"$out" || fail "the window was not taken"
_lock_state=$("$PKG_PY" "$REPO/scripts/publication_lock.py" state --lock "$LOCK_UNDER_TEST" 2>&1 || true)
case "$_lock_state" in
    free*) : ;;
    *) fail "the window was not released after the apply: $_lock_state" ;;
esac
# And the migration is complete and consistent: nothing is left mid-window.
out=$(run check 2>&1) || fail "post-apply check failed: $out"
grep -q "every reader already uses the target export" <<<"$out" \
    || fail "the post-apply state is not consistent: $out"
echo "OK"

echo "=== Test 33: an unprovable coordination point keeps the migration blocked (N5) ==="
# The journal stays readable through MIXED_STATE_FILE, but the units declare no
# --state-file: the publisher's lock file cannot be derived, so the exclusion
# cannot be PROVEN. The migration must refuse instead of guessing.
printf '[Service]\nEnvironment=EXPORT_CONFIG_FILE=%s\nExecStart=%s/fake-bridge --export-config %s\n' \
    "$OLD" "$TMP" "$OLD" > "$BRIDGE_UNIT"
printf '[Service]\nExecStart=/bin/true --export-config %s\n' "$OLD" > "$PREPARE_UNIT"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
set +e
out=$(env PATH="$BIN:$PATH" MIGRATE_CONFIRM_WINDOW=1 MIXED_BRIDGE_SPOOL="$SPOOL" \
      MIXED_STATE_FILE="$STATE_FILE" sh "$SCRIPT" apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "the migration ran without a provable coordination point"
grep -q "cannot be proven shared" <<<"$out" \
    || { fail "the refusal was not explained: $out"; }
grep -q -- "--export-config $OLD" "$BRIDGE_UNIT" || fail "the refusal touched the units"
write_units "$OLD"
echo "OK"

echo "=== Test 34: a relative --state-file cannot pin the coordination point (N5) ==="
# A relative path resolves against each process's own cwd, so the bridge and the
# migrator could lock two different files. The window cannot be proven: refuse.
write_units "$OLD" "rel/state.json"
: > "$TMP/state/timer.active"
"$SYSTEMCTL" restart "$BRIDGE_SERVICE"
set +e
out=$(env PATH="$BIN:$PATH" MIGRATE_CONFIRM_WINDOW=1 MIXED_BRIDGE_SPOOL="$SPOOL" \
      MIXED_STATE_FILE="$STATE_FILE" sh "$SCRIPT" apply 2>&1)
status=$?
set -e
[ "$status" -ne 0 ] || fail "the migration ran with a relative --state-file"
grep -q "cannot be proven shared" <<<"$out" \
    || { fail "the refusal was not explained: $out"; }
grep -q -- "--export-config $OLD" "$BRIDGE_UNIT" || fail "the refusal touched the units"
write_units "$OLD"
echo "OK"

if [ "$rc" -eq 0 ]; then
    echo "ALL EXPORT MIGRATION TESTS PASSED!"
else
    echo "EXPORT MIGRATION TESTS FAILED" >&2
fi
exit "$rc"
