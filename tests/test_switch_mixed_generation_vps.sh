#!/bin/bash
# Tests for scripts/switch_mixed_generation_vps.sh: a generation switch must
# reconfigure BOTH server-side consumers (VOD proxy allowlist + Live manager
# manifest/session) as one rollback-able set, must never kill a process it does
# not own, and must never put a token/PIN on a command line.
#
# The real start_mixed_vod_proxy_vps.sh launcher is used with stub python3/curl
# binaries, so the launcher's own validation + exec path is exercised too.
set -euo pipefail

ROOT=$(cd -- "$(dirname "$0")/.." && pwd)
SWITCH=$ROOT/scripts/switch_mixed_generation_vps.sh
REAL_PY=$(command -v python3)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/test-switch-mixed-gen.XXXXXX")
DECOY=""
DECOY2=""

# Only ever kills stub services this test started: the cmdline must name the
# per-run temp directory.
kill_stub() {
    [ -n "${1:-}" ] || return 0
    ks_file=/proc/$1/cmdline
    [ -r "$ks_file" ] || return 0
    ks_cmd=$( { tr '\0' ' ' < "$ks_file"; } 2>/dev/null || true )
    case "$ks_cmd" in
        *"$TMP"*) kill -9 "$1" 2>/dev/null || true ;;
    esac
    return 0
}

cleanup() {
    if [ -n "${DECOY2:-}" ]; then
        { kill -9 "$DECOY2"; wait "$DECOY2"; } 2>/dev/null || true
        DECOY2=""
    fi
    if [ -n "$DECOY" ]; then
        { kill -9 "$DECOY"; wait "$DECOY"; } 2>/dev/null || true
        DECOY=""
    fi
    for file in "$TMP/python.pids.seen" "$TMP/python.pids"; do
        [ -f "$file" ] || continue
        while read -r p; do
            kill_stub "$p"
        done < "$file"
    done
    rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

SWITCH_ROOT=$TMP/switch-root
APP=$TMP/app
BIN=$TMP/bin
TOKEN2="token-generation-two-0123456789abcdef"
TOKEN3="token-generation-three-0123456789abcdef"
TASK=a1b2c3d4
CHANNEL=1001
LIVE_FILE_ID=305419896

mkdir -p "$APP/scripts" "$APP/catalog_cache" "$APP/config" "$BIN"
cp "$ROOT/scripts/run_mixed_vod_proxy.py" "$APP/scripts/"
: > "$APP/scripts/run_live_favorites_manager.py"
echo 'TV_PIN=p-1234-secret' > "$APP/.env"
# The export the harness generations are built for: approving the exact Live set
# mk_gen produces, so a rollback can prove which export a session belongs to.
cat > "$APP/config/live_favorites_export.json" <<'JSON'
{"schema_version": 1, "identities": [
  {"identity": "canal", "status": "approved", "category": "Canais",
   "filename": "Canal", "chosen": {"channel_id": "1001"}}]}
JSON
echo '[]' > "$APP/catalog_cache/live_streams.json"

# ---------------------------------------------------------------- generators
mk_gen() {
    local dir=$1 gen=$2 task=$3 token=$4 channel=${5:-1001}
    mkdir -p "$dir"
    REPO_SCRIPTS="$ROOT/scripts" "$REAL_PY" - "$dir" "$gen" "$task" "$token" "$channel" <<'PY'
import json
import os
import sys

sys.path.insert(0, os.environ["REPO_SCRIPTS"])
from run_live_favorites_manager import file_id_for, manifest_digest

out, gen, task, token, channel = (sys.argv[1], int(sys.argv[2]), sys.argv[3],
                                  sys.argv[4], sys.argv[5])
fid = file_id_for(channel)
revision = "md5:" + "a" * 32
entries = [
    {"file_id": fid, "namespace": "live", "channel_id": channel, "kind": "live",
     "folder": ["Canais"], "filename": "Canal.trp", "logical_size": 0,
     "bootstrap_id": "bootstrap-%08x" % fid},
    {"file_id": 2, "namespace": "vod", "channel_id": task, "kind": "vod",
     "folder": ["Filmes"], "filename": "Filme.mp4", "logical_size": 4096,
     "bootstrap_id": "", "content_revision": revision},
]
manifest = {"schema_version": 1, "layout_version": 1, "generation": gen, "entries": entries}
manifest["manifest_id"] = manifest_digest(manifest)
with open(os.path.join(out, "favorites_manifest.json"), "w") as handle:
    json.dump(manifest, handle, indent=2)
with open(os.path.join(out, "vod_allowlist.json"), "w") as handle:
    json.dump({"tasks": [{"task_id": task, "size": 4096, "revision": revision}]}, handle, indent=2)
with open(os.path.join(out, "transport.token"), "w") as handle:
    handle.write(token + "\n")
with open(os.path.join(out, "chisel.auth"), "w") as handle:
    handle.write("chisel-auth-placeholder\n")
for name in ("transport.token", "chisel.auth"):
    os.chmod(os.path.join(out, name), 0o600)
PY
}

# ------------------------------------------------------------------- shims
cat > "$BIN/python3" <<EOF
#!/bin/bash
printf '%s\n' "\$*" >> "$TMP/python.args"
for a in "\$@"; do
    case "\$a" in
        *probe_mixed_live_endpoint.py*)
            printf '{\n  "ok": true,\n  "frames": 2\n}\n'
            exit 0 ;;
    esac
done
if [ "\${1:-}" = "-" ] || [ "\${1:-}" = "-c" ]; then
    # ``-c`` is how the switch launches the manager now (shlex + execvp): the stub
    # must delegate so the real interpreter performs the exec.
    exec "$REAL_PY" "\$@"
fi
printf '%s\n' "\$\$" >> "$TMP/python.pids"
if [ "\${MOCK_MANAGER_RC:-0}" != 0 ]; then
    case "\$*" in *run_live_favorites_manager.py*) exit "\$MOCK_MANAGER_RC" ;; esac
fi
# A one-second loop: SIGTERM kills this stub at once and leaves at most one
# short-lived child behind.
while :; do sleep 1; done
EOF

cat > "$BIN/curl" <<EOF
#!/bin/bash
printf '%s\n' "\$*" >> "$TMP/curl.args"
out=/dev/null
probe=0
while [ \$# -gt 0 ]; do
    case "\$1" in
        -o) out=\${2:-/dev/null}; shift 2 || true ;;
        -w) shift 2 || true ;;
        --max-time) shift 2 || true ;;
        -H) h=\${2:-}; shift 2 || true
            case "\$h" in @*) cat "\${h#@}" >> "$TMP/curl.headers.seen" 2>/dev/null || true ;; esac ;;
        *probe*) probe=1; shift ;;
        *) shift ;;
    esac
