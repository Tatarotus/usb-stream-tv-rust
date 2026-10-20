#!/bin/sh
# TASK-003 host harness: watchdog observation + extracted resync decisions.
#
# Never touches /data: the script is imported in functions-only mode, all
# dependencies (busybox/setsid, log, cooldown, mode script) are mocked, and
# every path lives under a temporary directory.

HERE=$(cd "$(dirname "$0")" && pwd)
WD=$(cd "$HERE/.." && pwd)/scripts/tv_watchdog.sh
if [ ! -f "$WD" ]; then
    echo "FAIL - watchdog not found at $WD" >&2
    exit 1
fi

TMP=${TMPDIR:-/tmp}/tvwd-obs.$$
mkdir -p "$TMP/bin" "$TMP/base/tvwd" "$TMP/usb/f_mass_storage/lun0" || exit 1
trap 'rm -rf "$TMP"' EXIT INT TERM

TRACE=$TMP/trace.log
LOGF=$TMP/watchdog.log
: > "$TRACE"
: > "$LOGF"
export TRACE

# Boot-time window: every observation must carry a boot_ms inside it.
UPTIME_A=$(cat /proc/uptime | cut -d. -f1)
case "$UPTIME_A" in *[!0-9]*|"") UPTIME_A=0 ;; esac
UPTIME_LO=$((UPTIME_A > 5 ? (UPTIME_A - 5) * 1000 : 0))

PASS=0
FAILED=0
ok() { PASS=$((PASS + 1)); echo "ok - $1"; }
bad() { FAILED=$((FAILED + 1)); echo "FAIL - $1" >&2; }
eq() { if [ "$1" = "$2" ]; then ok "$3"; else bad "$3 (got '$1' want '$2')"; fi; }
has() { if grep -q -- "$2" "$1" 2>/dev/null; then ok "$3"; else bad "$3 (missing '$2')"; fi; }
hasnt() { if grep -q -- "$2" "$1" 2>/dev/null; then bad "$3 (unexpected '$2')"; else ok "$3"; fi; }
reset_obs() {
    OBS_LAST_MARKER=""
    OBS_LAST_DECISION=""
    OBS_LAST_GADGET=""
    GADGET_LAST=""
    GADGET_PENDING=""
    : > "$TRACE"
    [ -n "${LOGF:-}" ] && : > "$LOGF"
}

# ---------------------------------------------------------------- stubs
cat > "$TMP/bin/busybox" <<'STUB'
#!/bin/sh
echo "bb $*" >> "$TRACE"
case "$1" in
    setsid) shift; exec "$@" ;;
    sh) shift; exec /bin/sh "$@" ;;
    date) shift; exec /bin/date "$@" ;;
    *) exit 0 ;;
esac
STUB
chmod +x "$TMP/bin/busybox"

cat > "$TMP/switch_ok.sh" <<'SW'
#!/bin/sh
echo "switch $1" >> "$TRACE"
SW
chmod +x "$TMP/switch_ok.sh"

# ------------------------------------------------- functions-only import purity
# A successful import with an empty PATH proves no external command runs and,
# with no redirects, that no file is touched before the gate.
(
    PATH=/nonexistent
    USBTV_WATCHDOG_FUNCTIONS_ONLY=1
    . "$WD" || exit 1
    command -v resync_cycle >/dev/null 2>&1 || exit 1
    command -v obs_event >/dev/null 2>&1 || exit 1
    # Import purity: the executable probes must not have run yet, so the
    # defaults are intact (a probe would have rewritten SELF to $BASE/...).
    [ "$SELF" = /system/xbin/tv_watchdog.sh ] || exit 1
    [ "$CHISEL_BIN" = /system/xbin/chisel ] || exit 1
    exit 0
)
eq "$?" 0 "functions-only import runs no external command and defines helpers"

USBTV_WATCHDOG_FUNCTIONS_ONLY=1
export USBTV_WATCHDOG_FUNCTIONS_ONLY
. "$WD" || { echo "FAIL - could not source definitions" >&2; exit 1; }

