#!/bin/sh
# Host-only test of watchdog command arbitration and supervised favorites handoff.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/favorites-handoff.XXXXXX")
SUP=''
cleanup() {
    [ -z "$SUP" ] || kill "$SUP" 2>/dev/null || true
    rm -rf "$TMP"
}
trap cleanup EXIT INT TERM
mkdir -p "$TMP/bin" "$TMP/base/tvwd" "$TMP/usb/f_mass_storage/lun0"
TRACE=$TMP/trace
export TRACE
cat > "$TMP/bin/busybox" <<'EOF'
#!/bin/sh
cmd=$1; shift
case "$cmd" in
    wget) printf '%s\n' "$*" >> "$TRACE"; echo none ;;
    sh) exec /bin/sh "$@" ;;
    *) exec "$cmd" "$@" ;;
esac
EOF
chmod +x "$TMP/bin/busybox"
USBTV_WATCHDOG_FUNCTIONS_ONLY=1
export USBTV_WATCHDOG_FUNCTIONS_ONLY
. "$ROOT/scripts/tv_watchdog.sh"
BASE=$TMP/base
STATE=$BASE/tvwd
USBD=$TMP/usb
BB=$TMP/bin/busybox
LOGF=$TMP/log
SERVER=http://127.0.0.1
TOKEN='test'
PKG=$BASE/live-favorites/test-package
export BASE PKG
mkdir -p "$PKG/activation" "$PKG/mnt"
cat > "$PKG/activate_live_favorites_tablet.sh" <<'EOF'
#!/bin/sh
case "$1" in
    start) while :; do sleep 1; done ;;
    rollback)
        echo rollback >> "$TRACE"
        [ ! -f "$PKG/activation/fail" ] || exit 1
        kill "$(cat "$PKG/activation/supervisor.pid")" 2>/dev/null || true
        rm -rf "$BASE/mode_switch.lock"
        rm -f "$BASE/favorites_mode.flag"
        ;;
esac
EOF
chmod +x "$PKG/activate_live_favorites_tablet.sh"
log() { :; }
send_ack() { :; }
# The fake shell verifies the requested command runs only after lock removal.
sh() {
    [ "$1" = -c ] || return 1
    [ ! -d "$BASE/mode_switch.lock" ] || { echo 'executed under lock' >> "$TRACE"; return 1; }
    printf 'executed:%s\n' "$2" >> "$TRACE"
}
start_supervisor() {
    mkdir -p "$BASE/mode_switch.lock"
    /bin/sh "$PKG/activate_live_favorites_tablet.sh" start &
    SUP=$!
    echo "$SUP" > "$BASE/mode_switch.lock/pid"
    echo "$SUP" > "$PKG/activation/supervisor.pid"
    echo session > "$BASE/favorites_mode.flag"
    echo "$PKG" > "$BASE/favorites_supervisor.path"
    echo "$PKG/mnt/tv_stream.img" > "$USBD/f_mass_storage/lun0/file"
    # Wait for the new process to exec the path used by the ownership check.
    tries=0
    until favorites_supervisor_package >/dev/null; do
        tries=$((tries + 1)); [ "$tries" -lt 50 ] || { echo 'supervisor did not start' >&2; exit 1; }
        sleep 0.02
    done
}

start_supervisor
: > "$TRACE"
poll_and_dispatch
! grep -q 'nocmd=1' "$TRACE" || { echo 'verified supervisor incorrectly blocks polling' >&2; exit 1; }
: > "$TRACE"
run_cmd 1 'sh /data/local/tmp/switch_vod.sh 123'
[ "$(cat "$STATE/rc.1")" = 0 ]
[ "$(sed -n '1p' "$TRACE")" = rollback ]
[ "$(sed -n '2p' "$TRACE")" = 'executed:sh /data/local/tmp/switch_vod.sh 123' ]
[ ! -d "$BASE/mode_switch.lock" ]
wait "$SUP" 2>/dev/null || true
SUP=''

start_supervisor
: > "$TRACE"
run_cmd 2 'sh /data/local/tmp/other.sh'
[ "$(cat "$STATE/rc.2")" = 75 ]
[ -d "$BASE/mode_switch.lock" ]
[ ! -s "$TRACE" ] || { echo 'unrelated command detached favorites' >&2; exit 1; }