done
if [ "\$probe" = 1 ]; then
    code=\${MOCK_PROBE_CODE:-200}
    # One-shot failure: the first probe of this run fails and later ones
    # succeed, so a candidate start can be made to fail while the restore of
    # the superseded session still succeeds.
    if [ -n "\${MOCK_FAIL_ONCE_FILE:-}" ] && [ ! -f "\${MOCK_FAIL_ONCE_FILE}" ]; then
        : > "\${MOCK_FAIL_ONCE_FILE}"
        code=503
    fi
    if [ "\$code" = 200 ]; then
        printf '%s\n' "\${MOCK_PROBE_BODY:-OK a1b2c3d4 4096}" > "\$out"
    fi
else
    # Origin range read done by start_mixed_vod_proxy_vps.sh before it execs.
    sleep "\${MOCK_ORIGIN_SLEEP:-0}"
    code=\${MOCK_CODE:-206}
fi
printf '%s' "\$code"
EOF

for forbidden in pkill killall; do
    cat > "$BIN/$forbidden" <<EOF
#!/bin/bash
printf 'INVOKED %s\n' "\$*" >> "$TMP/FORBIDDEN.$forbidden"
exit 1
EOF
done
chmod +x "$BIN/python3" "$BIN/curl" "$BIN/pkill" "$BIN/killall"

switch_root() {
    local root=$1
    shift
    env PATH="$BIN:$PATH" SWITCH_ROOT="$root" APP_DIR="$APP" ENV_FILE="$APP/.env" \
        UPSTREAM="http://127.0.0.1:8080" \
        MOCK_CODE="${MOCK_CODE:-206}" MOCK_PROBE_CODE="${MOCK_PROBE_CODE:-200}" \
        MOCK_PROBE_BODY="${MOCK_PROBE_BODY:-OK a1b2c3d4 4096}" \
        MOCK_ORIGIN_SLEEP="${MOCK_ORIGIN_SLEEP:-0}" \
        MOCK_MANAGER_RC="${MOCK_MANAGER_RC:-0}" \
        MOCK_FAIL_ONCE_FILE="${MOCK_FAIL_ONCE_FILE:-}" \
        SWITCH_PROBE_ATTEMPTS="${SWITCH_PROBE_ATTEMPTS:-160}" \
        SWITCH_PROBE_INTERVAL="${SWITCH_PROBE_INTERVAL:-0.25}" \
        sh "$SWITCH" "$@"
}

switch() { switch_root "$SWITCH_ROOT" "$@"; }

reset_capture() {
    : > "$TMP/python.args"
    : > "$TMP/curl.args"
    : > "$TMP/curl.headers.seen"
    if [ -s "$TMP/python.pids" ]; then
        cat "$TMP/python.pids" >> "$TMP/python.pids.seen"
    fi
    : > "$TMP/python.pids"
}