# ------------------------------------------------------------ test workspace
# Re-point every path after sourcing (the script assigns its tablet defaults).
BASE=$TMP/base
STATE=$TMP/base/tvwd
USBD=$TMP/usb
BB=$TMP/bin/busybox
LOGF=$TMP/watchdog.log
OBS_LOG=$TMP/watchdog.log
CHISEL_LOG=$TMP/chisel.log
CHISEL_BIN=$TMP/no-chisel
TOKEN_FILE=$TMP/no-token
TOKEN=""
SW_OK=$TMP/switch_ok.sh
SW_MISSING=$TMP/nope.sh

# Hard guard: this harness must never write outside its temporary workspace.
case "$LOGF $OBS_LOG $BASE $STATE $USBD" in
    *"/data/"*|*"/data "*) echo "FAIL - harness would touch /data" >&2; exit 1 ;;
esac
case "$LOGF" in "$TMP"/*) ;; *) echo "FAIL - log outside temp dir" >&2; exit 1 ;; esac

# Deliberate mocks: log, cooldown and setsid (via $BB) are recorded.
log() { echo "log:$*" >> "$TRACE"; }
COOLDOWN=0
cooldown_ok() { echo "cooldown:$1:$2" >> "$TRACE"; return "$COOLDOWN"; }

# ------------------------------------------------------------ 1. no marker
reset_obs
rm -f "$BASE/resync_usb" "$BASE/vod_mode.flag" "$BASE/favorites_mode.flag"
resync_cycle "$SW_OK" ""
eq "$?" 1 "no marker: returns 'no decision'"
eq "$(wc -l < "$TRACE")" 0 "no marker: no side effects"
eq "$(grep -c 'USBTV_OBS' "$LOGF")" 0 "no marker: no observation line"

# ---------------------------------------------------- 2. live mode suppressed
reset_obs
: > "$BASE/resync_usb"
: > "$BASE/vod_mode.flag"
resync_cycle "$SW_OK" ""
eq "$?" 2 "vod mode: decision suppressed"
[ -f "$BASE/resync_usb" ] && ok "vod mode: marker preserved" || bad "vod mode: marker removed"
has "$LOGF" "event=resync_marker_observed" "vod mode: marker observed is logged"
has "$LOGF" "event=resync_decision .*reason=mode_suppressed" "vod mode: reason recorded"
hasnt "$TRACE" "bb setsid" "vod mode: no launch"
rm -f "$BASE/vod_mode.flag"

# ------------------------------------------------------ 3. missing switcher
reset_obs
: > "$BASE/resync_usb"
resync_cycle "$SW_MISSING" "$TMP/nope2.sh"
eq "$?" 3 "missing switch: decision"
[ -f "$BASE/resync_usb" ] && bad "missing switch: marker kept" || ok "missing switch: marker consumed"
has "$LOGF" "reason=missing_switch" "missing switch: reason recorded"
has "$TRACE" "log:resync_usb ignorado" "missing switch: legacy log line kept"

# -------------------------------------------------------------- 4. cooldown
reset_obs
COOLDOWN=1
: > "$BASE/resync_usb"
resync_cycle "$SW_OK" ""
eq "$?" 4 "cooldown: blocked"
has "$LOGF" "reason=cooldown" "cooldown: reason recorded"
hasnt "$TRACE" "bb setsid" "cooldown: no launch"
COOLDOWN=0

# --------------------------------------------------------------- 5. breaker
reset_obs
now=$(cat /proc/uptime | cut -d. -f1)
case "$now" in *[!0-9]*) now=0 ;; esac
echo 3 > "$STATE/rs.count"
echo "$now" > "$STATE/rs.first"
: > "$BASE/resync_usb"
resync_cycle "$SW_OK" ""
eq "$?" 5 "breaker: third cycle in 15min is deferred"
has "$LOGF" "reason=breaker" "breaker: reason recorded"
has "$TRACE" "log:resync breaker" "breaker: legacy log line kept"
hasnt "$TRACE" "bb setsid" "breaker: no launch"

# ---------------------------------------------------------------- 6. launch
reset_obs
echo 0 > "$STATE/rs.count"
echo "$now" > "$STATE/rs.first"
: > "$BASE/resync_usb"
resync_cycle "$SW_OK" ""
eq "$?" 0 "launch: primary switch script used"
wait 2>/dev/null
[ -f "$BASE/resync_usb" ] && bad "launch: marker kept" || ok "launch: marker consumed"
has "$LOGF" "reason=launch" "launch: reason recorded"
has "$TRACE" "switch live" "launch: mode script invoked with 'live'"
has "$TRACE" "bb setsid" "launch: detached through busybox setsid"
L_LOG=$(grep -n "log:resync_usb pedido" "$TRACE" | cut -d: -f1 | head -n 1)
L_BB=$(grep -n "bb setsid" "$TRACE" | cut -d: -f1 | head -n 1)
L_SW=$(grep -n "switch live" "$TRACE" | cut -d: -f1 | head -n 1)
if [ -n "$L_LOG" ] && [ -n "$L_BB" ] && [ -n "$L_SW" ] && [ "$L_LOG" -lt "$L_BB" ] && [ "$L_BB" -lt "$L_SW" ]; then
    ok "launch order: log -> setsid -> switch live"
else
    bad "launch order: log=$L_LOG setsid=$L_BB switch=$L_SW"
fi

# ------------------------------------------------------- 7. fallback candidate
reset_obs
: > "$BASE/resync_usb"
resync_cycle "" "$SW_OK"
eq "$?" 0 "fallback: secondary switch script used"
wait 2>/dev/null
has "$TRACE" "switch live" "fallback: mode script invoked"

# ------------------------------------------------------------ 8. rate gate
GATE_OK=0
i=0
while [ "$i" -lt 5 ]; do
    a=$(cat /proc/uptime | cut -d. -f1)
    obs_gate gadget
    r1=$?
    obs_gate gadget
    r2=$?
    b=$(cat /proc/uptime | cut -d. -f1)
    if [ "$a" = "$b" ]; then
        GATE_OK=1
        break
    fi
    i=$((i + 1))
done
if [ "$GATE_OK" = 1 ]; then
    eq "$r1/$r2" "0/1" "gate: one observation per category per second"
else
    bad "gate: could not sample a stable second"
fi

# ---------------------------------------------------- 9. gadget observation
reset_obs
echo configured > "$USBD/state"
echo 1 > "$USBD/enable"
: > "$USBD/f_mass_storage/lun0/file"
n0=$(wc -l < "$LOGF")
gadget_observe
has "$LOGF" "event=gadget_observation .*reason=gadget_changed" "gadget: changed state logged"
has "$LOGF" "gadget_state=configured gadget_enable=1 lun=empty" "gadget: allowlisted tokens only"
n1=$(wc -l < "$LOGF")
gadget_observe
n2=$(wc -l < "$LOGF")
eq "$n2" "$n1" "gadget: unchanged state emits nothing"

# The kernel writes upper case; the allowlist must accept it and normalize the
# token so the same condition never shows as `other` nor churns.
reset_obs
echo CONFIGURED > "$USBD/state"
echo 1 > "$USBD/enable"
: > "$USBD/f_mass_storage/lun0/file"
gadget_observe
has "$LOGF" "gadget_state=configured gadget_enable=1 lun=empty" "gadget: upper case state normalized"
hasnt "$LOGF" "gadget_state=other" "gadget: upper case is not 'other'"

reset_obs
echo "/data/local/tmp/secret/backing.img" > "$USBD/f_mass_storage/lun0/file"
gadget_observe
has "$LOGF" "lun=present" "gadget: LUN presence sampled"
hasnt "$LOGF" "backing.img" "gadget: raw backing path never logged"
hasnt "$LOGF" "secret" "gadget: path components never logged"

# A read error is distinct from an empty attribute: the classification comes
# from the read result, not from a size test that conflates the two.
reset_obs
rm -f "$USBD/f_mass_storage/lun0/file"
gadget_observe
has "$LOGF" "reason=gadget_unknown" "gadget: unreadable LUN is explicit"
has "$LOGF" "lun=read_error" "gadget: read error classified by content read"

reset_obs
: > "$USBD/f_mass_storage/lun0/file"
rm -f "$USBD/state" "$USBD/enable"
gadget_observe
has "$LOGF" "reason=gadget_unknown" "gadget: unreadable state is explicit"
has "$LOGF" "gadget_state=unknown gadget_enable=unknown" "gadget: unknown tokens"
[ "$(wc -l < "$LOGF")" -gt "$n0" ] && ok "gadget: lines were produced" || bad "gadget: no lines"

# ---------------------------------------------------- 9b. R5 uppercase + content edge cases
reset_obs
echo CONNECTED > "$USBD/state"
echo 1 > "$USBD/enable"
: > "$USBD/f_mass_storage/lun0/file"
gadget_observe
has "$LOGF" "gadget_state=connected" "gadget: upper case CONNECTED normalized"
hasnt "$LOGF" "gadget_state=other" "gadget: CONNECTED is not other"

reset_obs
echo DISCONNECTED > "$USBD/state"
gadget_observe
has "$LOGF" "gadget_state=disconnected" "gadget: upper case DISCONNECTED normalized"

# non-empty LUN content survives command substitution
reset_obs
printf 'backing' > "$USBD/f_mass_storage/lun0/file"
gadget_observe
has "$LOGF" "lun=present" "gadget: non-empty LUN is present"

# unreadable LUN stays read_error regardless of apparent size
reset_obs
rm -f "$USBD/f_mass_storage/lun0/file"
mkdir -p "$USBD/f_mass_storage/lun0/file"
gadget_observe
has "$LOGF" "lun=read_error" "gadget: directory LUN is read_error"
rmdir "$USBD/f_mass_storage/lun0/file"
: > "$USBD/f_mass_storage/lun0/file"

# ---------------------------------------------------- 9c. R5 pending transition after gate refusal
reset_obs
MOCK_UPTIME=100
obs_gate_save() {
    cat <<'ORIG_GATE'
obs_gate() { # $1=categoria -> 0 se ainda nao emitiu neste segundo
    _now=$(cat /proc/uptime 2>/dev/null | cut -d. -f1)
    case "$_now" in *[!0-9]*|"") _now=0 ;; esac
    case "$1" in
        marker)
            [ "$OBS_LAST_MARKER" = "$_now" ] && return 1
            OBS_LAST_MARKER=$_now ;;
        decision)
            [ "$OBS_LAST_DECISION" = "$_now" ] && return 1
            OBS_LAST_DECISION=$_now ;;
        gadget)
            [ "$OBS_LAST_GADGET" = "$_now" ] && return 1
            OBS_LAST_GADGET=$_now ;;
    esac
    return 0
}
ORIG_GATE
}

# Controlled clock eliminates race conditions on /proc/uptime second transitions:
obs_gate() {
    _now=${MOCK_UPTIME:-0}
    case "$1" in
        gadget)
            [ "$OBS_LAST_GADGET" = "$_now" ] && return 1
            OBS_LAST_GADGET=$_now ;;
        *) return 0 ;;
    esac
    return 0
}

echo configured > "$USBD/state"
echo 1 > "$USBD/enable"
: > "$USBD/f_mass_storage/lun0/file"
gadget_observe
n0=$(wc -l < "$LOGF")

# force a different state in the same controlled second (MOCK_UPTIME=100) -> gate refusal
echo connected > "$USBD/state"
gadget_observe
n1=$(wc -l < "$LOGF")

# 1. verify zero lines emitted during refusal
# 2. verify GADGET_LAST preserves prior state
# 3. verify GADGET_PENDING preserves new state
refusal_ok=0
if [ "$n1" -eq "$n0" ] && [ "$GADGET_LAST" = "configured/1/empty" ] && [ "$GADGET_PENDING" = "connected/1/empty" ]; then
    refusal_ok=1
fi

# simulate next eligible second with controlled clock
MOCK_UPTIME=101
gadget_observe
n2=$(wc -l < "$LOGF")

# 4. verify retry emitted gadget_state=connected, cleared pending, and updated last
retry_ok=0
if [ "$n2" -gt "$n1" ] && [ -z "$GADGET_PENDING" ] && [ "$GADGET_LAST" = "connected/1/empty" ] && grep -q "gadget_state=connected" "$LOGF"; then
    retry_ok=1
fi

# restore original production obs_gate
eval "$(obs_gate_save)"

if [ "$refusal_ok" = 1 ] && [ "$retry_ok" = 1 ]; then
    ok "gadget: pending state emitted on next eligible sample"
else
    bad "gadget: deterministic pending transition failed (refusal_ok=$refusal_ok, retry_ok=$retry_ok)"
fi

# ------------------------------------------------------- 10. clock format
CLOCK_OK=0
i=0
while [ "$i" -lt 5 ]; do
    a=$(cat /proc/uptime | cut -d. -f1)
    ms=$(obs_uptime_ms)
    rc=$?
    b=$(cat /proc/uptime | cut -d. -f1)
    if [ "$a" = "$b" ] && [ "$rc" = 0 ]; then
        case "$ms" in
            "$a"[0-9][0-9]0) CLOCK_OK=1 ;;
        esac
        break
    fi
    i=$((i + 1))
done
eq "$CLOCK_OK" 1 "clock: boot_ms is <seconds><centiseconds>0 (no 32-bit multiply)"

# ------------------------------------------------------- 11. line contract
BAD_LINES=0
TOTAL=0
while IFS= read -r line; do
    case "$line" in
        USBTV_OBS*)
            TOTAL=$((TOTAL + 1))
            case "$line" in
                "USBTV_OBS version=1 component=watchdog event="*" pid="*" instance="*" event_seq="*" clock="*" boot_ms="*" precision_ms="*" reason="*) ;;
                *) BAD_LINES=$((BAD_LINES + 1)) ;;
            esac
            [ ${#line} -le 512 ] || BAD_LINES=$((BAD_LINES + 1))
            ;;
    esac
done < "$LOGF"
[ "$TOTAL" -gt 0 ] && ok "contract: $TOTAL observation lines inspected" || bad "contract: no observation lines"
eq "$BAD_LINES" 0 "contract: every line is schema-shaped and <= 512 bytes"

# ------------------------------------- 12. logging failure is decision-neutral
# A directory as the log target makes the `>>` redirect fail with EISDIR for
# every uid (including root), so the control is active unconditionally.
reset_obs
now=$(cat /proc/uptime | cut -d. -f1)
case "$now" in *[!0-9]*) now=0 ;; esac
mkdir -p "$TMP/obsdir"
SAVED_OBS_LOG=$OBS_LOG
OBS_LOG=$TMP/obsdir
echo 0 > "$STATE/rs.count"
echo "$now" > "$STATE/rs.first"
: > "$BASE/resync_usb"
resync_cycle "$SW_OK" ""
eq "$?" 0 "logging failure: launch decision unchanged"
wait 2>/dev/null
has "$TRACE" "switch live" "logging failure: mode script still invoked"
if [ -d "$OBS_LOG" ] && [ -z "$(ls -A "$OBS_LOG" 2>/dev/null)" ]; then
    ok "logging failure: observation write was denied (control active)"
else
    bad "logging failure: unexpected write into the log target"
fi
OBS_LOG=$SAVED_OBS_LOG

# ------------------------------------------ 13. clock domain and event order
UPTIME_B=$(cat /proc/uptime | cut -d. -f1)
case "$UPTIME_B" in *[!0-9]*|"") UPTIME_B=$UPTIME_A ;; esac
UPTIME_HI=$((UPTIME_B + 5))
UPTIME_HI=$((UPTIME_HI * 1000))
BAD_TS=0
BAD_SEQ=0
LAST_SEQ=0
while IFS= read -r line; do
    case "$line" in USBTV_OBS*) ;; *) continue ;; esac
    ms=$(printf '%s\n' "$line" | sed -n 's/.* boot_ms=\([0-9]*\) .*/\1/p')
    if [ -z "$ms" ]; then BAD_TS=$((BAD_TS + 1)); continue; fi
    if [ "$ms" -lt "$UPTIME_LO" ] || [ "$ms" -gt "$UPTIME_HI" ]; then
        BAD_TS=$((BAD_TS + 1))
    fi
    sq=$(printf '%s\n' "$line" | sed -n 's/.* event_seq=\([0-9]*\) .*/\1/p')
    if [ -n "$sq" ]; then
        [ "$sq" -le "$LAST_SEQ" ] && BAD_SEQ=$((BAD_SEQ + 1))
        [ "$sq" -gt "$LAST_SEQ" ] && LAST_SEQ=$sq
    fi
done < "$LOGF"
eq "$BAD_TS" 0 "clock: every boot_ms lies inside the harness uptime window"
eq "$BAD_SEQ" 0 "clock: event_seq increases in file order"

# ---------------------------------------------------------------- summary
echo
echo "passed: $PASS  failed: $FAILED"
[ "$FAILED" -eq 0 ] || exit 1
exit 0
