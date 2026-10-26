#!/system/xbin/busybox sh
# Supervised, reversible tablet-only activation. Does not replace system binaries.
# Run via: busybox setsid busybox sh THIS start PACKAGE_DIR 127.0.0.1:PORT SESSION
# Return to installed Single Live via: busybox sh THIS rollback PACKAGE_DIR
set -eu
umask 077

BB=/system/xbin/busybox
BASE=/data/local/tmp
USB=/sys/class/android_usb/android0
LOCK=$BASE/mode_switch.lock
SWITCH=/system/xbin/switch_tv_mode.sh
[ -x "$SWITCH" ] || SWITCH=$BASE/switch_tv_mode.sh
ACTION=${1:-}
PKG=${2:-}

die() { echo "[favorites] $*" >&2; exit 1; }
case "$PKG" in
    /data/local/tmp/live-favorites/*) ;;
    *) die 'package must be under /data/local/tmp/live-favorites/' ;;
esac
case "$PKG" in *[!a-zA-Z0-9_./-]*|*/../*|*/..|*/./*) die 'unsafe package path' ;; esac
[ -d "$PKG" ] || die 'package directory absent'
STATE=$PKG/activation
MNT=$PKG/mnt
BACKING=$MNT/tv_stream.img
BIN=$PKG/favorites_fuse
PIDFILE=$STATE/candidate.pid
SUPERVISOR=$STATE/supervisor.pid
LOG=$STATE/fuse.log
TUNNEL=$PKG/favorites_tun
TUNNEL_PID=$STATE/tunnel.pid

same_process() { # exact private executable path must occur in cmdline
    _pid=$1; _path=$2
    case "$_pid" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$_pid" 2>/dev/null || return 1
    [ -r "/proc/$_pid/cmdline" ] || return 1
    "$BB" tr '\000' '\n' < "/proc/$_pid/cmdline" | "$BB" grep -F -x -q -- "$_path"
}

release_lock() {
    _owner=$(cat "$LOCK/pid" 2>/dev/null || true)
    [ "$_owner" = "${LOCK_OWNER:-$$}" ] && rm -rf "$LOCK"
    return 0
}

stop_candidate() {
    _candidate=$(cat "$PIDFILE" 2>/dev/null || true)
    if same_process "$_candidate" "$BIN"; then
        kill "$_candidate" 2>/dev/null || true
        sleep 0.2
        same_process "$_candidate" "$BIN" && kill -9 "$_candidate" 2>/dev/null || true
    fi
    "$BB" umount -l "$MNT" 2>/dev/null || true
    rm -f "$PIDFILE"
}

stop_tunnel() {
    _tpid=$(cat "$TUNNEL_PID" 2>/dev/null || true)
    if same_process "$_tpid" "$TUNNEL"; then
        kill "$_tpid" 2>/dev/null || true
        sleep 0.2
        same_process "$_tpid" "$TUNNEL" && kill -9 "$_tpid" 2>/dev/null || true
    fi
    rm -f "$TUNNEL_PID"
}

check_tunnel() {
    # Invalid, secret-free preamble: the manager must reject it before opening
    # any provider. Seeing its exact reply proves the forwarded TCP path.
    _reply=$(printf 'PROBE\n' | "$BB" nc -w 2 127.0.0.1 "$PORT" 2>/dev/null || true)
    [ "$_reply" = 'ERR source unavailable' ]
}

rollback() {
    trap - EXIT INT TERM
    # Only detach if this test still owns the LUN. An external mode switch
    # must not be replaced by an old supervisor finishing late.
    _lun=$(cat "$USB/f_mass_storage/lun0/file" 2>/dev/null || true)
    _recover=0
    [ "$_lun" = "$BACKING" ] && _recover=1
    if [ -z "$_lun" ] && [ "${USB_CHANGED:-0}" = 1 ]; then
        _recover=1
    fi
    if [ "$_recover" = 1 ]; then
        echo 0 > "$USB/enable" 2>/dev/null || true
        sleep 0.8
        echo '' > "$USB/f_mass_storage/lun0/file" 2>/dev/null || true
        sleep 0.3
    fi
    stop_candidate
    release_lock
    rm -f "$SUPERVISOR" "$STATE/rollback.request"
    if [ "$_recover" = 1 ]; then
        rm -f "$BASE/favorites_mode.flag"
        _previous=$(cat "$STATE/previous_vod" 2>/dev/null || true)
        case "$_previous" in
            ''|*[!a-zA-Z0-9_-]*) _previous='' ;;
        esac
        if [ -n "$_previous" ]; then
            echo '[favorites] restoring previous VOD'
            "$BB" sh "$SWITCH" vod "$_previous" && _restored=1 || _restored=0
        else
            echo '[favorites] restoring installed Single Live'
            "$BB" sh "$SWITCH" live && _restored=1 || _restored=0
        fi
        if [ "$_restored" = 1 ]; then
            echo restored > "$STATE/result"
        else
            echo rollback_failed > "$STATE/result"
            echo '[favorites] ERROR: installed Live recovery failed; inspect USB and logs' >&2
            return 1
        fi
    else
        echo stopped_before_activation > "$STATE/result"
    fi
}

case "$ACTION" in
    tunnel-start)
        PORT=${3:-18189}
        case "$PORT" in ''|*[!0-9]*) die 'invalid tunnel port' ;; esac
        [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die 'invalid tunnel port'
        mkdir -p "$STATE"
        _old=$(cat "$TUNNEL_PID" 2>/dev/null || true)
        same_process "$_old" "$TUNNEL" && die 'dedicated tunnel is already running'
        _hex=$(printf '%04X' "$PORT")
        if "$BB" awk -v endpoint="0100007F:$_hex" '$2 == endpoint && $4 == "0A" { found=1 } END { exit !found }' /proc/net/tcp; then
            die 'loopback media port is already listening; inspect before starting another tunnel'
        fi
        CHISEL=/system/xbin/chisel
        [ -x "$CHISEL" ] || CHISEL=$BASE/chisel
        [ -x "$CHISEL" ] || die 'installed Chisel missing'
        # A second process named chisel makes the existing watchdog kill both
        # clients. A copied executable gives this client its own comm name.
        cp "$CHISEL" "$TUNNEL"
        chmod 755 "$TUNNEL"
        [ -s "$PKG/chisel.auth" ] || die 'private chisel.auth file missing'
        chmod 600 "$PKG/chisel.auth"
        (
            AUTH=$(cat "$PKG/chisel.auth")
            [ -n "$AUTH" ] || exit 1
            export AUTH
            exec "$BB" setsid "$TUNNEL" client --max-retry-interval 15s \
                http://tv.smre.run.place/chisel \
                "127.0.0.1:$PORT:127.0.0.1:$PORT"
        ) > "$STATE/tunnel.log" 2>&1 &
        echo $! > "$TUNNEL_PID"
        for _i in $("$BB" seq 1 12); do
            sleep 1
            if ! same_process "$(cat "$TUNNEL_PID")" "$TUNNEL"; then
                stop_tunnel
                die 'dedicated tunnel exited; inspect its private log'
            fi
            if check_tunnel; then
                echo "[favorites] dedicated loopback tunnel tested on port $PORT; watchdog Chisel preserved"
                exit 0
            fi
        done
        stop_tunnel
        die 'manager probe failed; no USB changes performed' ;;
    tunnel-check)
        PORT=${3:-18189}
        case "$PORT" in ''|*[!0-9]*) die 'invalid tunnel port' ;; esac
        [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die 'invalid tunnel port'
        check_tunnel || die 'manager probe failed'
        echo "[favorites] manager reachable through loopback port $PORT"
        exit 0 ;;
    tunnel-stop)
        [ "$(cat "$USB/f_mass_storage/lun0/file" 2>/dev/null || true)" != "$BACKING" ] || die 'rollback the active candidate before stopping its media tunnel'
        stop_tunnel
        exit 0 ;;
    status)
        echo "package=$PKG"
        echo "result=$(cat "$STATE/result" 2>/dev/null || true)"
        echo "supervisor=$(cat "$SUPERVISOR" 2>/dev/null || true)"
        echo "candidate=$(cat "$PIDFILE" 2>/dev/null || true)"
        echo "tunnel=$(cat "$TUNNEL_PID" 2>/dev/null || true)"
        echo "usb=$(cat "$USB/state" 2>/dev/null || true)"
        echo "lun=$(cat "$USB/f_mass_storage/lun0/file" 2>/dev/null || true)"
        exit 0 ;;
    rollback)
        _sup=$(cat "$SUPERVISOR" 2>/dev/null || true)
        if same_process "$_sup" "$0"; then
            touch "$STATE/rollback.request"
            for _i in $("$BB" seq 1 45); do
                kill -0 "$_sup" 2>/dev/null || break
                sleep 1
            done
            kill -0 "$_sup" 2>/dev/null && die 'supervisor did not finish rollback; inspect logs'
            [ "$(cat "$STATE/result" 2>/dev/null || true)" != rollback_failed ] || exit 1
            exit 0
        fi
        LOCK_OWNER=$_sup
        USB_CHANGED=0
        rollback
        exit $? ;;
    start) ;;
    *) die 'usage: start PACKAGE_DIR 127.0.0.1:PORT SESSION | rollback/status PACKAGE_DIR | tunnel-start/tunnel-check PACKAGE_DIR PORT | tunnel-stop PACKAGE_DIR' ;;
esac

MEDIA=${3:-}
SESSION=${4:-}
case "$MEDIA" in 127.0.0.1:[0-9]*) ;; *) die 'media must use a pretested private loopback tunnel' ;; esac
PORT=${MEDIA#127.0.0.1:}
case "$PORT" in ''|*[!0-9]*) die 'invalid media port' ;; esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die 'invalid media port'
case "$SESSION" in ''|*[!a-zA-Z0-9_.-]*) die 'invalid mount session' ;; esac
[ ${#SESSION} -le 128 ] || die 'mount session too long'
[ -x "$SWITCH" ] || die 'installed rollback switch missing'
[ -x "$BB" ] || die 'BusyBox missing'
_worker=$(cat "$BASE/tvwd/cur.pid" 2>/dev/null || true)
case "$_worker" in
    ''|*[!0-9]*) ;;
    *) kill -0 "$_worker" 2>/dev/null && die 'watchdog command worker is still running' ;;
esac
for _file in fuse_ntfs sparse_unpack favorites_template.sparse.gz favorites_package.json favorites_layout.json favorites_manifest.json transport.token; do
    [ -s "$PKG/$_file" ] || die "missing $_file"
done
[ ! -L "$PKG" ] || die 'package must not be a symlink'
mkdir -p "$STATE" "$MNT"
_sup=$(cat "$SUPERVISOR" 2>/dev/null || true)
same_process "$_sup" "$0" && die 'this package already has a supervisor'
chmod 700 "$PKG" "$STATE"
chmod 600 "$PKG/transport.token"
chmod 755 "$PKG/fuse_ntfs" "$PKG/sparse_unpack"

# The staged binary has a distinct comm name, so legacy killall fuse_ntfs
# cannot kill it while the old driver is being removed.
cp "$PKG/fuse_ntfs" "$BIN"
chmod 755 "$BIN"
IMAGE=$PKG/template.$SESSION.bin
[ ! -e "$IMAGE" ] || die 'session image exists; use a fresh session identity'
"$BB" gzip -t "$PKG/favorites_template.sparse.gz" || die 'archive gzip integrity failure'
"$BB" gunzip -c "$PKG/favorites_template.sparse.gz" | "$PKG/sparse_unpack" "$IMAGE"
[ "$("$BB" stat -c %s "$IMAGE")" = 137438953472 ] || die 'wrong image logical size'
_used=$("$BB" du -k "$IMAGE" | "$BB" awk '{print $1}')
[ "$_used" -le 262144 ] || die 'sparse unpack exceeded 256 MiB physical limit'

# A live lock is never removed. Stale lock cleanup requires proving its owner
# exited, matching the installed mode-switch semantics.
if ! mkdir "$LOCK" 2>/dev/null; then
    _owner=$(cat "$LOCK/pid" 2>/dev/null || true)
    case "$_owner" in ''|*[!0-9]*) die 'mode lock has no valid owner; inspect manually' ;; esac
    kill -0 "$_owner" 2>/dev/null && die 'another mode switch/test owns the lock'
    rm -rf "$LOCK"
    mkdir "$LOCK" || die 'cannot acquire mode lock'
fi
echo $$ > "$LOCK/pid"
echo $$ > "$SUPERVISOR"
echo preparing > "$STATE/result"
_previous=$("$BB" head -n 1 "$BASE/vod_mode.flag" 2>/dev/null | "$BB" tr -d '\r' || true)
case "$_previous" in ''|*[!a-zA-Z0-9_-]*) _previous='' ;; esac
printf '%s\n' "$_previous" > "$STATE/previous_vod"
rm -f "$STATE/rollback.request"
USB_CHANGED=0
trap '' HUP
trap rollback EXIT
trap 'exit 143' INT TERM

# Candidate validates package hashes, canonical manifest, VBR/MFT/runlists and
# transport configuration before mounting. Existing USB remains untouched.
"$BB" setsid "$BIN" "$MNT" test "$IMAGE" --no-engine \
    "--favorites-layout=$PKG/favorites_layout.json" \
    "--favorites-package=$PKG/favorites_package.json" \
    "--favorites-manifest=$PKG/favorites_manifest.json" \
    "--favorites-template-archive=$PKG/favorites_template.sparse.gz" \
    "--favorites-template-image=$IMAGE" \
    "--favorites-session=$SESSION" \
    "--favorites-media=$MEDIA" \
    "--favorites-token-file=$PKG/transport.token" > "$LOG" 2>&1 &
echo $! > "$PIDFILE"
for _i in $("$BB" seq 1 12); do
    [ -f "$BACKING" ] && break
    same_process "$(cat "$PIDFILE")" "$BIN" || die 'candidate exited before mount; inspect fuse.log'
    sleep 1
done
[ -f "$BACKING" ] || die 'candidate mount not ready'
same_process "$(cat "$PIDFILE")" "$BIN" || die 'candidate died after mount'

USB_CHANGED=1
echo 0 > "$USB/enable"
sleep 0.8
echo '' > "$USB/f_mass_storage/lun0/file"
sleep 0.3
killall -9 fuse_ntfs stream_fetcher fuse_direct fuse_direct_arm32 2>/dev/null || true
"$BB" umount -l "$BASE/ntfs_lab/mnt" 2>/dev/null || true
"$BB" umount -l "$BASE/vfat_mnt" 2>/dev/null || true
rm -f "$BASE/vod_mode.flag" "$BASE/resync_usb"
echo "$SESSION" > "$BASE/favorites_mode.flag"

# Per-channel NTFS names are immutable package paths. Never run the legacy
# single-file patch_trp on this image. Rust already randomizes VSN per mount.
_current=$(cat "$USB/f_mass_storage/inquiry_string" 2>/dev/null || true)
if [ "$_current" = LIVETV1 ]; then _next=LIVETV2; else _next=LIVETV1; fi
echo "$_next" > "$USB/f_mass_storage/inquiry_string"
sleep 0.2
echo 1 > "$USB/f_mass_storage/lun0/ro"
sleep 0.2
echo "$BACKING" > "$USB/f_mass_storage/lun0/file"
sleep 0.3
echo 1 > "$USB/enable"
echo active > "$STATE/result"
echo "[favorites] active session=$SESSION; installed binaries preserved; rollback supervised"

# Keep the mode lock owned by a live process: watchdog still maintains ADB,
# Chisel and telemetry, while nocmd=1 prevents queued Web switches.
while [ ! -f "$STATE/rollback.request" ]; do
    same_process "$(cat "$PIDFILE")" "$BIN" || die 'candidate exited; restoring installed Live'
    sleep 1
done
exit 0