state_of() { sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p" "$1" | head -n 1; }

assert_dead() {
    if ! kill -0 "$1" 2>/dev/null; then
        return 0
    fi
    state=$(sed -n 's/^State:[[:space:]]*\([A-Z]\).*/\1/p' "/proc/$1/status" 2>/dev/null || true)
    if [ "$state" = "Z" ]; then
        return 0
    fi
    echo "FAIL: pid $1 is still alive (state=${state:-unknown})" >&2
    exit 1
}

assert_alive() {
    if ! kill -0 "$1" 2>/dev/null; then
        echo "FAIL: pid $1 is not alive" >&2
        exit 1
    fi
}

GEN2=$TMP/gen-2
GEN3=$TMP/gen-3
mk_gen "$GEN2" 2 "$TASK" "$TOKEN2"
mk_gen "$GEN3" 3 "$TASK" "$TOKEN3"
SDIR2=$SWITCH_ROOT/gen-2
SDIR3=$SWITCH_ROOT/gen-3

echo "=== Test 1: a missing artifact aborts and creates nothing ==="
GEN_MISSING=$TMP/gen-missing
mk_gen "$GEN_MISSING" 2 "$TASK" "$TOKEN2"
rm -f "$GEN_MISSING/chisel.auth"
reset_capture
set +e
out=$(switch start "$GEN_MISSING" gen-missing 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "missing generation artifact: $GEN_MISSING/chisel.auth" <<<"$out"
[ ! -e "$SWITCH_ROOT" ]
[ ! -s "$TMP/python.args" ]
echo "OK"

echo "=== Test 2: a contract failure aborts after validation, starts nothing ==="
GEN_CORRUPT=$TMP/gen-corrupt
mk_gen "$GEN_CORRUPT" 4 "$TASK" "$TOKEN2"
"$REAL_PY" - "$GEN_CORRUPT/favorites_manifest.json" <<'PY'
import json
import sys

path = sys.argv[1]
data = json.load(open(path))
data["manifest_id"] = "0" * 64
json.dump(data, open(path, "w"), indent=2)
PY
reset_capture
set +e
out=$(switch start "$GEN_CORRUPT" gen-corrupt 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "generation contract invalid" <<<"$out"
grep -q 'manifest digest mismatch' <<<"$out"
# Validation really ran (python3 -) and no service was started.
grep -q '^- ' "$TMP/python.args"
! grep -q 'run_mixed_vod_proxy.py' "$TMP/python.args"
! grep -q 'run_live_favorites_manager.py' "$TMP/python.args"
[ ! -e "$SWITCH_ROOT" ]
echo "OK"

echo "=== Test 3: happy path starts both consumers and proves both byte paths ==="
reset_capture
out=$(switch start "$GEN2" gen-2 --generation 2 --app-dir "$APP" --upstream http://127.0.0.1:8080 \
    --live-port 18190 --vod-port 18191 \
    --channel-id "$CHANNEL" --live-file-id "$LIVE_FILE_ID" --live-epoch 1)
[ "$out" = "switch ok generation=2 session=gen-2 live_port=18190 vod_port=18191" ]
[ -f "$SDIR2/state.json" ]
[ "$(stat -c %a "$SDIR2")" = 700 ]
[ "$(stat -c %a "$SDIR2/state.json")" = 600 ]
for name in favorites_manifest.json vod_allowlist.json transport.token chisel.auth; do
    [ -f "$SDIR2/$name" ]
    [ "$(stat -c %a "$SDIR2/$name")" = 600 ]
done
[ "$(stat -c %a "$SWITCH_ROOT/leases/gen-2.lease")" = 600 ]
PROXY2=$(state_of "$SDIR2/state.json" proxy_pid)
MANAGER2=$(state_of "$SDIR2/state.json" manager_pid)
assert_alive "$PROXY2"
assert_alive "$MANAGER2"
"$REAL_PY" - "$SDIR2/state.json" <<'PY'
import json
import sys

state = json.load(open(sys.argv[1]))
expected = {"session", "generation", "previous_session", "previous_dir", "proxy_pid",
            "manager_pid", "ports", "started_at"}
assert set(state) == expected, sorted(state)
assert state["session"] == "gen-2"
assert state["generation"] == 2
assert state["previous_session"] == ""
assert state["previous_dir"] == ""
assert state["ports"] == {"live": 18190, "vod": 18191}
assert isinstance(state["proxy_pid"], int) and state["proxy_pid"] > 0
assert isinstance(state["manager_pid"], int) and state["manager_pid"] > 0
assert isinstance(state["started_at"], int) and state["started_at"] > 0
PY
[ "$(readlink "$SWITCH_ROOT/current")" = "$SDIR2" ]
# The VOD proxy was started through the real VPS launcher with the private dir.
grep -q -- "$APP/scripts/run_mixed_vod_proxy.py --port 18191 --allowlist $SDIR2/vod_allowlist.json --token-file $SDIR2/transport.token --upstream http://127.0.0.1:8080" "$TMP/python.args"
# The Live manager got the new manifest, token and session.
grep -q -- "$APP/scripts/run_live_favorites_manager.py --manifest $SDIR2/favorites_manifest.json --live-cache $APP/catalog_cache/live_streams.json --export-config $APP/config/live_favorites_export.json --token-file $SDIR2/transport.token --session gen-2 --profile 1080p30 --port 18190" "$TMP/python.args"
# The Live byte path was proven with the real probe CLI.
grep -q -- "probe_mixed_live_endpoint.py --host 127.0.0.1 --port 18190 --session gen-2" "$TMP/python.args"
grep -q -- "--channel-id $CHANNEL --file-id $LIVE_FILE_ID --generation 2 --epoch 1 --token-file $SDIR2/transport.token --frames 2" "$TMP/python.args"
# Credentials: only ever through a 0600 header file / a file path, never argv,
# never a log, never state.json, never /proc/<pid>/cmdline.
grep -q "Authorization: Bearer $TOKEN2" "$TMP/curl.headers.seen"
! grep -q "$TOKEN2" "$TMP/curl.args"
! grep -q "$TOKEN2" "$TMP/python.args"
! grep -q "$TOKEN2" "$SDIR2/state.json"
! grep -q "$TOKEN2" "$SDIR2/proxy.log"
! grep -q "$TOKEN2" "$SDIR2/manager.log"
! grep -qa "$TOKEN2" "/proc/$PROXY2/cmdline"
! grep -qa "$TOKEN2" "/proc/$MANAGER2/cmdline"
! grep -q 'p-1234-secret' "$TMP/curl.args"
! grep -q 'p-1234-secret' "$TMP/python.args"
! grep -q 'p-1234-secret' "$SDIR2/proxy.log"
! grep -q 'p-1234-secret' "$SDIR2/state.json"
# The credential header file must not survive the probe.
! ls "$SDIR2"/.probe.* >/dev/null 2>&1
echo "OK"

echo "=== Test 4: a second start of the same session is a no-op ==="
cp "$SDIR2/state.json" "$TMP/state.json.before"
reset_capture
out=$(switch start "$GEN2" gen-2 --generation 2 --app-dir "$APP")
[ "$out" = "already_active session=gen-2" ]
cmp -s "$SDIR2/state.json" "$TMP/state.json.before"
[ "$(state_of "$SDIR2/state.json" proxy_pid)" = "$PROXY2" ]
[ "$(state_of "$SDIR2/state.json" manager_pid)" = "$MANAGER2" ]
[ "$(wc -l < "$TMP/python.pids")" -eq 0 ]
! grep -q 'run_mixed_vod_proxy.py' "$TMP/python.args"
! grep -q 'run_live_favorites_manager.py' "$TMP/python.args"
echo "OK"

echo "=== Test 5: a proxy that cannot serve bytes rolls back and dies ==="
cp "$SDIR2/state.json" "$TMP/state2.snapshot"
CURRENT_BEFORE=$(readlink "$SWITCH_ROOT/current")
reset_capture
set +e
out=$(MOCK_PROBE_CODE=503 switch start "$GEN3" gen-3 --generation 3 --app-dir "$APP" 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q 'rejected the /probe read (HTTP 503)' <<<"$out"
grep -q '/probe' "$TMP/curl.args"
[ ! -f "$SDIR3/state.json" ]
[ ! -f "$SWITCH_ROOT/leases/gen-3.lease" ]
[ "$(readlink "$SWITCH_ROOT/current")" = "$CURRENT_BEFORE" ]
cmp -s "$SDIR2/state.json" "$TMP/state2.snapshot"
assert_alive "$PROXY2"
assert_alive "$MANAGER2"
[ "$(wc -l < "$TMP/python.pids")" -eq 2 ]
for p in $(cat "$TMP/python.pids"); do assert_dead "$p"; done
! ls "$SDIR3"/.probe.* >/dev/null 2>&1
echo "OK"

echo "=== Test 6: a listener serving a foreign allowlist is never accepted ==="
# A stale proxy of the previous generation answers 200 on the shared port with
# a task that is not in the new allowlist: the switch must not be published.
reset_capture
set +e
out=$(MOCK_PROBE_BODY='OK deadbeef 4096' SWITCH_PROBE_ATTEMPTS=8 SWITCH_PROBE_INTERVAL=0.1 \
    switch start "$GEN3" gen-3-stale --generation 3 --app-dir "$APP" 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q 'did not serve a real /probe read' <<<"$out"
grep -q '/probe' "$TMP/curl.args"
[ ! -f "$SWITCH_ROOT/gen-3-stale/state.json" ]
[ "$(readlink "$SWITCH_ROOT/current")" = "$CURRENT_BEFORE" ]
cmp -s "$SDIR2/state.json" "$TMP/state2.snapshot"
[ "$(wc -l < "$TMP/python.pids")" -eq 2 ]
for p in $(cat "$TMP/python.pids"); do assert_dead "$p"; done
echo "OK"

echo "=== Test 7: a Live manager that exits is a failed start too ==="
reset_capture
set +e
out=$(MOCK_MANAGER_RC=5 switch start "$GEN3" gen-3b --generation 3 --app-dir "$APP" 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q 'Live manager exited' <<<"$out"
[ ! -f "$SWITCH_ROOT/gen-3b/state.json" ]
[ "$(readlink "$SWITCH_ROOT/current")" = "$CURRENT_BEFORE" ]
[ "$(wc -l < "$TMP/python.pids")" -eq 2 ]
for p in $(cat "$TMP/python.pids"); do assert_dead "$p"; done
echo "OK"

echo "=== Test 8: the launcher stays kill-able while it is still validating ==="
# The origin read is slow, so the run fails while the proxy launcher has not
# exec'd yet. The launcher must still be recognised as ours and stopped.
reset_capture
set +e
out=$(MOCK_ORIGIN_SLEEP=3 SWITCH_PROBE_ATTEMPTS=2 SWITCH_PROBE_INTERVAL=0.05 \
    switch start "$GEN3" gen-3d --generation 3 --app-dir "$APP" 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q 'did not serve a real /probe read' <<<"$out"
! grep -q 'refusing to signal' <<<"$out"
[ ! -f "$SWITCH_ROOT/gen-3d/state.json" ]
[ "$(readlink "$SWITCH_ROOT/current")" = "$CURRENT_BEFORE" ]
[ "$(wc -l < "$TMP/python.pids")" -eq 1 ]
for p in $(cat "$TMP/python.pids"); do assert_dead "$p"; done
# The killed launcher never exec'd the proxy.
sleep 0.5
! grep -q 'origin verified' "$SWITCH_ROOT/gen-3d/proxy.log"
! grep -q 'run_mixed_vod_proxy.py' "$TMP/python.args"
echo "OK"

echo "=== Test 9: stop kills only its own pids and restores the previous session ==="
# The publisher stops generation N before starting N+1 (the VOD port is shared).
out=$(switch stop gen-2)
[ "$out" = "switch stop session=gen-2 restored=none" ]
assert_dead "$PROXY2"
assert_dead "$MANAGER2"
[ ! -f "$SWITCH_ROOT/leases/gen-2.lease" ]

reset_capture
# A rollback has to be able to prove the export of the session it restores: the
# harness sessions were built for this export.
out=$(EXPORT_CONFIG_FILE="$APP/config/live_favorites_export.json" \
    switch start "$GEN3" gen-3c --generation 3 --app-dir "$APP" \
    --channel-id "$CHANNEL" --live-file-id "$LIVE_FILE_ID" --live-epoch 1)
[ "$out" = "switch ok generation=3 session=gen-3c live_port=18190 vod_port=18191" ]
SDIR3C=$SWITCH_ROOT/gen-3c
"$REAL_PY" - "$SDIR3C/state.json" "$SDIR2" <<'PY'
import json
import sys

state = json.load(open(sys.argv[1]))
assert state["previous_session"] == "gen-2", state["previous_session"]
assert state["previous_dir"] == sys.argv[2], state["previous_dir"]
PY
PROXY3=$(state_of "$SDIR3C/state.json" proxy_pid)
MANAGER3=$(state_of "$SDIR3C/state.json" manager_pid)
assert_alive "$PROXY3"
assert_alive "$MANAGER3"

# A decoy that looks exactly like the manager of the session being stopped:
# only the recorded pids may be signalled.
bash -c "exec -a \"run_live_favorites_manager.py --manifest $SDIR3C/favorites_manifest.json --session gen-3c\" sleep 300" &
DECOY=$!
sleep 0.3
assert_alive "$DECOY"

out=$(switch stop gen-3c)
[ "$out" = "switch stop session=gen-3c restored=gen-2" ]
assert_dead "$PROXY3"
assert_dead "$MANAGER3"
assert_alive "$DECOY"
grep -qa 'run_live_favorites_manager.py' "/proc/$DECOY/cmdline"
[ ! -f "$SWITCH_ROOT/leases/gen-3c.lease" ]
[ "$(readlink "$SWITCH_ROOT/current")" = "$SDIR2" ]
# The previous generation was restarted for real (fresh pids, alive).
NEW_PROXY2=$(state_of "$SDIR2/state.json" proxy_pid)
NEW_MANAGER2=$(state_of "$SDIR2/state.json" manager_pid)
[ "$NEW_PROXY2" != "$PROXY2" ]
assert_alive "$NEW_PROXY2"
assert_alive "$NEW_MANAGER2"

# Safe to run twice: no second restore, no pid touched.
cp "$SDIR2/state.json" "$TMP/state2.restored"
out=$(switch stop gen-3c)
[ "$out" = "switch stop session=gen-3c restored=none" ]
cmp -s "$SDIR2/state.json" "$TMP/state2.restored"
assert_alive "$NEW_PROXY2"
assert_alive "$NEW_MANAGER2"
assert_alive "$DECOY"
{ kill -9 "$DECOY"; wait "$DECOY"; } 2>/dev/null || true
DECOY=""
echo "OK"

echo "=== Test 10: a reused pid that is not this session is never killed ==="
# Simulate pid reuse: the session state records a pid that now belongs to a
# different process which even carries the proxy script name. Only the session
# directory is proof of ownership, so it must be refused.
bash -c 'exec -a "run_mixed_vod_proxy.py --port 18191" sleep 300' &
DECOY2=$!
sleep 0.2
REUSED_DIR=$SWITCH_ROOT/gen-reused
mkdir -p "$REUSED_DIR"
chmod 700 "$REUSED_DIR"
"$REAL_PY" - "$REUSED_DIR/state.json" "$DECOY2" <<'PY'
import json
import sys

state = {"session": "gen-reused", "generation": 9, "previous_session": "",
         "previous_dir": "", "proxy_pid": int(sys.argv[2]), "manager_pid": 0,
         "ports": {"live": 18190, "vod": 18191}, "started_at": 1}
with open(sys.argv[1], "w") as handle:
    json.dump(state, handle, indent=2)
PY
chmod 600 "$REUSED_DIR/state.json"
out=$(switch stop gen-reused 2>"$TMP/stop-reused.err")
[ "$out" = "switch stop session=gen-reused restored=none" ]
grep -q 'refusing to signal' "$TMP/stop-reused.err"
assert_alive "$DECOY2"
{ kill -9 "$DECOY2"; wait "$DECOY2"; } 2>/dev/null || true
DECOY2=""
echo "OK"

echo "=== Test 11: status reports every known session with alive/dead pids ==="
out=$(switch status)
grep -q "^session=gen-2 .*proxy=alive .*manager=alive current=yes$" <<<"$out"
grep -q "^session=gen-3c .*proxy=dead .*manager=dead current=no$" <<<"$out"
# Sessions that never reached state.json are not known sessions.
! grep -q '^session=gen-3 ' <<<"$out"
one=$(switch status gen-2)
grep -q "^session=gen-2 .*current=yes$" <<<"$one"
[ "$(wc -l <<<"$one")" -eq 1 ]
missing=$(switch status no-such-session)
[ "$missing" = "session=no-such-session state=missing" ]
echo "OK"

echo "=== Test 12: process-wide killers were never used ==="
[ ! -e "$TMP/FORBIDDEN.pkill" ]
[ ! -e "$TMP/FORBIDDEN.killall" ]
echo "OK"

echo "=== Test 13: --manager-cmd replaces the default manager ==="
# Isolated switch root: this start/stop pair must not disturb the sessions the
# other tests observe through $SWITCH_ROOT.
ALT_ROOT=$TMP/alt-root
CMD_SDIR=$ALT_ROOT/gen-cmd
custom_cmd="$BIN/python3 $APP/scripts/run_live_favorites_manager.py --manifest $CMD_SDIR/favorites_manifest.json --token-file $CMD_SDIR/transport.token --session gen-cmd --port 18190"
reset_capture
out=$(switch_root "$ALT_ROOT" start "$GEN2" gen-cmd --generation 2 --app-dir "$APP" --manager-cmd "$custom_cmd")
[ "$out" = "switch ok generation=2 session=gen-cmd live_port=18190 vod_port=18191" ]
# The custom command was used (the default one carries --live-cache).
grep -q -- "run_live_favorites_manager.py --manifest $CMD_SDIR/favorites_manifest.json --token-file $CMD_SDIR/transport.token --session gen-cmd --port 18190" "$TMP/python.args"
! grep -q -- '--live-cache' "$TMP/python.args"
CMD_PID=$(state_of "$CMD_SDIR/state.json" manager_pid)
assert_alive "$CMD_PID"
out=$(switch_root "$ALT_ROOT" stop gen-cmd)
[ "$out" = "switch stop session=gen-cmd restored=none" ]
assert_dead "$CMD_PID"
[ ! -f "$ALT_ROOT/leases/gen-cmd.lease" ]
echo "OK"

echo "=== Test 14: --supersede stops only the previous session and stop restores it ==="
SUP_ROOT=$TMP/sup-root
SUP_SDIR_A=$SUP_ROOT/gen-a
SUP_SDIR_B=$SUP_ROOT/gen-b
reset_capture
out=$(switch_root "$SUP_ROOT" start "$GEN2" gen-a --generation 2 --app-dir "$APP" --live-port 18190 --vod-port 18191)
[ "$out" = "switch ok generation=2 session=gen-a live_port=18190 vod_port=18191" ]
A_PROXY=$(state_of "$SUP_SDIR_A/state.json" proxy_pid)
A_MGR=$(state_of "$SUP_SDIR_A/state.json" manager_pid)
assert_alive "$A_PROXY"
assert_alive "$A_MGR"

reset_capture
out=$(switch_root "$SUP_ROOT" start "$GEN3" gen-b --generation 3 --app-dir "$APP" --supersede gen-a)
[ "$out" = "switch ok generation=3 session=gen-b live_port=18190 vod_port=18191" ]
# Only the previous session's own processes were stopped, never a blanket kill.
assert_dead "$A_PROXY"
assert_dead "$A_MGR"
B_PROXY=$(state_of "$SUP_SDIR_B/state.json" proxy_pid)
B_MGR=$(state_of "$SUP_SDIR_B/state.json" manager_pid)
assert_alive "$B_PROXY"
assert_alive "$B_MGR"
[ "$(readlink "$SUP_ROOT/current")" = "$SUP_SDIR_B" ]
"$REAL_PY" - "$SUP_SDIR_B/state.json" "$SUP_SDIR_A" <<'PY'
import json
import sys

state = json.load(open(sys.argv[1]))
assert state["previous_session"] == "gen-a", state["previous_session"]
assert state["previous_dir"] == sys.argv[2], state["previous_dir"]
PY
# The superseded session's directory and lease are never deleted by supersede.
[ -f "$SUP_SDIR_A/state.json" ]
[ -f "$SUP_ROOT/leases/gen-a.lease" ]

# Rollback of the set: stop the new session brings the superseded one back.
out=$(switch_root "$SUP_ROOT" stop gen-b)
[ "$out" = "switch stop session=gen-b restored=gen-a" ]
assert_dead "$B_PROXY"
assert_dead "$B_MGR"
NEW_A_PROXY=$(state_of "$SUP_SDIR_A/state.json" proxy_pid)
[ "$NEW_A_PROXY" != "$A_PROXY" ]
assert_alive "$NEW_A_PROXY"
assert_alive "$(state_of "$SUP_SDIR_A/state.json" manager_pid)"
[ "$(readlink "$SUP_ROOT/current")" = "$SUP_SDIR_A" ]
echo "OK"

echo "=== Test 15: a failed supersede start restores the superseded session ==="
reset_capture
A_PROXY_NOW=$(state_of "$SUP_SDIR_A/state.json" proxy_pid)
FAIL_ONCE=$TMP/fail-once.marker
rm -f "$FAIL_ONCE"
set +e
out=$(MOCK_FAIL_ONCE_FILE="$FAIL_ONCE" switch_root "$SUP_ROOT" start "$GEN3" gen-c --generation 3 \
    --app-dir "$APP" --supersede gen-a 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q 'rejected the /probe read (HTTP 503)' <<<"$out"
grep -q 'restoring it' <<<"$out"
# The verdict line proves the restore really happened.
grep -q '^switch failed previous_restored=yes reason=' <<<"$out"
# The failed candidate left nothing behind and the working generation is back.
[ ! -f "$SUP_ROOT/gen-c/state.json" ]
[ ! -f "$SUP_ROOT/leases/gen-c.lease" ]
[ "$(readlink "$SUP_ROOT/current")" = "$SUP_SDIR_A" ]
RESTORED_A=$(state_of "$SUP_SDIR_A/state.json" proxy_pid)
[ "$RESTORED_A" != "$A_PROXY_NOW" ]
assert_alive "$RESTORED_A"
assert_alive "$(state_of "$SUP_SDIR_A/state.json" manager_pid)"
# No blanket killer was ever used by the rollback either.
[ ! -e "$TMP/FORBIDDEN.pkill" ]
[ ! -e "$TMP/FORBIDDEN.killall" ]
echo "OK"

echo "=== Test 16: --supersede refuses a session that is not the current one ==="
reset_capture
BEFORE_CURRENT=$(readlink "$SUP_ROOT/current")
set +e
out=$(switch_root "$SUP_ROOT" start "$GEN3" gen-d --generation 3 --app-dir "$APP" \
    --supersede gen-nowhere 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q 'is not the current session' <<<"$out"
[ ! -f "$SUP_ROOT/gen-d/state.json" ]
[ "$(readlink "$SUP_ROOT/current")" = "$BEFORE_CURRENT" ]
assert_alive "$(state_of "$SUP_SDIR_A/state.json" proxy_pid)"
! grep -q 'run_mixed_vod_proxy.py' "$TMP/python.args"
echo "OK"

echo "=== Test 15b: an unrecoverable restore is reported as such, never claimed ==="
rm -f "$FAIL_ONCE"
set +e
out=$(MOCK_PROBE_CODE=503 switch_root "$SUP_ROOT" start "$GEN3" gen-c2 --generation 3 \
    --app-dir "$APP" --supersede gen-a 2>&1)
rc=$?
set -e
[ "$rc" -ne 0 ]
# The restore could not succeed with the same broken environment, and the
# script says so instead of pretending the previous generation is healthy.
grep -q '^switch failed previous_restored=no reason=' <<<"$out"
grep -q 'could not restore superseded session gen-a' <<<"$out"
[ ! -f "$SUP_ROOT/gen-c2/state.json" ]
[ "$(readlink "$SUP_ROOT/current")" = "$SUP_SDIR_A" ]
echo "OK"

echo "=== Test 17: rollback reinstates the export of the restored session ==="
# Migrating the Live export must be reversible: after N+1 runs with export B,
# stopping it has to bring N back with export A, otherwise the manager rejects
# its own manifest and Live stays down.
EXP_A=$TMP/export-A.json
EXP_B=$TMP/export-B.json
printf '%s\n' '{"schema_version":1,"generation":1,"live_root":"TV","identities":[]}' > "$EXP_A"
printf '%s\n' '{"schema_version":1,"generation":2,"identities":[]}' > "$EXP_B"
REST_ROOT=$TMP/export-root
reset_capture
out=$(EXPORT_CONFIG_FILE="$EXP_A" switch_root "$REST_ROOT" start "$GEN2" exp-a \
    --generation 2 --app-dir "$APP")
[ "$out" = "switch ok generation=2 session=exp-a live_port=18190 vod_port=18191" ]
grep -q -- "--export-config $EXP_A" "$TMP/python.args"
grep -q "^export_config=$EXP_A$" "$REST_ROOT/leases/exp-a.lease"
reset_capture
# The publisher supersedes the previous generation: that is what stops exp-a and
# records it as the session a rollback must bring back.
out=$(EXPORT_CONFIG_FILE="$EXP_B" switch_root "$REST_ROOT" start "$GEN3" exp-b \
    --generation 3 --app-dir "$APP" --supersede exp-a)
[ "$out" = "switch ok generation=3 session=exp-b live_port=18190 vod_port=18191" ]
reset_capture
out=$(switch_root "$REST_ROOT" stop exp-b)
[ "$out" = "switch stop session=exp-b restored=exp-a" ]
# The restored manager must be launched with A, never with the caller's B.
grep -q -- "--export-config $EXP_A" "$TMP/python.args" \
    || { echo "FAIL: restored session used the wrong export" >&2
         echo "--- python.args:" >&2; cat "$TMP/python.args" >&2
         echo "--- exp-b/restore.log:" >&2; tail -20 "$REST_ROOT/exp-b/restore.log" >&2
         echo "--- mangager args capturados:" >&2; cat "$TMP/python.args" >&2
         exit 1; }
! grep -q -- "--export-config $EXP_B" "$TMP/python.args" \
    || { echo "FAIL: restored session inherited the new export" >&2; exit 1; }
assert_alive "$(state_of "$REST_ROOT/exp-a/state.json" manager_pid)"
# And the lease of the restored session is untouched, so a further stop is inert.
cp "$REST_ROOT/exp-a/state.json" "$TMP/exp-a.snapshot"
out=$(switch_root "$REST_ROOT" stop exp-b)
[ "$out" = "switch stop session=exp-b restored=none" ]
cmp -s "$REST_ROOT/exp-a/state.json" "$TMP/exp-a.snapshot"
echo "OK"

echo "=== Test 18: a legacy lease without export_config is proven, not inherited ==="
# Generations 6 and older carry leases written before the field existed. After
# the export migrates, inheriting the caller's export would start the restored
# manager with an export that does not match its own manifest. The export must be
# PROVEN by matching the session manifest against the candidate exports.
LEG_EXPORT=$APP/config/legacy-export-A.json
cat > "$LEG_EXPORT" <<'JSON'
{"schema_version": 1, "generation": 1, "live_root": "TV", "identities": [
  {"identity": "canal-a", "status": "approved", "category": "Abertos",
   "filename": "Canal A 1080",
   "chosen": {"channel_id": "1001", "region": "rj", "native": "1920x1080"}}]}
JSON
# A manifest that matches LEG_EXPORT exactly (real file_id/bootstrap rules).
mk_gen_leg() {
    local dir=$1 gen=$2 task=$3 token=$4
    mkdir -p "$dir"
    REPO_SCRIPTS="$ROOT/scripts" "$REAL_PY" - "$dir" "$gen" "$task" "$token" <<'PY'
import json
import os
import sys

sys.path.insert(0, os.environ["REPO_SCRIPTS"])
from run_live_favorites_manager import file_id_for, manifest_digest

out, gen, task, token = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
fid = file_id_for("1001")
revision = "md5:" + "b" * 32
entries = [
    {"file_id": fid, "namespace": "live", "channel_id": "1001", "kind": "live",
     "folder": ["TV", "Abertos"], "filename": "Canal A 1080.trp", "logical_size": 0,
     "bootstrap_id": "bootstrap-%08x" % fid},
    {"file_id": 3, "namespace": "vod", "channel_id": task, "kind": "vod",
     "folder": ["Filmes"], "filename": "Filme.mp4", "logical_size": 4096,
     "bootstrap_id": "", "content_revision": revision},
]
manifest = {"schema_version": 1, "layout_version": 1, "generation": gen, "entries": entries}
manifest["manifest_id"] = manifest_digest(manifest)
with open(os.path.join(out, "favorites_manifest.json"), "w") as handle:
    json.dump(manifest, handle, indent=2)
with open(os.path.join(out, "vod_allowlist.json"), "w") as handle:
    json.dump({"tasks": [{"task_id": task, "size": 4096, "revision": revision}]}, handle, indent=2)
with open(os.path.join(out, "transport.token"), "w") as handle:
    handle.write(token + "\n")
with open(os.path.join(out, "chisel.auth"), "w") as handle:
    handle.write("chisel-auth-placeholder\n")
for name in ("transport.token", "chisel.auth"):
    os.chmod(os.path.join(out, name), 0o600)
PY
}
LEG_ROOT=$TMP/legacy-root
GEN_LEG=$TMP/gen-leg
mk_gen_leg "$GEN_LEG" 2 "$TASK" "$(printf 'l%.0s' $(seq 1 40))"
reset_capture
out=$(EXPORT_CONFIG_FILE="$LEG_EXPORT" switch_root "$LEG_ROOT" start "$GEN_LEG" leg-a \
    --generation 2 --app-dir "$APP")
[ "$out" = "switch ok generation=2 session=leg-a live_port=18190 vod_port=18191" ] \
    || { echo "FAIL: legacy session did not start: $out" >&2; exit 1; }
# Simulate the lease an older revision wrote: same session, no export_config.
grep -v '^export_config=' "$LEG_ROOT/leases/leg-a.lease" > "$TMP/leg-a.lease.new"
mv "$TMP/leg-a.lease.new" "$LEG_ROOT/leases/leg-a.lease"
! grep -q '^export_config=' "$LEG_ROOT/leases/leg-a.lease"
reset_capture
out=$(EXPORT_CONFIG_FILE="$EXP_B" switch_root "$LEG_ROOT" start "$GEN3" leg-b \
    --generation 3 --app-dir "$APP" --supersede leg-a)
[ "$out" = "switch ok generation=3 session=leg-b live_port=18190 vod_port=18191" ] \
    || { echo "FAIL: supersede failed: $out" >&2; exit 1; }
reset_capture
out=$(EXPORT_CONFIG_FILE="$EXP_B" switch_root "$LEG_ROOT" stop leg-b)
[ "$out" = "switch stop session=leg-b restored=leg-a" ] \
    || { echo "FAIL: legacy restore did not happen: $out" >&2; exit 1; }
grep -q -- "--export-config $LEG_EXPORT" "$TMP/python.args" \
    || { echo "FAIL: restored legacy session did not use the proven export" >&2
         echo "--- python.args:" >&2; cat "$TMP/python.args" >&2
         echo "--- leg-b/restore.log:" >&2; tail -20 "$LEG_ROOT/leg-b/restore.log" >&2
         exit 1; }
! grep -q -- "--export-config $EXP_B" "$TMP/python.args" \
    || { echo "FAIL: legacy session inherited the caller export" >&2; exit 1; }
grep -q "restoring with proven export $LEG_EXPORT" "$LEG_ROOT/leg-b/restore.log" \
    || { echo "FAIL: the export was not reported as proven" >&2
         tail -20 "$LEG_ROOT/leg-b/restore.log" >&2; exit 1; }
assert_alive "$(state_of "$LEG_ROOT/leg-a/state.json" manager_pid)"
echo "OK (proven export: legacy lease restored with its own export)"

echo "=== Test 18b: an unprovable legacy lease warns, never claims a proof ==="
# When no candidate export matches the session manifest (for instance because the
# export that built it is gone), the caller's export is still used -- that is the
# pre-fix behaviour -- but the log must say the export is unproven, so an operator
# can tell a proven rollback from a guessed one.
REF_ROOT=$TMP/refuse-root
GEN_REF=$TMP/gen-ref
# The session is built for LEG_EXPORT's set but its lease will carry nothing, and
# every candidate except the caller's diverges (EXP_B has an empty identity list,
# the app export approves 1001 under a different category): no proof is possible.
mk_gen_leg "$GEN_REF" 2 "$TASK" "$(printf 'r%.0s' $(seq 1 40))"
reset_capture
out=$(EXPORT_CONFIG_FILE="$LEG_EXPORT" switch_root "$REF_ROOT" start "$GEN_REF" ref-a \
    --generation 2 --app-dir "$APP")
[ "$out" = "switch ok generation=2 session=ref-a live_port=18190 vod_port=18191" ] \
    || { echo "FAIL: ref session did not start: $out" >&2; exit 1; }
grep -v '^export_config=' "$REF_ROOT/leases/ref-a.lease" > "$TMP/ref-a.lease.new"
mv "$TMP/ref-a.lease.new" "$REF_ROOT/leases/ref-a.lease"
reset_capture
out=$(EXPORT_CONFIG_FILE="$EXP_B" switch_root "$REF_ROOT" start "$GEN3" ref-b \
    --generation 3 --app-dir "$APP" --supersede ref-a)
[ "$out" = "switch ok generation=3 session=ref-b live_port=18190 vod_port=18191" ] \
    || { echo "FAIL: supersede failed: $out" >&2; exit 1; }
# Nothing on disk matches the session any more (the export that built it is gone):
# this is exactly the case where a proof is impossible.
mv "$LEG_EXPORT" "$TMP/legacy-export-A.json.away"
reset_capture
out=$(EXPORT_CONFIG_FILE="$EXP_B" switch_root "$REF_ROOT" stop ref-b)
[ "$out" = "switch stop session=ref-b restored=ref-a" ] \
    || { echo "FAIL: unexpected stop output: $out" >&2; exit 1; }
grep -q "warning: export of ref-a could not be proven; using " \
    "$REF_ROOT/ref-b/restore.log" \
    || { echo "FAIL: the unproven export was not called out" >&2
         tail -20 "$REF_ROOT/ref-b/restore.log" >&2; exit 1; }
! grep -q "restoring with proven export" "$REF_ROOT/ref-b/restore.log" \
    || { echo "FAIL: an unproven export was reported as proven" >&2; exit 1; }
# The caller's export is the one used, exactly as before the fix.
grep -q -- "--export-config $EXP_B" "$TMP/python.args" \
    || { echo "FAIL: the fallback export was not used" >&2
         echo "EXP_B=$EXP_B" >&2; echo "--- python.args:" >&2
         cat "$TMP/python.args" >&2
         echo "--- ref-b/restore.log:" >&2; tail -20 "$REF_ROOT/ref-b/restore.log" >&2
         exit 1; }
# With no usable caller export either, an unprovable session is refused outright:
# there is nothing safe to launch it with. The export path is passed explicitly
# (an empty value has no effect because the default is substituted), pointing at
# a file that does not exist: that is what an unusable export looks like.
# The restore that just ran re-started ref-a, so its lease now records the export
# the fallback used; strip it again so the session stays unprovable, and run a
# fresh supersede/stop cycle: that is what reaches the refusal.
grep -v '^export_config=' "$REF_ROOT/leases/ref-a.lease" > "$TMP/ref-a.lease.new"
mv "$TMP/ref-a.lease.new" "$REF_ROOT/leases/ref-a.lease"
out=$(EXPORT_CONFIG_FILE="$EXP_B" switch_root "$REF_ROOT" start "$GEN3" ref-c \
    --generation 3 --app-dir "$APP" --supersede ref-a)
[ "$out" = "switch ok generation=3 session=ref-c live_port=18190 vod_port=18191" ] \
    || { echo "FAIL: second supersede failed: $out" >&2; exit 1; }
grep -v '^export_config=' "$REF_ROOT/leases/ref-a.lease" > "$TMP/ref-a.lease.new"
mv "$TMP/ref-a.lease.new" "$REF_ROOT/leases/ref-a.lease"
reset_capture
out=$(EXPORT_CONFIG_FILE="$TMP/missing-export.json" \
    switch_root "$REF_ROOT" stop ref-c)
echo "$out" | grep -q "^switch stop session=ref-c restored=" \
    || { echo "FAIL: unexpected stop output: $out" >&2; exit 1; }
grep -q "refusing to restore ref-a: export .* is unusable and none was proven" \
    "$REF_ROOT/ref-c/restore.log" \
    || { echo "FAIL: an unprovable session was restored without a usable export" >&2
         tail -20 "$REF_ROOT/ref-c/restore.log" >&2; exit 1; }
mv "$TMP/legacy-export-A.json.away" "$LEG_EXPORT"
echo "OK (unprovable export warned about, never presented as proven)"

echo "=== Test 19: the restored manager keeps an export path containing & literal ==="
# The N7 fix: rewriting the recorded manager command must not treat the export
# path as a sed replacement, where & means "the whole match".
# The ampersand goes into the FILE NAME through a variable, so the harness itself
# never needs to quote it inside a heredoc.
AMP_DIR=${TMP}/amp-dir
AMP_FILE='leg&export.json'
AMP_JSON="$AMP_DIR/$AMP_FILE"
mkdir -p "$AMP_DIR"
cat > "$AMP_JSON" <<'JSON'
{"schema_version": 1, "generation": 1, "live_root": "TV", "identities": [
  {"identity": "canal-a", "status": "approved", "category": "Abertos",
   "filename": "Canal A 1080",
   "chosen": {"channel_id": "1001", "region": "rj", "native": "1920x1080"}}]}
JSON
AMP_ROOT=$TMP/amp-root
GEN_AMP=$TMP/gen-amp
mk_gen_leg "$GEN_AMP" 2 "$TASK" "$(printf 'a%.0s' $(seq 1 40))"
reset_capture
out=$(EXPORT_CONFIG_FILE="$AMP_JSON" switch_root "$AMP_ROOT" start "$GEN_AMP" amp-a \
    --generation 2 --app-dir "$APP")
[ "$out" = "switch ok generation=2 session=amp-a live_port=18190 vod_port=18191" ] \
    || { echo "FAIL: amp session did not start: $out" >&2; exit 1; }
# Force the proof path: the lease must not carry the export it was started with.
grep -v '^export_config=' "$AMP_ROOT/leases/amp-a.lease" > "$TMP/amp-a.lease.new"
mv "$TMP/amp-a.lease.new" "$AMP_ROOT/leases/amp-a.lease"
reset_capture
out=$(EXPORT_CONFIG_FILE="$AMP_JSON" switch_root "$AMP_ROOT" start "$GEN3" amp-b \
    --generation 3 --app-dir "$APP" --supersede amp-a)
[ "$out" = "switch ok generation=3 session=amp-b live_port=18190 vod_port=18191" ] \
    || { echo "FAIL: supersede failed: $out" >&2; exit 1; }
reset_capture
out=$(EXPORT_CONFIG_FILE="$AMP_JSON" switch_root "$AMP_ROOT" stop amp-b)
[ "$out" = "switch stop session=amp-b restored=amp-a" ] \
    || { echo "FAIL: amp restore did not happen: $out" >&2; exit 1; }
# The final argv written by the shim: one line, the export path literal (the
# quotes the switch added were consumed by the shell), ampersand intact.
# Two lines matter: the command the switch built (quoted, so `sh -c` keeps the
# path whole) and the argv the manager really received (quotes consumed).
grep -qF -- "--export-config '$AMP_JSON'" "$TMP/python.args" \
    || { echo "FAIL: the switch did not build the quoted ampersand path" >&2
         grep -- "export-config" "$TMP/python.args" >&2; exit 1; }
grep -qF -- "--export-config $AMP_JSON --token-file" "$TMP/python.args" \
    || { echo "FAIL: the manager did not receive the literal ampersand path" >&2
         grep -- "export-config" "$TMP/python.args" >&2
         tail -5 "$AMP_ROOT/amp-b/restore.log" >&2; exit 1; }
# A sed-based rewrite would have replaced the ampersand with the whole match,
# producing a path like ".../leg--export-config .../leg&export.jsonexport.json".
! grep -q "leg--export-config" "$TMP/python.args" \
    || { echo "FAIL: the manager command was mangled by a regex rewrite" >&2
         grep -- "leg--export-config" "$TMP/python.args" >&2; exit 1; }
grep -qF -- "restoring with proven export $AMP_JSON" "$AMP_ROOT/amp-b/restore.log" \
    || { echo "FAIL: the proven export was not used" >&2
         tail -5 "$AMP_ROOT/amp-b/restore.log" >&2; exit 1; }
assert_alive "$(state_of "$AMP_ROOT/amp-a/state.json" manager_pid)"
echo "OK (literal ampersand path preserved through the restore)"

echo "ALL MIXED GENERATION SWITCH TESTS PASSED!"
