#!/system/xbin/busybox sh
# Supervised, reversible tablet-only activation. Does not replace system binaries.
# Run via: busybox setsid busybox sh THIS start PACKAGE_DIR 127.0.0.1:PORT SESSION
# Return to installed Single Live via: busybox sh THIS rollback PACKAGE_DIR
set -eu
umask 077

BB=${BB:-/system/xbin/busybox}
BASE=${BASE:-/data/local/tmp}
USB=${USB:-/sys/class/android_usb/android0}
LOCK=$BASE/mode_switch.lock
SWITCH=/system/xbin/switch_tv_mode.sh
[ -x "$SWITCH" ] || SWITCH=$BASE/switch_tv_mode.sh
ACTION=${1:-}
PKG=${2:-}

die() { echo "[favorites] $*" >&2; exit 1; }
case "$PKG" in
    "$BASE"/live-favorites/*|/data/local/tmp/live-favorites/*) ;;
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
LEASEFILE=$BASE/mixed_handoff.lease

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

# ------------------------------------------------------------- handoff lease
# $BASE/mixed_handoff.lease, mode 600, holds exactly one line
# "<token> <owner_pid> <expires_epoch>". Every reader treats a malformed or
# expired lease as absent, so a crashed publisher can never wedge the tablet.
lease_read() { # echoes "<token> <owner_pid> <expires>" for a valid live lease
    [ -f "$LEASEFILE" ] || return 1
    _lease_line=$("$BB" head -n 1 "$LEASEFILE" 2>/dev/null || true)
    set -- $_lease_line
    [ "$#" -eq 3 ] || return 1
    case "$1" in ''|*[!0-9a-f]*) return 1 ;; esac
    [ ${#1} -eq 32 ] || return 1
    case "$2" in ''|*[!0-9]*) return 1 ;; esac
    case "$3" in ''|*[!0-9]*) return 1 ;; esac
    _lease_now=$("$BB" date +%s 2>/dev/null || true)
    case "$_lease_now" in ''|*[!0-9]*) return 1 ;; esac
    [ "$3" -gt "$_lease_now" ] || return 1
    printf '%s %s %s\n' "$1" "$2" "$3"
}

lease_token_raw() { # echoes the first field even if malformed or expired
    [ -f "$LEASEFILE" ] || return 1
    _lease_line=$("$BB" head -n 1 "$LEASEFILE" 2>/dev/null || true)
    set -- $_lease_line
    [ "$#" -ge 1 ] || return 1
    printf '%s\n' "$1"
}

lease_lock_held_by_other() { # 0 when another live process owns the mode lock
    [ -d "$LOCK" ] || return 1
    _lease_owner=$(cat "$LOCK/pid" 2>/dev/null || true)
    case "$_lease_owner" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$_lease_owner" 2>/dev/null || return 1
    [ "$_lease_owner" = "$$" ] && return 1
    return 0
}

# --------------------------------------------- atomic lease critical section
# Two publishers racing must not both believe they own the handoff lease, so
# acquisition runs under a kernel lock on a private file: the winner re-reads
# the lease and only then writes.
LEASELOCK=$LEASEFILE.lock

# --------------------------------------------------------- process identity
# A pid alone is reused by the kernel. Its start time (field 22 of
# /proc/<pid>/stat, in clock ticks since boot) makes one incarnation
# distinguishable from the next, which is what a handoff marker must bind to:
# otherwise a recycled pid could accept a stale marker.
# A takeover refused BEFORE any signal is a known state: nothing changed. The
# printed marker is the contract (exit status is masked by adb/su), so a
# refusal must say so instead of relying on a non-zero exit.
takeover_refuse() { # <reason-code> <human message>
    echo "[favorites] $2" >&2
    echo "takeover released=0 reason=$1"
    exit 0
}

proc_incarnation() { # <pid> -> "<pid>-<starttime>", or empty
    _pi_pid=${1:-}
    case "$_pi_pid" in ''|*[!0-9]*) return 1 ;; esac
    _pi_stat=$("$BB" cat "/proc/$_pi_pid/stat" 2>/dev/null || true)
    [ -n "$_pi_stat" ] || return 1
    # comm may contain spaces/parentheses; fields after the last ')' are stable.
    _pi_rest=${_pi_stat##*\) }
    set -- $_pi_rest
    # state is $1, so starttime (field 22) is $20 here.
    _pi_start=${20:-}
    case "$_pi_start" in ''|*[!0-9]*) return 1 ;; esac
    printf '%s-%s\n' "$_pi_pid" "$_pi_start"
}

# ------------------------------------------------------------------ identity
# A full identity, not a fragment. Every component has to be present, because a
# comparison against "unknown" would authorise whatever happens to be running.
#
# BOOT: /proc/sys/kernel/random/boot_id is stable for the whole boot. Android
# kernels often lack it, in which case btime (seconds since epoch at boot, from
# /proc/stat) is used - ALSO stable. Uptime is deliberately never used: it grows
# on every call, so two reads of the SAME boot would look like two different
# boots and no comparison could ever succeed.
boot_identity() { # echoes a stable per-boot marker, or none
    _bi=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)
    if [ -z "$_bi" ]; then
        _bi=$("$BB" sed -n 's/^btime //p' /proc/stat 2>/dev/null | "$BB" head -n 1)
    fi
    case "$_bi" in
        ''|*[!0-9a-fA-F:-]*) printf 'none\n' ;;
        *) printf '%s\n' "$_bi" ;;
    esac
}

instance_of() { # <pkgdir> -> the instance id written when its supervisor started
    _in=$(cat "$1/activation/instance.id" 2>/dev/null || true)
    case "$_in" in ''|*[!0-9a-f]*) printf 'none\n' ;; *) printf '%s\n' "$_in" ;; esac
}

owner_pkg_dir() { # <pid> -> the package dir that process is supervising, or empty
    # DISCOVERY, not authorisation. The path is used to find which package the
    # live lock owner belongs to; the identity computed from that package is
    # what has to match. Without this, an identity observed in a recovered
    # folder could never be recomputed, and the comparison would fall back to a
    # recorded name - a location.
    _od_pid=${1:-}
    case "$_od_pid" in ''|*[!0-9]*) return 1 ;; esac
    [ -r "/proc/$_od_pid/cmdline" ] || return 1
    _od_cmd=$("$BB" tr '\000' '\n' < "/proc/$_od_pid/cmdline" 2>/dev/null || true)
    for _od_tok in $_od_cmd; do
        case "$_od_tok" in
            "$BASE"/live-favorites/*/activate_live_favorites_tablet.sh)
                printf '%s\n' "${_od_tok%/activate_live_favorites_tablet.sh}"
                return 0 ;;
        esac
    done
    return 1
}

identity_of() { # <pkgdir> -> boot|instance|incarnation|manifest_id|package_digest
    _io_pkg=$1
    _io_boot=$(boot_identity)
    _io_inst=$(instance_of "$_io_pkg")
    _io_sup=$(cat "$_io_pkg/activation/supervisor.pid" 2>/dev/null || true)
    _io_inc=$(proc_incarnation "$_io_sup" 2>/dev/null || true)
    [ -n "$_io_inc" ] || _io_inc=none
    _io_mid=none
    if [ -s "$_io_pkg/favorites_manifest.json" ]; then
        _io_mid=$("$BB" sed -n 's/.*"manifest_id"[[:space:]]*:[[:space:]]*"\([0-9a-f]\{1,\}\)".*/\1/p' \
            "$_io_pkg/favorites_manifest.json" | "$BB" head -n 1)
    fi
    [ -n "$_io_mid" ] || _io_mid=none
    _io_digest=none
    if [ -s "$_io_pkg/SHA256SUMS" ]; then
        _io_digest=$("$BB" sha256sum "$_io_pkg/SHA256SUMS" 2>/dev/null | "$BB" cut -c1-32)
    fi
    [ -n "$_io_digest" ] || _io_digest=none
    printf '%s|%s|%s|%s|%s\n' "$_io_boot" "$_io_inst" "$_io_inc" "$_io_mid" "$_io_digest"
}