: > "$PKG/activation/fail"
: > "$TRACE"
run_cmd 3 'sh /data/local/tmp/switch_live.sh'
[ "$(cat "$STATE/rc.3")" = 75 ]
[ -d "$BASE/mode_switch.lock" ]
[ "$(cat "$TRACE")" = rollback ]
rm -f "$PKG/activation/fail"

# A valid process with a different LUN must not be allowed to detach USB.
echo /some/other/backing > "$USBD/f_mass_storage/lun0/file"
: > "$TRACE"
poll_and_dispatch
grep -q 'nocmd=1' "$TRACE" || { echo 'different LUN did not suppress commands' >&2; exit 1; }
: > "$TRACE"
run_cmd 4 'sh /data/local/tmp/switch_vod.sh 123'
[ "$(cat "$STATE/rc.4")" = 75 ]
[ ! -s "$TRACE" ] || { echo 'different LUN triggered handoff' >&2; exit 1; }
echo "$PKG/mnt/tv_stream.img" > "$USBD/f_mass_storage/lun0/file"

echo 99999999 > "$BASE/mode_switch.lock/pid"
: > "$TRACE"
poll_and_dispatch
grep -q 'nocmd=1' "$TRACE" || { echo 'unverified lock did not suppress commands' >&2; exit 1; }
: > "$TRACE"
run_cmd 5 'sh /data/local/tmp/switch_vod.sh 123'
[ "$(cat "$STATE/rc.5")" = 75 ]
[ ! -s "$TRACE" ] || { echo 'unverified owner triggered handoff' >&2; exit 1; }

# ------------------------------------------------ mixed-media handoff lease
# A live foreign lease must block execution entirely and must never run the
# favorites rollback (which could interrupt a viewer or fight the publisher).
kill "$SUP" 2>/dev/null || true
wait "$SUP" 2>/dev/null || true
SUP=''
rm -rf "$BASE/mode_switch.lock"
rm -f "$BASE/favorites_mode.flag" "$BASE/favorites_supervisor.path" "$BASE/mixed_handoff.lease"
start_supervisor
: > "$TRACE"
printf '%s %s %s\n' 0123456789abcdef0123456789abcdef 4242 9999999999 > "$BASE/mixed_handoff.lease"
run_cmd 6 'sh /data/local/tmp/switch_vod.sh 123'
[ "$(cat "$STATE/rc.6")" = 75 ]
grep -q 'Handoff in progress' "$STATE/out.6"
[ ! -s "$TRACE" ] || { echo 'live lease still executed a command' >&2; exit 1; }
[ -d "$BASE/mode_switch.lock" ]
[ -f "$BASE/favorites_mode.flag" ]

# An expired lease is treated as absent: the normal handoff path runs again.
printf '%s %s %s\n' 0123456789abcdef0123456789abcdef 4242 1 > "$BASE/mixed_handoff.lease"
: > "$TRACE"
run_cmd 7 'sh /data/local/tmp/switch_vod.sh 123'
[ "$(cat "$STATE/rc.7")" = 0 ]
[ "$(sed -n '1p' "$TRACE")" = rollback ]
[ "$(sed -n '2p' "$TRACE")" = 'executed:sh /data/local/tmp/switch_vod.sh 123' ]
[ ! -d "$BASE/mode_switch.lock" ]
rm -f "$BASE/mixed_handoff.lease"
wait "$SUP" 2>/dev/null || true
SUP=''

# A malformed lease is also absent (invalid token/expiry).
start_supervisor
printf 'garbage\n' > "$BASE/mixed_handoff.lease"
: > "$TRACE"
run_cmd 8 'sh /data/local/tmp/switch_vod.sh 123'
[ "$(cat "$STATE/rc.8")" = 0 ]
[ "$(sed -n '1p' "$TRACE")" = rollback ]
[ "$(sed -n '2p' "$TRACE")" = 'executed:sh /data/local/tmp/switch_vod.sh 123' ]
rm -f "$BASE/mixed_handoff.lease"
wait "$SUP" 2>/dev/null || true
SUP=''

# Without any lease the behaviour is unchanged (regression).
start_supervisor
: > "$TRACE"
run_cmd 9 'sh /data/local/tmp/switch_vod.sh 123'
[ "$(cat "$STATE/rc.9")" = 0 ]
[ "$(sed -n '1p' "$TRACE")" = rollback ]
[ "$(sed -n '2p' "$TRACE")" = 'executed:sh /data/local/tmp/switch_vod.sh 123' ]
wait "$SUP" 2>/dev/null || true
SUP=''

echo 'watchdog favorites handoff: passed'
