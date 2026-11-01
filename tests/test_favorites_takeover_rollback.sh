#!/bin/bash
# Exercise the real rollback function with a fake LUN and switch command.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/base/live-favorites/previous/activation" \
    "$tmp/usb/f_mass_storage/lun0"
cat > "$tmp/bin/busybox" <<'EOF'
#!/bin/sh
cmd=$1; shift
exec "$cmd" "$@"
EOF
chmod +x "$tmp/bin/busybox"
cat > "$tmp/switch" <<'EOF'
#!/bin/sh
echo switch >> "$TRACE"
EOF
chmod +x "$tmp/switch"
sed -n '/^lease_read() {/,/^lease_token_raw() {/ { /^lease_token_raw() {/d; p; }' \
    "$root/scripts/activate_live_favorites_tablet.sh" > "$tmp/functions"
sed -n '/^lease_token_raw() {/,/^lease_lock_held_by_other() {/ { /^lease_lock_held_by_other() {/d; p; }' \
    "$root/scripts/activate_live_favorites_tablet.sh" >> "$tmp/functions"
sed -n '/^rollback() {/,/^case "\$ACTION" in/ { /^case "\$ACTION" in/d; p; }' \
    "$root/scripts/activate_live_favorites_tablet.sh" >> "$tmp/functions"
cat > "$tmp/run" <<'EOF'
#!/bin/bash
set -eu
BASE=$TEST_ROOT/base
PKG=$BASE/live-favorites/previous
STATE=$PKG/activation
USB=$TEST_ROOT/usb
LOCK=$BASE/mode_switch.lock
LEASEFILE=$BASE/mixed_handoff.lease
BACKING=$PKG/mnt/tv_stream.img
SUPERVISOR=$STATE/supervisor.pid
BIN=$PKG/favorites_fuse
SWITCH=$TEST_ROOT/switch
BB=$TEST_ROOT/bin/busybox
TRACE=$TEST_ROOT/trace
export TRACE
stop_candidate() { :; }
release_lock() { rm -rf "$LOCK"; }
. "$TEST_ROOT/functions"
mkdir -p "$LOCK"
echo $$ > "$LOCK/pid"
printf '%s' "$BACKING" > "$USB/f_mass_storage/lun0/file"
echo 1 > "$USB/enable"
token=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
case "$1" in
 valid) printf '%s %s %s\n' "$token" 1 "$(($(date +%s)+60))" > "$LEASEFILE"; printf '%s %s\n' "$token" $$ > "$STATE/takeover.request" ;;
 expired) printf '%s %s %s\n' "$token" 1 1 > "$LEASEFILE"; printf '%s %s\n' "$token" $$ > "$STATE/takeover.request" ;;
 wrongpid) printf '%s %s %s\n' "$token" 1 "$(($(date +%s)+60))" > "$LEASEFILE"; printf '%s %s\n' "$token" 1 > "$STATE/takeover.request" ;;
 wrongtoken) printf '%s %s %s\n' "$token" 1 "$(($(date +%s)+60))" > "$LEASEFILE"; printf '%s %s\n' bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb $$ > "$STATE/takeover.request" ;;
 none) rm -f "$LEASEFILE" "$STATE/takeover.request" ;;
esac
rollback
EOF
chmod +x "$tmp/run"
for scenario in valid expired wrongpid wrongtoken none; do
    : > "$tmp/trace"
    TEST_ROOT="$tmp" "$tmp/run" "$scenario" >/dev/null
    if [ "$scenario" = valid ]; then
        [ ! -s "$tmp/trace" ]
        [ "$(cat "$tmp/base/live-favorites/previous/activation/result")" = handoff_released ]
        [ ! -s "$tmp/usb/f_mass_storage/lun0/file" ]
    else
        grep -qx switch "$tmp/trace"
        [ "$(cat "$tmp/base/live-favorites/previous/activation/result")" = restored ]
    fi
done
echo 'takeover rollback tests passed'