identity_is_complete() { # <token>; 0 only when all five components are real
    _ic_tok=${1:-}
    case "$_ic_tok" in ''|*none*) return 1 ;; esac
    case "$_ic_tok" in *'|'*'|'*'|'*'|'*) ;; *) return 1 ;; esac
    set -- $("$BB" printf '%s' "$_ic_tok" | "$BB" tr '|' ' ')
    [ "$#" -eq 5 ] || return 1
    for _ic_field in "$@"; do
        [ -n "$_ic_field" ] || return 1
    done
    return 0
}

# Kernel-managed exclusion. A killed or crashed holder has its lock released by
# the kernel itself, so there is no stale-lock reclaim path, no rm -rf of a lock
# another contender may have just created, and no window in which two
# acquisition attempts can both believe they won.
#
# It is fail-closed: if the applet cannot be used the lease is refused, because
# proceeding without mutual exclusion is what makes two publishers swap the
# same LUN.
# BusyBox ``flock`` on this firmware only accepts the ``FILE -c CMD`` form (the
# inherited-fd form reports "Bad file descriptor"), so the critical section is a
# re-entrant call to this same script rather than a lock held across statements.
# The kernel releases the lock when the holder dies, so a crashed publisher can
# never wedge the tablet and no stale-lock reclaim path is needed.
# BusyBox ``flock`` on this firmware supports ONLY ``[-sxun] FILE -c CMD``: it
# has no ``-w`` and it returns 1 both when the lock is busy and when the inner
# command failed. Exit status alone is therefore ambiguous, so the inner action
# prints an explicit marker and the wrapper retries only when it saw nothing.
lease_cs_answer() { # <ok|refused> <reason>
    if [ "$1" = ok ]; then
        echo 'lease-cs ok'
    else
        echo "lease-cs refused reason=$2"
    fi
    exit 0
}

_lce_args() { # quote every remaining argument for the inner shell
    for _lce_a in "$@"; do
        "$BB" printf " '%s'" "$_lce_a"
    done
}

lease_cs_exec() { # <internal action> [args...]; echoes the inner marker
    _lce_action=$1
    shift
    _lce_cmd=$("$BB" printf '%s sh %s %s%s' "$BB" "$0" "$_lce_action" "$(_lce_args "$@")")
    _lce_tries=0
    while [ "$_lce_tries" -lt 50 ]; do
        _lce_out=$("$BB" flock -n "$LEASELOCK" -c "$_lce_cmd" 2>&1)
        case "$_lce_out" in
            *"lease-cs refused reason="*)
                printf '%s\n' "$_lce_out"
                return 0 ;;
            *'lease-cs ok'*)
                printf '%s\n' "$_lce_out"
                return 0 ;;
        esac
        # No marker at all: the lock was busy (flock never ran the action).
        "$BB" sleep 0.1 2>/dev/null || sleep 1
        _lce_tries=$((_lce_tries + 1))
    done
    return 1
}

lease_write() { # <token> <owner_pid> <expires>; atomic replace, mode 600
    _lw_tmp=$LEASEFILE.tmp.$$
    printf '%s %s %s\n' "$1" "$2" "$3" > "$_lw_tmp" || return 1
    # This firmware ships toolbox `mv`, which rejects "-f"; the busybox
    # applet is the one that performs the atomic rename correctly.
    "$BB" chmod 600 "$_lw_tmp" 2>/dev/null || true
    "$BB" mv -f "$_lw_tmp" "$LEASEFILE" || return 1
    return 0
}

lease_now() { # echoes the current epoch seconds, or fails
    _ln_now=$("$BB" date +%s 2>/dev/null || true)
    case "$_ln_now" in ''|*[!0-9]*) return 1 ;; esac
    printf '%s\n' "$_ln_now"
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

check_tunnel_live() {
    _p=${1:-${PORT:-18190}}
    _reply=$(printf 'PROBE\n' | "$BB" nc -w 2 127.0.0.1 "$_p" 2>/dev/null || true)
    [ "$_reply" = 'ERR source unavailable' ]
}

check_tunnel_vod() {
    _p=${1:-${VOD_PORT:-18191}}
    _tok=$("$BB" tr -d '\r\n' < "$PKG/transport.token" 2>/dev/null || true)
    [ -n "$_tok" ] || return 1
    _reply=$(printf "GET /probe HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nAuthorization: Bearer %s\r\nConnection: close\r\n\r\n" "$_p" "$_tok" | "$BB" nc -w 2 127.0.0.1 "$_p" 2>/dev/null || true)
    case "$_reply" in
        *"200 OK"*|*"OK mixed-vod-proxy"*) return 0 ;;
        *) return 1 ;;
    esac
}

check_tunnel() {
    check_tunnel_live "${1:-${PORT:-18190}}"
}

vod_first_task() { # echoes "<task_id> <size>" of the first allowlisted task
    [ -s "$PKG/vod_allowlist.json" ] || return 1
    _tid=$("$BB" sed -n 's/.*"task_id"[[:space:]]*:[[:space:]]*"\([0-9a-f]\{8\}\)".*/\1/p' "$PKG/vod_allowlist.json" | "$BB" head -n 1)
    _size=$("$BB" sed -n 's/.*"size"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' "$PKG/vod_allowlist.json" | "$BB" head -n 1)
    [ -n "$_tid" ] && [ -n "$_size" ] || return 1
    printf '%s %s\n' "$_tid" "$_size"
}

vod_first_revision() {
    "$BB" sed -n 's/.*"revision"[[:space:]]*:[[:space:]]*"\([a-z0-9:]\{1,\}\)".*/\1/p' "$PKG/vod_allowlist.json" | "$BB" head -n 1
}

# Real end-to-end byte proof: tablet -> loopback tunnel -> proxy -> server.
# Verifies HTTP status, exact range, declared total size and real media bytes
# (the MP4 'ftyp' box must be present in the returned body). A proxy that only
# listens, or an upstream that cannot serve the selected task, always fails.
check_vod_range() {
    _p=${1:-${VOD_PORT:-18191}}
    [ -s "$PKG/vod_allowlist.json" ] || return 1
    _tok=$("$BB" tr -d '\r\n' < "$PKG/transport.token" 2>/dev/null || true)
    _rev=$(vod_first_revision)
    _pair=$(vod_first_task) || return 1
    _task=${_pair%% *}
    _size=${_pair##* }
    [ -n "$_tok" ] && [ -n "$_rev" ] && [ -n "$_task" ] && [ -n "$_size" ] || return 1
    case "$_size" in ''|*[!0-9]*) return 1 ;; esac
    _end=63
    [ "$_size" -ge 64 ] || _end=$((_size - 1))
    [ "$_end" -ge 0 ] || return 1
    _length=$((_end + 1))
    mkdir -p "$STATE"
    _out=$STATE/preflight.vod
    printf 'GET /vod/%s HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nAuthorization: Bearer %s\r\nX-Vod-Revision: %s\r\nRange: bytes=0-%s\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n' \
        "$_task" "$_p" "$_tok" "$_rev" "$_end" | "$BB" nc -w 8 127.0.0.1 "$_p" > "$_out" 2>/dev/null || true
    "$BB" grep -a -q ' 206 ' "$_out" || return 1
    "$BB" grep -a -q "Content-Length: $_length" "$_out" || return 1
    "$BB" grep -a -q "Content-Range: bytes 0-$_end/$_size" "$_out" || return 1
    "$BB" grep -a -q 'ftyp' "$_out" || return 1
    return 0
}

# Mandatory integrity and media preflight, shared by the read-only probe action
# and by the activation path, so both can never drift apart.
run_preflights() {
    [ -s "$PKG/SHA256SUMS" ] || die 'SHA256SUMS missing or empty; refusing to touch the USB'
    ( cd "$PKG" && "$BB" sha256sum -c SHA256SUMS ) || die 'package SHA256 verification failed'
    check_tunnel_live "$PORT" || die "media live tunnel on port $PORT not reachable (preflight failed); aborting before USB switch"
    if [ -n "$VOD_FLAG" ]; then
        _pf_port=${VOD_MEDIA#127.0.0.1:}
        [ -n "$_pf_port" ] || _pf_port=18191
        check_tunnel_vod "$_pf_port" || die "mixed vod proxy tunnel on port $_pf_port not reachable or auth failed (preflight failed); aborting before USB switch"
        check_vod_range "$_pf_port" || die "mixed vod range preflight failed on port $_pf_port (no real VOD bytes through the tunnel); aborting before USB switch"
    fi
}

rollback() {
    trap - EXIT INT TERM
    # Only a takeover that names THIS supervisor and still owns a live lease
    # may suppress legacy restoration. A lease alone is not sufficient: a
    # failed or unrelated publisher must not change normal rollback behavior.
    _handoff=0
    _handoff_request=$STATE/takeover.request
    _handoff_ack=$STATE/handoff.ack
    rm -f "$_handoff_ack"
    if [ -f "$_handoff_request" ]; then
        _handoff_line=$("$BB" head -n 1 "$_handoff_request" 2>/dev/null || true)
        _handoff_live=$(lease_read || true)
        if [ -n "$_handoff_line" ] && [ -n "$_handoff_live" ]; then
            set -- $_handoff_line
            _handoff_token=$1
            # Three independent proofs, all required: the marker names THIS
            # process incarnation (a reused pid cannot accept a stale marker),
            # this process still owns the mode lock, and a live lease carries
            # the same token (an unrelated or expired publisher cannot).
            if [ "$#" -eq 3 ] && [ "$3" = "$(proc_incarnation $$)" ] \
                && [ "$(cat "$LOCK/pid" 2>/dev/null || true)" = "$$" ]; then
                set -- $_handoff_live
                if [ "$1" = "$_handoff_token" ]; then
                    _handoff=1
                    # Durable acceptance: the publisher only believes a
                    # takeover that this supervisor acknowledged, so a silient
                    # (or legacy-restoring) supervisor can never look like a
                    # successful handoff.
                    printf '%s %s\n' "$_handoff_token" "$(proc_incarnation $$)" \
                        > "$_handoff_ack.tmp.$$" 2>/dev/null \
                        && "$BB" chmod 600 "$_handoff_ack.tmp.$$" 2>/dev/null \
                        && "$BB" mv -f "$_handoff_ack.tmp.$$" "$_handoff_ack" 2>/dev/null || true
                fi
            fi
        fi
    fi
    rm -f "$_handoff_request"
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
    # Release only the handoff lease this activation owns; a lease held by a
    # different generation is never erased here.
    _rb_tok=$(cat "$STATE/handoff.token" 2>/dev/null || true)
    if [ -n "$_rb_tok" ]; then
        _rb_lease=$(lease_token_raw 2>/dev/null || true)
        [ "$_rb_lease" = "$_rb_tok" ] && rm -f "$LEASEFILE"
    fi
    rm -f "$STATE/handoff.token"
    if [ -n "${IMAGE:-}" ] && [ -f "$IMAGE" ]; then
        rm -f "$IMAGE"
    fi
    # Only artifacts produced by this attempt are removed. The original package
    # binary, the LUN and any previous playback are left untouched.
    rm -f "$BIN" "$STATE/preflight.vod"
    # Remove only our own registry entry; a later session must never be erased.
    if [ "$(cat "$BASE/favorites_supervisor.path" 2>/dev/null || true)" = "$PKG" ]; then
        rm -f "$BASE/favorites_supervisor.path"
    fi
    rm -f "$SUPERVISOR" "$STATE/rollback.request"
    if [ "$_recover" = 1 ] && [ "$_handoff" = 1 ]; then
        # The incoming generation owns the handoff; do not race it by
        # starting the legacy switch and reacquiring mode_switch.lock.
        rm -f "$BASE/favorites_mode.flag"
        echo handoff_released > "$STATE/result"
    elif [ "$_recover" = 1 ]; then
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
        LIVE_PORT=${3:-18190}
        VOD_PORT=${4:-}
        if [ -z "$VOD_PORT" ] && [ -s "$PKG/vod_allowlist.json" ]; then
            VOD_PORT=18191
        fi
        case "$LIVE_PORT" in ''|*[!0-9]*) die 'invalid live tunnel port' ;; esac
        [ "$LIVE_PORT" -ge 1 ] && [ "$LIVE_PORT" -le 65535 ] || die 'invalid live tunnel port'
        if [ -n "$VOD_PORT" ]; then
            case "$VOD_PORT" in ''|*[!0-9]*) die 'invalid vod tunnel port' ;; esac
            [ "$VOD_PORT" -ge 1 ] && [ "$VOD_PORT" -le 65535 ] || die 'invalid vod tunnel port'
            [ "$VOD_PORT" != "$LIVE_PORT" ] || die 'live and vod tunnel ports must differ'
        fi
        mkdir -p "$STATE"
        _old=$(cat "$TUNNEL_PID" 2>/dev/null || true)
        same_process "$_old" "$TUNNEL" && die 'dedicated tunnel is already running'
        _hex_live=$(printf '%04X' "$LIVE_PORT")
        if "$BB" awk -v endpoint="0100007F:$_hex_live" '$2 == endpoint && $4 == "0A" { found=1 } END { exit !found }' /proc/net/tcp; then
            die "loopback live port $LIVE_PORT is already listening; inspect before starting another tunnel"
        fi
        if [ -n "$VOD_PORT" ]; then
            _hex_vod=$(printf '%04X' "$VOD_PORT")
            if "$BB" awk -v endpoint="0100007F:$_hex_vod" '$2 == endpoint && $4 == "0A" { found=1 } END { exit !found }' /proc/net/tcp; then
                die "loopback vod port $VOD_PORT is already listening; inspect before starting another tunnel"
            fi
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
            if [ -n "$VOD_PORT" ]; then
                exec "$BB" setsid "$TUNNEL" client --max-retry-interval 15s \
                    http://tv.smre.run.place/chisel \
                    "127.0.0.1:$LIVE_PORT:127.0.0.1:$LIVE_PORT" \
                    "127.0.0.1:$VOD_PORT:127.0.0.1:$VOD_PORT"
            else
                exec "$BB" setsid "$TUNNEL" client --max-retry-interval 15s \
                    http://tv.smre.run.place/chisel \
                    "127.0.0.1:$LIVE_PORT:127.0.0.1:$LIVE_PORT"
            fi
        ) > "$STATE/tunnel.log" 2>&1 &
        echo $! > "$TUNNEL_PID"
        for _i in $("$BB" seq 1 15); do
            "$BB" sleep 1
            if ! same_process "$(cat "$TUNNEL_PID")" "$TUNNEL"; then
                stop_tunnel
                die 'dedicated tunnel exited; inspect its private log'
            fi
            _l_ok=0
            _v_ok=0
            check_tunnel_live "$LIVE_PORT" && _l_ok=1 || _l_ok=0
            if [ -n "$VOD_PORT" ]; then
                check_tunnel_vod "$VOD_PORT" && _v_ok=1 || _v_ok=0
            else
                _v_ok=1
            fi
            if [ "$_l_ok" = 1 ] && [ "$_v_ok" = 1 ]; then
                if [ -n "$VOD_PORT" ]; then
                    echo "[favorites] dedicated dual loopback tunnels tested (live=$LIVE_PORT vod=$VOD_PORT); watchdog Chisel preserved"
                else
                    echo "[favorites] dedicated loopback tunnel tested on port $LIVE_PORT; watchdog Chisel preserved"
                fi
                exit 0
            fi
        done
        stop_tunnel
        if [ "$_l_ok" = 0 ]; then
            die "manager live tunnel probe failed on port $LIVE_PORT; no USB changes performed"
        else
            die "vod proxy tunnel probe failed on port $VOD_PORT; no USB changes performed"
        fi ;;
    tunnel-check)
        LIVE_PORT=${3:-18190}
        VOD_PORT=${4:-}
        if [ -z "$VOD_PORT" ] && [ -s "$PKG/vod_allowlist.json" ]; then
            VOD_PORT=18191
        fi
        case "$LIVE_PORT" in ''|*[!0-9]*) die 'invalid live tunnel port' ;; esac
        [ "$LIVE_PORT" -ge 1 ] && [ "$LIVE_PORT" -le 65535 ] || die 'invalid live tunnel port'
        check_tunnel_live "$LIVE_PORT" || die "manager live probe failed on port $LIVE_PORT"
        if [ -n "$VOD_PORT" ]; then
            case "$VOD_PORT" in ''|*[!0-9]*) die 'invalid vod tunnel port' ;; esac
            [ "$VOD_PORT" -ge 1 ] && [ "$VOD_PORT" -le 65535 ] || die 'invalid vod tunnel port'
            check_tunnel_vod "$VOD_PORT" || die "vod proxy probe failed on port $VOD_PORT"
            echo "[favorites] live ($LIVE_PORT) and vod ($VOD_PORT) reachable through loopback"
        else
            echo "[favorites] manager reachable through loopback port $LIVE_PORT"
        fi
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
                same_process "$_sup" "$0" || break
                sleep 1
            done
            same_process "$_sup" "$0" && die 'supervisor did not finish rollback; inspect logs'
            [ "$(cat "$STATE/result" 2>/dev/null || true)" != rollback_failed ] || exit 1
            exit 0
        fi
        LOCK_OWNER=$_sup
        USB_CHANGED=0
        rollback
        exit $? ;;
    start) ;;
    start-detached)
        # Re-exec ``start`` in its own session with its own log and return at
        # once. Detaching here (native shell, no nested quoting through
        # adb shell -> su -c) is what makes a silent no-start impossible: the
        # log file is created by this very shell before the child runs.
        _sd_media=${3:-}
        _sd_session=${4:-}
        _sd_vod=${5:-}
        _sd_token=${6:-}
        case "$_sd_media" in 127.0.0.1:[0-9]*) ;; *) die 'start-detached requires a loopback media address' ;; esac
        case "$_sd_session" in ''|*[!a-zA-Z0-9_.-]*) die 'start-detached requires a session name' ;; esac
        if [ -n "$_sd_vod" ]; then
            case "$_sd_vod" in 127.0.0.1:[0-9]*) ;; *) die 'invalid VOD media address' ;; esac
        fi
        mkdir -p "$STATE"
        "$BB" setsid "$BB" sh "$PKG/activate_live_favorites_tablet.sh" start "$PKG" \
            "$_sd_media" "$_sd_session" "$_sd_vod" "$_sd_token" \
            > "$STATE/start.log" 2>&1 < /dev/null &
        _sd_pid=$!
        # adb shell kills the shell's process group when the command returns, so
        # the child must survive on its own BEFORE this shell exits. Give it a
        # bounded grace period and, if it dies, report why instead of pretending
        # the session started.
        _sd_i=0
        while [ "$_sd_i" -lt 8 ]; do
            "$BB" sleep 0.5 2>/dev/null || sleep 1
            kill -0 "$_sd_pid" 2>/dev/null || break
            _sd_i=$((_sd_i + 1))
        done
        if ! kill -0 "$_sd_pid" 2>/dev/null; then
            echo "[favorites] detached start exited immediately; log tail:"
            "$BB" tail -n 20 "$STATE/start.log" 2>/dev/null || true
            die 'detached start exited immediately'
        fi
        echo "start-detached pid=$_sd_pid log=$STATE/start.log"
        exit 0 ;;
    preflight)
        LIVE_PORT=${3:-18190}
        VOD_PORT=${4:-18191}
        case "$LIVE_PORT" in ''|*[!0-9]*) die 'invalid live tunnel port' ;; esac
        case "$VOD_PORT" in ''|*[!0-9]*) die 'invalid vod tunnel port' ;; esac
        PORT=$LIVE_PORT
        VOD_MEDIA=127.0.0.1:$VOD_PORT
        if [ -s "$PKG/vod_allowlist.json" ]; then
            VOD_FLAG="--mixed-vod-address=$VOD_MEDIA"
        else
            VOD_FLAG=""
        fi
        trap 'rm -f "$STATE/preflight.vod"' EXIT INT TERM
        run_preflights
        rm -f "$STATE/preflight.vod"
        echo "[favorites] preflight ok (live=$LIVE_PORT vod=$VOD_PORT; real byte range verified through the tunnel)"
        exit 0 ;;
    verify-active)
        _expected_gen=${3:-}
        _expected_mid=${4:-}
        case "$_expected_gen" in ''|*[!0-9]*) die 'verify-active requires the expected generation' ;; esac
        case "$_expected_mid" in ''|*[!0-9a-f]*) die 'verify-active requires the expected manifest id' ;; esac
        [ -s "$PKG/favorites_manifest.json" ] || die 'favorites_manifest.json missing'
        _gen=$("$BB" sed -n 's/.*"generation"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' "$PKG/favorites_manifest.json" | "$BB" head -n 1)
        _mid=$("$BB" sed -n 's/.*"manifest_id"[[:space:]]*:[[:space:]]*"\([0-9a-f]\{1,\}\)".*/\1/p' "$PKG/favorites_manifest.json" | "$BB" head -n 1)
        _result=$(cat "$STATE/result" 2>/dev/null || true)
        _lun=$(cat "$USB/f_mass_storage/lun0/file" 2>/dev/null || true)
        _verified=0
        if [ "$_result" = active ] && [ "$_lun" = "$BACKING" ] \
            && same_process "$(cat "$PIDFILE" 2>/dev/null || true)" "$BIN" \
            && [ "$_gen" = "$_expected_gen" ] && [ "$_mid" = "$_expected_mid" ]; then
            _verified=1
        fi
        # Guard state: the mode lock must belong to this package's live
        # supervisor and the lease distinguishes our own handoff from a foreign
        # one (another publisher or a stale token).
        _lock_owned=0
        _lock_pid=$(cat "$LOCK/pid" 2>/dev/null || true)
        _sup_pid=$(cat "$SUPERVISOR" 2>/dev/null || true)
        case "$_lock_pid" in
            ''|*[!0-9]*) ;;
            *) if [ "$_lock_pid" = "$_sup_pid" ] && kill -0 "$_lock_pid" 2>/dev/null; then
                   _lock_owned=1
               fi ;;
        esac
        _lease_state=none
        _live_lease=$(lease_read || true)
        if [ -n "$_live_lease" ]; then
            set -- $_live_lease
            _ours_tok=$(cat "$STATE/handoff.token" 2>/dev/null || true)
            if [ -n "$_ours_tok" ] && [ "$_ours_tok" = "$1" ]; then
                _lease_state=ours
            else
                _lease_state=foreign
            fi
        fi
        echo "verify verified=$_verified generation=${_gen:-none} manifest_id=${_mid:-none} lock_owned=$_lock_owned lease=$_lease_state"
        [ "$_verified" = 1 ] && exit 0
        exit 1 ;;
    describe)
        # Identity of the RUNNING supervisor, reported so a caller can decide
        # without depending on the package path.
        #
        # Path is a location, not an identity: a generation recovered into an
        # alternative directory supervises the same content from somewhere else,
        # and correlating by path is exactly what refused publication 19. This
        # action makes the identity observable so the caller can compare an
        # incarnation instead of a directory name.
        #
        # Read-only: it never signals a process, never touches the USB and never
        # rewrites the mode lock. Every field is proven here or reported as
        # `none` — never invented.
        _d_proto=$("$BB" cat "$STATE/handoff.protocol" 2>/dev/null || true)
        [ -n "$_d_proto" ] || _d_proto=none
        # A STABLE boot marker. Never uptime: it grows on every call, so two
        # reads of the same boot would differ and no identity comparison could
        # ever hold.
        _d_boot=$(boot_identity)
        _d_instance=$(instance_of "$PKG")
        _d_gen=none
        _d_mid=none
        if [ -s "$PKG/favorites_manifest.json" ]; then
            _d_gen=$("$BB" sed -n 's/.*"generation"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' "$PKG/favorites_manifest.json" | "$BB" head -n 1)
            _d_mid=$("$BB" sed -n 's/.*"manifest_id"[[:space:]]*:[[:space:]]*"\([0-9a-f]\{1,\}\)".*/\1/p' "$PKG/favorites_manifest.json" | "$BB" head -n 1)
        fi
        [ -n "$_d_gen" ] || _d_gen=none
        [ -n "$_d_mid" ] || _d_mid=none
        _d_pkg_digest=none
        if [ -s "$PKG/SHA256SUMS" ]; then
            _d_pkg_digest=$("$BB" sha256sum "$PKG/SHA256SUMS" 2>/dev/null | "$BB" cut -c1-32)
            [ -n "$_d_pkg_digest" ] || _d_pkg_digest=none
        fi
        _d_rt_digest=none
        if [ -f "$BIN" ]; then
            _d_rt_digest=$("$BB" sha256sum "$BIN" 2>/dev/null | "$BB" cut -c1-32)
            [ -n "$_d_rt_digest" ] || _d_rt_digest=none
        fi
        _d_sup=$("$BB" cat "$SUPERVISOR" 2>/dev/null || true)
        _d_sup_inc=$(proc_incarnation "$_d_sup" || true)
        [ -n "$_d_sup_inc" ] || _d_sup_inc=none
        _d_cand=$("$BB" cat "$PIDFILE" 2>/dev/null || true)
        _d_cand_inc=$(proc_incarnation "$_d_cand" || true)
        [ -n "$_d_cand_inc" ] || _d_cand_inc=none
        _d_lun=$("$BB" cat "$USB/f_mass_storage/lun0/file" 2>/dev/null || true)
        [ -n "$_d_lun" ] || _d_lun=none
        _d_lock_owned=0
        _d_lock_pid=$("$BB" cat "$LOCK/pid" 2>/dev/null || true)
        case "$_d_lock_pid" in
            ''|*[!0-9]*) ;;
            *) if [ "$_d_lock_pid" = "$_d_sup" ] && kill -0 "$_d_lock_pid" 2>/dev/null; then
                   _d_lock_owned=1
               fi ;;
        esac
        echo "describe protocol=$_d_proto boot_id=$_d_boot generation=$_d_gen manifest_id=$_d_mid package_digest=$_d_pkg_digest runtime_digest=$_d_rt_digest supervisor=$_d_sup_inc candidate=$_d_cand_inc lun=$_d_lun lock_owned=$_d_lock_owned instance_id=$_d_instance identity=$(identity_of "$PKG") path=$PKG"
        exit 0 ;;
    handoff-lease)
        _hl_tok=${3:-}
        _hl_ttl=${4:-}
        _hl_prev=${5:-}
        _hl_prev_inc=${6:-}
        case "$_hl_tok" in ''|*[!0-9a-f]*) die 'handoff-lease requires a 32-hex token' ;; esac
        [ ${#_hl_tok} -eq 32 ] || die 'handoff-lease requires a 32-hex token'
        case "$_hl_ttl" in ''|*[!0-9]*) die 'handoff-lease requires a numeric TTL (seconds)' ;; esac
        [ "$_hl_ttl" -ge 15 ] && [ "$_hl_ttl" -le 120 ] || die 'handoff-lease TTL must be between 15 and 120 seconds'
        case "$_hl_prev" in ''|*[!a-zA-Z0-9_.-]*) _hl_prev='' ;; esac
        # The observed IDENTITY TOKEN: boot|instance|incarnation|manifest|digest.
        # It is not shape-sanitised here; ``lease-write`` refuses it unless all
        # five components are real, so a fragment can never authorise a lease.
        # (Blanking it by an old ``<pid>-<starttime>`` pattern would silently
        # turn a full identity into "nothing observed".)
        mkdir -p "$BASE"
        # The lease must name the CALLER's pid: the inner shell is a different
        # process, and the owner field is what a human inspects later.
        _hl_out=$(lease_cs_exec lease-write "$PKG" "$_hl_tok" "$_hl_ttl" "$_hl_prev" "$$" "$_hl_prev_inc") \
            || die 'handoff lease is being acquired by another publisher'
        case "$_hl_out" in
            *'lease-cs ok'*) echo "handoff-lease token=$_hl_tok expires_in=$_hl_ttl"; exit 0 ;;
            *'lease-cs refused reason='*) echo "$_hl_out" >&2; die 'handoff lease refused' ;;
        esac
        die 'handoff lease gave no answer' ;;
    lease-write) # MUST run under lease_cs_exec: mutual exclusion is the lock itself
        _lw_tok=${3:-}
        _lw_ttl=${4:-}
        _lw_prev=${5:-}
        _lw_owner_pid=${6:-}
        _lw_prev_inc=${7:-}
        case "$_lw_tok" in ''|*[!0-9a-f]*) lease_cs_answer refused invalid_token ;; esac
        [ ${#_lw_tok} -eq 32 ] || lease_cs_answer refused invalid_token
        _lw_now=$(lease_now) || lease_cs_answer refused no_clock
        if lease_lock_held_by_other; then
            _lw_owner=$(cat "$LOCK/pid" 2>/dev/null || true)
            _lw_ok=0
            # Identity first, and the WHOLE identity: boot + instance + the
            # incarnation of the live lock owner + the manifest and package
            # digests of the previous generation. A single component (a pid and
            # its start time) is not enough to authorise a handover, and a
            # directory name authorises nothing at all. An incomplete token is
            # refused rather than compared: "unknown" must never match.
            if identity_is_complete "$_lw_prev_inc" && [ -n "$_lw_owner" ]; then
                _lw_dir=$(owner_pkg_dir "$_lw_owner" 2>/dev/null || true)
                _lw_owner_inc=$(proc_incarnation "$_lw_owner" 2>/dev/null || true)
                if [ -n "$_lw_dir" ] && [ -n "$_lw_owner_inc" ]; then
                    _lw_live_id=$(identity_of "$_lw_dir")
                    _lw_live_inc=$("$BB" printf '%s' "$_lw_live_id" | "$BB" cut -d'|' -f3)
                    if [ "$_lw_live_id" = "$_lw_prev_inc" ] \
                            && [ "$_lw_owner_inc" = "$_lw_live_inc" ]; then
                        _lw_ok=1
                    fi
                fi
            fi
            [ "$_lw_ok" = 1 ] || lease_cs_answer refused previous_generation_owns_lock
        fi
        _lw_live=$(lease_read || true)
        if [ -n "$_lw_live" ]; then
            set -- $_lw_live
            [ "$1" = "$_lw_tok" ] || lease_cs_answer refused lease_held_by_other
        fi
        lease_write "$_lw_tok" "$_lw_owner_pid" "$((_lw_now + _lw_ttl))" \
            || lease_cs_answer refused lease_unwritable
        lease_cs_answer ok ;;
    handoff-renew)
        _hrn_tok=${3:-}
        _hrn_ttl=${4:-}
        case "$_hrn_tok" in ''|*[!0-9a-f]*) die 'handoff-renew requires a 32-hex token' ;; esac
        [ ${#_hrn_tok} -eq 32 ] || die 'handoff-renew requires a 32-hex token'
        case "$_hrn_ttl" in ''|*[!0-9]*) die 'handoff-renew requires a numeric TTL (seconds)' ;; esac
        [ "$_hrn_ttl" -ge 15 ] && [ "$_hrn_ttl" -le 120 ] || die 'handoff-renew TTL must be between 15 and 120 seconds'
        mkdir -p "$BASE"
        _hrn_out=$(lease_cs_exec lease-renew "$PKG" "$_hrn_tok" "$_hrn_ttl") \
            || die 'handoff lease is being renewed by another publisher'
        case "$_hrn_out" in
            *'lease-cs ok'*) echo "handoff-renew token=$_hrn_tok expires_in=$_hrn_ttl"; exit 0 ;;
            *'lease-cs refused reason='*) echo "$_hrn_out" >&2; die 'handoff renew refused' ;;
        esac
        die 'handoff renew gave no answer' ;;
    lease-renew)
        _lr_tok=${3:-}
        _lr_ttl=${4:-}
        _lr_live=$(lease_read || true)
        [ -n "$_lr_live" ] || lease_cs_answer refused lease_missing
        set -- $_lr_live
        [ "$1" = "$_lr_tok" ] || lease_cs_answer refused lease_not_ours
        _lr_owner=$2
        _lr_now=$(lease_now) || lease_cs_answer refused no_clock
        lease_write "$_lr_tok" "$_lr_owner" "$((_lr_now + _lr_ttl))" \
            || lease_cs_answer refused lease_unwritable
        lease_cs_answer ok ;;
    handoff-takeover)
        # Controlled N -> N+1 handover. Runs only under the caller's live lease
        # and only stops a supervisor whose own cmdline proves it belongs to the
        # named previous generation. TERM (never KILL) so that supervisor runs
        # its rollback trap and frees mode_switch.lock.
        _ht_tok=${3:-}
        _ht_prev=${4:-}
        # The instance the caller OBSERVED (describe) before asking for the
        # takeover. Optional for the legacy 5-argument form; when supplied it is
        # binding, so a command that was delayed, retried or replayed against a
        # different process lifetime is refused instead of signalled.
        _ht_expect_inc=${5:-}
        case "$_ht_tok" in ''|*[!0-9a-f]*) die 'handoff-takeover requires a 32-hex token' ;; esac
        [ ${#_ht_tok} -eq 32 ] || die 'handoff-takeover requires a 32-hex token'
        case "$_ht_prev" in ''|*[!a-zA-Z0-9_.-]*) die 'handoff-takeover requires the previous session' ;; esac
        # The expected identity is NOT sanitised by shape here. Its validity is
        # decided by ``identity_is_complete`` at the moment of the effect: five
        # real components or a refusal. Shape-matching it with the old
        # ``<pid>-<starttime>`` pattern would silently blank a full token and
        # turn a comparable identity into "nothing observed".
        _ht_lease=$(lease_read || true)
        [ -n "$_ht_lease" ] || takeover_refuse 'lease_missing' 'handoff lease missing or expired'
        set -- $_ht_lease
        [ "$1" = "$_ht_tok" ] || takeover_refuse 'lease_foreign' 'handoff lease is not ours'
        if [ ! -d "$LOCK" ]; then
            # There is nothing to take over: the previous supervisor is already
            # gone. That is a SUCCESS for a handoff (free to activate), not a
            # refusal - conflating it with "still running" would block a switch
            # that is actually safe.
            echo "takeover released=1 already_free=1 previous=$_ht_prev"
            exit 0
        fi
        _ht_owner=$(cat "$LOCK/pid" 2>/dev/null || true)
        case "$_ht_owner" in ''|*[!0-9]*) takeover_refuse 'lock_unowned' 'mode lock has no valid owner; inspect manually' ;; esac
        kill -0 "$_ht_owner" 2>/dev/null || takeover_refuse 'lock_orphan' "mode lock owner is gone but the lock remains"
        [ -r "/proc/$_ht_owner/cmdline" ] || takeover_refuse 'lock_unprovable' 'cannot prove the lock owner'
        # A SIGNAL requires the instance the caller observed. A directory name is
        # not an identity: refusing here is what stops a delayed or replayed
        # command from terminating whatever happens to own the lock by the time
        # it arrives. The caller proves the instance with `describe`; this is the
        # last moment before the signal, so the proof is rechecked below.
        identity_is_complete "$_ht_expect_inc" \
            || takeover_refuse 'lock_incarnation_unproven' 'refusing to signal a lock owner whose full identity was not observed'
        _ht_inc=$(proc_incarnation "$_ht_owner") \
            || takeover_refuse 'lock_incarnation_unreadable' 'cannot read the lock owner incarnation'
        # Revalidation under the guard, and BEFORE anything path-shaped is
        # even looked at: the caller proved an instance with `describe`, so the
        # process actually holding the lock must still BE that instance, with the
        # whole tuple (boot, instance, incarnation, digests) unchanged. A reboot,
        # a recycled pid, a replaced package or a competing publisher between
        # observation and action makes the tuple differ, and then nothing is
        # signalled. Identity first means the directory cannot authorise a
        # signal.
        _ht_dir=$(owner_pkg_dir "$_ht_owner" 2>/dev/null || true)
        [ -n "$_ht_dir" ] \
            || takeover_refuse 'lock_incarnation_unproven' 'cannot prove which package the lock owner supervises'
        _ht_live_id=$(identity_of "$_ht_dir")
        _ht_live_inc=$("$BB" printf '%s' "$_ht_live_id" | "$BB" cut -d'|' -f3)
        if [ "$_ht_inc" != "$_ht_live_inc" ] || [ "$_ht_live_id" != "$_ht_expect_inc" ]; then
            takeover_refuse 'lock_incarnation_changed' 'the lock owner is not the instance that was observed'
        fi
        # Tell the proven previous supervisor that this TERM is an intentional
        # handoff. Its rollback validates both this marker and the live lease;
        # an ordinary TERM or a stale marker continues to restore legacy VOD.
        _ht_state="$BASE/live-favorites/$_ht_prev/activation"
        [ -d "$_ht_state" ] || takeover_refuse 'no_previous_state' 'previous activation directory missing'
        _ht_marker="$_ht_state/takeover.request"
        _ht_tmp="$_ht_state/.takeover.request.$$"
        # token + pid + incarnation: the marker is only valid for the exact
        # process incarnation it was written for.
        printf '%s %s %s\n' "$_ht_tok" "$_ht_owner" "$_ht_inc" > "$_ht_tmp" \
            || takeover_refuse 'marker_unwritable' 'cannot write takeover marker'
        "$BB" chmod 600 "$_ht_tmp" 2>/dev/null || true
        "$BB" mv -f "$_ht_tmp" "$_ht_marker" || takeover_refuse 'marker_unpublishable' 'cannot publish takeover marker'
        rm -f "$_ht_state/handoff.ack"
        kill -TERM "$_ht_owner" 2>/dev/null || true
        # The previous supervisor's rollback detaches the LUN and removes a
        # 137 GiB sparse image before it exits, which on this tablet takes well
        # over 15 s. Waiting too little would report "still running" for a
        # handoff that actually succeeded, so the budget is generous (60 s) and
        # the caller's publication timeout is larger still.
        _ht_waited=0
        _ht_acked=0
        while [ "$_ht_waited" -lt 300 ]; do
            # A handoff is only proven by the supervisor's own acceptance
            # record. Disappearance of owner+lock is NOT enough: a rollback may
            # have released the lock while reinstating the legacy VOD mode,
            # which is exactly the state that must never look like success.
            if [ -f "$_ht_state/handoff.ack" ]; then
                _ht_ack=$("$BB" head -n 1 "$_ht_state/handoff.ack" 2>/dev/null || true)
                set -- $_ht_ack
                if [ "$#" -eq 2 ] && [ "$1" = "$_ht_tok" ]; then
                    _ht_acked=1
                    break
                fi
            fi
            sleep 0.2
            _ht_waited=$((_ht_waited + 1))
        done
        if [ "$_ht_acked" = 1 ]; then
            # Wait for the lock to be actually free before answering, so the
            # caller can start its own supervisor immediately.
            _ht_lockwait=0
            while [ "$_ht_lockwait" -lt 300 ]; do
                [ ! -d "$LOCK" ] && break
                sleep 0.2
                _ht_lockwait=$((_ht_lockwait + 1))
            done
            if [ -d "$LOCK" ]; then
                echo "takeover released=unknown owner=$_ht_owner previous=$_ht_prev reason=ack_without_lock"
                exit 0
            fi
            echo "takeover released=1 acked=1 owner=$_ht_owner previous=$_ht_prev"
            exit 0
        fi
        if ! kill -0 "$_ht_owner" 2>/dev/null; then
            # The old supervisor is gone but never accepted the handoff: it may
            # have restored the legacy mode. That is UNKNOWN, not success.
            echo "takeover released=unknown owner=$_ht_owner previous=$_ht_prev reason=no_ack"
            exit 0
        fi
        echo "takeover released=0 owner=$_ht_owner previous=$_ht_prev reason=still_running"
        exit 0 ;;
    handoff-release)
        _hr_tok=${3:-}
        _hr_released=0
        if [ -n "$_hr_tok" ] && [ -f "$LEASEFILE" ]; then
            _hr_line=$("$BB" head -n 1 "$LEASEFILE" 2>/dev/null || true)
            set -- $_hr_line
            if [ "$#" -ge 1 ] && [ "$1" = "$_hr_tok" ]; then
                rm -f "$LEASEFILE"
                _hr_released=1
            fi
        fi
        echo "handoff-release released=$_hr_released"
        exit 0 ;;
    handoff-status)
        _hs_tok=${3:-}
        _hs_present=0
        _hs_ours=0
        _hs_left=0
        _hs_lease=$(lease_read || true)
        if [ -n "$_hs_lease" ]; then
            _hs_present=1
            set -- $_hs_lease
            _hs_now=$("$BB" date +%s 2>/dev/null || true)
            case "$_hs_now" in ''|*[!0-9]*) _hs_now=0 ;; esac
            _hs_left=$(( $3 - _hs_now ))
            [ -n "$_hs_tok" ] && [ "$1" = "$_hs_tok" ] && _hs_ours=1
        fi
        echo "handoff present=$_hs_present ours=$_hs_ours expires_in=$_hs_left"
        exit 0 ;;
    *) die 'usage: start PACKAGE_DIR 127.0.0.1:PORT SESSION [127.0.0.1:VOD_PORT] [HANDOFF_TOKEN] | preflight PACKAGE_DIR [LIVE_PORT] [VOD_PORT] | verify-active PACKAGE_DIR GENERATION MANIFEST_ID | handoff-lease PACKAGE_DIR TOKEN TTL [PREVIOUS_SESSION] | handoff-renew PACKAGE_DIR TOKEN TTL | handoff-takeover PACKAGE_DIR TOKEN PREVIOUS_SESSION | handoff-release PACKAGE_DIR TOKEN | handoff-status PACKAGE_DIR [TOKEN] | rollback/status PACKAGE_DIR | tunnel-start/tunnel-check PACKAGE_DIR [LIVE_PORT] [VOD_PORT] | tunnel-stop PACKAGE_DIR | start-detached PACKAGE_DIR MEDIA SESSION [VOD_MEDIA] [HANDOFF_TOKEN]' ;;
esac

MEDIA=${3:-}
SESSION=${4:-}
VOD_MEDIA=${5:-}
HANDOFF_TOKEN=${6:-}
case "$MEDIA" in 127.0.0.1:[0-9]*) ;; *) die 'media must use a pretested private loopback tunnel' ;; esac
PORT=${MEDIA#127.0.0.1:}
case "$PORT" in ''|*[!0-9]*) die 'invalid media port' ;; esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die 'invalid media port'
case "$SESSION" in ''|*[!a-zA-Z0-9_.-]*) die 'invalid mount session' ;; esac
[ ${#SESSION} -le 128 ] || die 'mount session too long'
VOD_FLAG=""
if [ -n "$VOD_MEDIA" ]; then
    case "$VOD_MEDIA" in 127.0.0.1:[0-9]*) ;; *) die 'VOD media must use loopback address' ;; esac
    _vport=${VOD_MEDIA#127.0.0.1:}
    case "$_vport" in ''|*[!0-9]*) die 'invalid VOD media port' ;; esac
    [ "$_vport" -ge 1 ] && [ "$_vport" -le 65535 ] || die 'invalid VOD media port'
    VOD_FLAG="--mixed-vod-address=$VOD_MEDIA"
elif [ -s "$PKG/vod_allowlist.json" ]; then
    VOD_FLAG="--mixed-vod-address=127.0.0.1:18191"
fi
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
# The checksum manifest is enforced by run_preflights with its own explicit
# message (absent, empty or unverifiable must all abort before the USB switch).
[ ! -L "$PKG" ] || die 'package must not be a symlink'

# A handoff token (sixth positional, after the VOD media address) makes this
# start conditional on the publisher's live lease. The lease is proven BEFORE
# any artifact exists (no image, no binary copy, no lock), so a missing or
# expired lease can never disturb the previous LUN or playback.
if [ -n "$HANDOFF_TOKEN" ]; then
    case "$HANDOFF_TOKEN" in ''|*[!0-9a-f]*) die 'invalid handoff token' ;; esac
    [ ${#HANDOFF_TOKEN} -eq 32 ] || die 'invalid handoff token'
    _start_lease=$(lease_read || true)
    [ -n "$_start_lease" ] || die 'handoff lease missing or expired'
    set -- $_start_lease
    [ "$1" = "$HANDOFF_TOKEN" ] || die 'handoff lease missing or expired'
fi

mkdir -p "$STATE" "$MNT"
_sup=$(cat "$SUPERVISOR" 2>/dev/null || true)
same_process "$_sup" "$0" && die 'this package already has a supervisor'
chmod 700 "$PKG" "$STATE"
chmod 600 "$PKG/transport.token" "$PKG/chisel.auth" 2>/dev/null || true
chmod 755 "$PKG/fuse_ntfs" "$PKG/sparse_unpack"
chmod 644 "$PKG"/*.json "$PKG"/*.sparse.gz "$PKG"/SHA256SUMS 2>/dev/null || true

# Cleanup is armed BEFORE the first temporary artifact is created (image copy,
# expansion, size check, lock). It removes only what this attempt produced and
# never touches a LUN it does not own, so previous playback is preserved even
# when a preflight, an expansion or the lock acquisition fails.
USB_CHANGED=0
trap '' HUP
trap rollback EXIT
trap 'exit 143' INT TERM

# Record this attempt's handoff token (only after its lease was proven above)
# so verify-active and rollback can tell our lease from a foreign one.
[ -z "$HANDOFF_TOKEN" ] || printf '%s\n' "$HANDOFF_TOKEN" > "$STATE/handoff.token"

# The mode lock is acquired HERE: before the checksum/media preflights and
# before the image copy/expansion, so the whole switch happens inside it and
# no playback/mode command can start between the idleness proof and the LUN
# switch. A live lock is never removed. Stale lock cleanup requires proving its
# owner exited, matching the installed mode-switch semantics.
if ! mkdir "$LOCK" 2>/dev/null; then
    _owner=$(cat "$LOCK/pid" 2>/dev/null || true)
    case "$_owner" in ''|*[!0-9]*) die 'mode lock has no valid owner; inspect manually' ;; esac
    kill -0 "$_owner" 2>/dev/null && die 'another mode switch/test owns the lock'
    rm -rf "$LOCK"
    mkdir "$LOCK" || die 'cannot acquire mode lock'
fi
echo $$ > "$LOCK/pid"

# Mandatory integrity + real media preflight (checksums, both tunnels, and a
# byte-verified VOD range read through the tunnel) now runs under the lock and
# aborts before the USB switch.
run_preflights

# The staged binary has a distinct comm name, so legacy killall fuse_ntfs
# cannot kill it while the old driver is being removed.
cp "$PKG/fuse_ntfs" "$BIN"
chmod 755 "$BIN"
IMAGE=$PKG/template.$SESSION.bin
[ ! -e "$IMAGE" ] || die 'session image exists; use a fresh session identity'
"$BB" gzip -t "$PKG/favorites_template.sparse.gz" || die 'archive gzip integrity failure'
"$BB" gunzip -c "$PKG/favorites_template.sparse.gz" | "$PKG/sparse_unpack" "$IMAGE"
# The volume size is a property of the package, not a constant: a reviewed
# grade can be built on a larger volume (192 GiB) without changing this script.
# When the package declares its size, that value is authoritative; the historical
# 128 GiB stays the fallback for packages that predate the field.
_EXPECTED_IMAGE_BYTES=$(sed -n 's/.*"volume_bytes"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' \
    "$PKG/favorites_package.json" 2>/dev/null | head -n 1)
case "$_EXPECTED_IMAGE_BYTES" in
    ''|*[!0-9]*) _EXPECTED_IMAGE_BYTES=137438953472 ;;
esac
_IMAGE_BYTES=$("$BB" stat -c %s "$IMAGE")
[ "$_IMAGE_BYTES" = "$_EXPECTED_IMAGE_BYTES" ] \
    || die "wrong image logical size: got $_IMAGE_BYTES, package declares $_EXPECTED_IMAGE_BYTES"
_used=$("$BB" du -k "$IMAGE" | "$BB" awk '{print $1}')
[ "$_used" -le 262144 ] || die 'sparse unpack exceeded 256 MiB physical limit'

echo $$ > "$SUPERVISOR"
# Instance identity for THIS supervisor lifetime, written once here and only
# read afterwards (describe stays read-only). sha256 over the pid and the start
# moment gives a unique, stable value per start without a random source this
# firmware may not have.
printf '%s|%s' "$$" "$("$BB" date +%s 2>/dev/null || echo 0)" \
    | "$BB" sha256sum 2>/dev/null | "$BB" cut -c1-32 > "$STATE/instance.id" || true
echo preparing > "$STATE/result"
_previous=$("$BB" head -n 1 "$BASE/vod_mode.flag" 2>/dev/null | "$BB" tr -d '\r' || true)
case "$_previous" in ''|*[!a-zA-Z0-9_-]*) _previous='' ;; esac
printf '%s\n' "$_previous" > "$STATE/previous_vod"
rm -f "$STATE/rollback.request"

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
    "--favorites-token-file=$PKG/transport.token" \
    "--mixed-vod-allowlist=$PKG/vod_allowlist.json" \
    ${VOD_FLAG:+"$VOD_FLAG"} > "$LOG" 2>&1 &
echo $! > "$PIDFILE"
for _i in $("$BB" seq 1 12); do
    _candidate=$(cat "$PIDFILE")
    # setsid may still be starting the executable; require its identity before
    # switching USB, but do not mistake that short transition for an exit.
    if [ -f "$BACKING" ] && same_process "$_candidate" "$BIN"; then
        break
    fi
    kill -0 "$_candidate" 2>/dev/null || die 'candidate exited before mount; inspect fuse.log'
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
# The watchdog uses this only after verifying the live lock owner, supervisor
# process identity and LUN. It may then accept a mode-switch command and ask
# this supervisor to roll back before executing that command.
printf '%s\n' "$PKG" > "$BASE/favorites_supervisor.path.tmp"
"$BB" mv -f "$BASE/favorites_supervisor.path.tmp" "$BASE/favorites_supervisor.path"
echo "[favorites] active session=$SESSION; installed binaries preserved; rollback supervised"

# Announce, as the RUNNING process, that this supervisor understands the
# handoff protocol. A publisher must never infer that from the script file on
# disk: the file can be replaced while an older process is still supervising.
printf 'handoff.ack 1\n' > "$STATE/handoff.protocol" 2>/dev/null && \
    "$BB" chmod 600 "$STATE/handoff.protocol" 2>/dev/null || true

# Keep the lock until the candidate has stopped. The watchdog can explicitly
# request supervised rollback for a queued mode switch; no USB owners overlap.
while [ ! -f "$STATE/rollback.request" ]; do
    same_process "$(cat "$PIDFILE")" "$BIN" || die 'candidate exited; restoring installed Live'
    sleep 1
done
exit 0
