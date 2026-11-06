#!/bin/bash
# Tests for activate_live_favorites_tablet.sh: tunnels, mandatory integrity,
# real byte-range preflight, early cleanup and activation verification.
set -euo pipefail

ROOT=$(cd -- "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/test-fav-tunnel.XXXXXX")
cleanup() {
    rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

mkdir -p "$TMP/bin" "$TMP/base" "$TMP/usb/f_mass_storage/lun0" "$TMP/pkg/activation" "$TMP/pkg/mnt"
TRACE=$TMP/trace
: > "$TRACE"

# Mock BusyBox
cat > "$TMP/bin/busybox" <<'EOF'
#!/bin/bash
cmd=$1; shift
case "$cmd" in
    sleep)
        # Fast sleep for unit testing
        /bin/sleep 0.02
        ;;
    nc)
        # Check stdin
        payload=$(cat)
        echo "nc:$payload" >> "$TRACE"
        if echo "$payload" | grep -q "PROBE"; then
            if [ -f "$NC_FAIL_LIVE" ]; then
                echo "FAIL"
            else
                printf "ERR source unavailable"
            fi
        elif echo "$payload" | grep -q "GET /vod/"; then
            if [ -f "$NC_FAIL_RANGE" ]; then
                # A proxy that only listens: 503 without media bytes
                printf "HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n"
            elif [ -f "$NC_SHORT_RANGE" ]; then
                # Short body / wrong declared length
                printf "HTTP/1.1 206 Partial Content\r\nContent-Length: 64\r\nContent-Range: bytes 0-63/1000\r\n\r\nshort"
            else
                printf "HTTP/1.1 206 Partial Content\r\nContent-Length: 64\r\nContent-Range: bytes 0-63/1000\r\n\r\n"
                printf '\x00\x00\x00\x18ftyp'
                head -c 56 /dev/zero | tr '\0' 'A'
            fi
        elif echo "$payload" | grep -q "GET /probe"; then
            if [ -f "$NC_FAIL_VOD" ]; then
                printf "HTTP/1.1 403 Forbidden\r\n\r\n"
            else
                printf "HTTP/1.1 200 OK\r\nContent-Length: 19\r\n\r\nOK mixed-vod-proxy\n"
            fi
        fi
        ;;
    sha256sum)
        if [ -f "$SHA_FAIL" ]; then
            echo "checksum mismatch" >&2
            exit 1
        fi
        # Records whether the mode lock was already held when the preflight
        # started; used to prove the lock now wraps the whole switch.
        if [ -d "$BASE/mode_switch.lock" ]; then
            echo "sha:lock=present" >> "$TRACE"
        else
            echo "sha:lock=absent" >> "$TRACE"
        fi
        /usr/bin/sha256sum "$@"
        ;;
    seq)
        /usr/bin/seq "$@"
        ;;
    awk)
        # Emulate the /proc/net/tcp port check only
        for arg in "$@"; do
            if [ "$arg" = "/proc/net/tcp" ]; then
                exit 1
            fi
        done
        exec /usr/bin/awk "$@"
        ;;
    setsid)
        "$@"
        ;;
    flock)
        # Emulate this firmware's busybox flock: only [-sxun] FILE -c CMD.
        # In particular it has no -w, and any test that relies on it must fail
        # here instead of silently passing on the host's util-linux flock.
        for a in "$@"; do
            case "$a" in
                -w|--wait|-E|--conflict-exit-code)
                    echo "flock: unrecognized option: $a" >&2
                    exit 1 ;;
            esac
        done
        exec /usr/bin/flock "$@"
        ;;
    *)
        exec "$cmd" "$@"
        ;;
esac
EOF
chmod +x "$TMP/bin/busybox"

# Mock chisel
cat > "$TMP/base/chisel" <<'EOF'
#!/bin/bash
echo "chisel:$*" >> "$TRACE"
while true; do sleep 1; done
EOF
chmod +x "$TMP/base/chisel"

# Mock switch_tv_mode.sh
echo '#!/bin/sh' > "$TMP/base/switch_tv_mode.sh"
chmod +x "$TMP/base/switch_tv_mode.sh"

# Prepare package directory
PKG=$TMP/base/live-favorites/test-pkg
mkdir -p "$PKG"
echo "token-secret-123456789012345678901234" > "$PKG/transport.token"
echo "chisel-auth-secret" > "$PKG/chisel.auth"
echo '{"tasks": [{"task_id": "a1b2c3d4", "size": 1000, "revision": "md5:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]}' > "$PKG/vod_allowlist.json"
echo "fuse" > "$PKG/fuse_ntfs"
echo "unpack" > "$PKG/sparse_unpack"
gzip -c </dev/null > "$PKG/favorites_template.sparse.gz"
echo "{}" > "$PKG/favorites_package.json"
echo "{}" > "$PKG/favorites_layout.json"
echo "{}" > "$PKG/favorites_manifest.json"

# Exit 0 without touching the image when the tunnel probe fails, and make the
# sparse expansion observable for the early-cleanup test.
cat > "$PKG/sparse_unpack" <<'EOF'
#!/bin/sh
truncate -s 137438953472 "$1"
EOF
chmod +x "$PKG/sparse_unpack"

write_sums() {
    ( cd "$PKG" && rm -f SHA256SUMS
      /usr/bin/sha256sum favorites_template.sparse.gz favorites_layout.json \
          favorites_package.json favorites_manifest.json vod_allowlist.json \
          fuse_ntfs sparse_unpack transport.token chisel.auth > SHA256SUMS )
}

write_manifest() { # generation manifest_id
    cat > "$PKG/favorites_manifest.json" <<JSON
{
  "schema_version": 1,
  "layout_version": 1,
  "generation": $1,
  "manifest_id": "$2",
  "entries": []
}
JSON
}

write_manifest 2 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
write_sums

export TRACE PKG
export NC_FAIL_LIVE=$TMP/nc_fail_live
export NC_FAIL_VOD=$TMP/nc_fail_vod
export NC_FAIL_RANGE=$TMP/nc_fail_range
export NC_SHORT_RANGE=$TMP/nc_short_range
export SHA_FAIL=$TMP/sha_fail

SCRIPT=$ROOT/scripts/activate_live_favorites_tablet.sh
run_script() { # args...
    bash -c "BB=$TMP/bin/busybox BASE=$TMP/base USB=$TMP/usb bash '$SCRIPT' $*"
}

# Test 1: tunnel-start with both ports succeeding
echo "=== Test 1: tunnel-start success ==="
: > "$TRACE"
rm -f "$NC_FAIL_LIVE" "$NC_FAIL_VOD"
run_script "tunnel-start '$PKG' 18190 18191"
grep -q "chisel:client --max-retry-interval 15s http://tv.smre.run.place/chisel 127.0.0.1:18190:127.0.0.1:18190 127.0.0.1:18191:127.0.0.1:18191" "$TRACE"
bash -c "BB=$TMP/bin/busybox BASE=$TMP/base bash '$SCRIPT' tunnel-stop '$PKG'" || true

# Test 2: tunnel-start fails if Live probe fails
echo "=== Test 2: tunnel-start failure on Live ==="
: > "$TRACE"
touch "$NC_FAIL_LIVE"
set +e
run_script "tunnel-start '$PKG' 18190 18191" 2>"$TMP/err.log"
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "manager live tunnel probe failed" "$TMP/err.log"
rm -f "$NC_FAIL_LIVE"

# Test 3: tunnel-start fails if VOD probe fails
echo "=== Test 3: tunnel-start failure on VOD ==="
: > "$TRACE"
touch "$NC_FAIL_VOD"
set +e
run_script "tunnel-start '$PKG' 18190 18191" 2>"$TMP/err.log"
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "vod proxy tunnel probe failed" "$TMP/err.log"
rm -f "$NC_FAIL_VOD"

echo 1 > "$TMP/usb/enable"
echo "/existing/path" > "$TMP/usb/f_mass_storage/lun0/file"

assert_usb_untouched() {
    [ "$(cat "$TMP/usb/enable")" = "1" ]
    [ "$(cat "$TMP/usb/f_mass_storage/lun0/file")" = "/existing/path" ]
}

# Test 4: start fails before touching USB if SHA256SUMS fails
echo "=== Test 4: start fails before touching USB on SHA mismatch ==="
write_sums
touch "$SHA_FAIL"
set +e
bash -c "BB=$TMP/bin/busybox BASE=$TMP/base USB=$TMP/usb bash '$SCRIPT' start '$PKG' 127.0.0.1:18190 session-1 127.0.0.1:18191" 2>"$TMP/err.log"
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "package SHA256 verification failed" "$TMP/err.log"
assert_usb_untouched
rm -f "$SHA_FAIL"

# Test 5: SHA256SUMS is mandatory (missing, then empty)
echo "=== Test 5: SHA256SUMS is mandatory ==="
mv "$PKG/SHA256SUMS" "$TMP/sums.bak"
set +e
bash -c "BB=$TMP/bin/busybox BASE=$TMP/base USB=$TMP/usb bash '$SCRIPT' start '$PKG' 127.0.0.1:18190 session-1 127.0.0.1:18191" 2>"$TMP/err.log"
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "SHA256SUMS missing or empty" "$TMP/err.log"
assert_usb_untouched
: > "$PKG/SHA256SUMS"
set +e
bash -c "BB=$TMP/bin/busybox BASE=$TMP/base USB=$TMP/usb bash '$SCRIPT' start '$PKG' 127.0.0.1:18190 session-1 127.0.0.1:18191" 2>"$TMP/err.log"
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "SHA256SUMS missing or empty" "$TMP/err.log"
assert_usb_untouched
mv "$TMP/sums.bak" "$PKG/SHA256SUMS"

# Test 6: the preflight action requires real bytes, not just a live listener
echo "=== Test 6: preflight requires a proven byte range ==="
run_script "preflight '$PKG' 18190 18191" >/dev/null
touch "$NC_FAIL_RANGE"
set +e
run_script "preflight '$PKG' 18190 18191" 2>"$TMP/err.log"
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "no real VOD bytes through the tunnel" "$TMP/err.log"
rm -f "$NC_FAIL_RANGE"
touch "$NC_SHORT_RANGE"
set +e
run_script "preflight '$PKG' 18190 18191" 2>"$TMP/err.log"
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "no real VOD bytes through the tunnel" "$TMP/err.log"
rm -f "$NC_SHORT_RANGE"
[ ! -e "$PKG/activation/preflight.vod" ]

# Test 7: start fails before touching USB if live tunnel probe fails
echo "=== Test 7: start fails before touching USB on Live tunnel failure ==="
touch "$NC_FAIL_LIVE"
set +e
bash -c "BB=$TMP/bin/busybox BASE=$TMP/base USB=$TMP/usb bash '$SCRIPT' start '$PKG' 127.0.0.1:18190 session-2 127.0.0.1:18191" 2>"$TMP/err.log"
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "preflight failed" "$TMP/err.log"
assert_usb_untouched
[ ! -e "$PKG/template.session-2.bin" ]
rm -f "$NC_FAIL_LIVE"

# Test 8: start fails before touching USB on a VOD range failure
echo "=== Test 8: start fails before touching USB on VOD range failure ==="
touch "$NC_FAIL_RANGE"
set +e
bash -c "BB=$TMP/bin/busybox BASE=$TMP/base USB=$TMP/usb bash '$SCRIPT' start '$PKG' 127.0.0.1:18190 session-3 127.0.0.1:18191" 2>"$TMP/err.log"
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "no real VOD bytes through the tunnel" "$TMP/err.log"
assert_usb_untouched
[ ! -e "$PKG/template.session-3.bin" ]
rm -f "$NC_FAIL_RANGE"

# Test 9: an early failure after the image expansion removes only this
# attempt's artifacts and preserves the previous LUN and playback.
echo "=== Test 9: early cleanup after expansion, lock still owned ==="
mkdir -p "$TMP/base/mode_switch.lock"
echo $$ > "$TMP/base/mode_switch.lock/pid"   # a live owner: lock must not be stolen
set +e
bash -c "BB=$TMP/bin/busybox BASE=$TMP/base USB=$TMP/usb bash '$SCRIPT' start '$PKG' 127.0.0.1:18190 session-4 127.0.0.1:18191" 2>"$TMP/err.log"
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "owns the lock" "$TMP/err.log"
assert_usb_untouched
[ ! -e "$PKG/template.session-4.bin" ]
[ ! -e "$PKG/favorites_fuse" ]
[ -e "$TMP/base/mode_switch.lock" ]
rm -rf "$TMP/base/mode_switch.lock"

# Test 10: verify-active refuses an unconfirmed or mismatched activation
echo "=== Test 10: verify-active confirmation ==="
MOCK_MID=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
write_manifest 2 "$MOCK_MID"
write_sums
set +e
out=$(run_script "verify-active '$PKG' 2 '$MOCK_MID' 2>/dev/null")
rc=$?
set -e
[ "$rc" -ne 0 ]
echo "$out" | grep -q "verify verified=0"
set +e
out=$(run_script "verify-active '$PKG' 9 '$MOCK_MID' 2>/dev/null")
rc=$?
set -e
[ "$rc" -ne 0 ]
echo "$out" | grep -q "verify verified=0"

# Simulate the active state: candidate process alive, LUN owned, result active
cp /bin/sleep "$PKG/favorites_fuse"
"$PKG/favorites_fuse" 30 &
FAKE_PID=$!
echo "$FAKE_PID" > "$PKG/activation/candidate.pid"
echo active > "$PKG/activation/result"
echo "$PKG/mnt/tv_stream.img" > "$TMP/usb/f_mass_storage/lun0/file"
out=$(run_script "verify-active '$PKG' 2 '$MOCK_MID'")
echo "$out" | grep -q "verify verified=1 generation=2 manifest_id=$MOCK_MID"
set +e
out=$(run_script "verify-active '$PKG' 2 cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc")
rc=$?
set -e
[ "$rc" -ne 0 ]
echo "$out" | grep -q "verify verified=0"
kill "$FAKE_PID" 2>/dev/null || true
rm -f "$PKG/favorites_fuse" "$PKG/activation/candidate.pid" "$PKG/activation/result"
echo "/existing/path" > "$TMP/usb/f_mass_storage/lun0/file"

# Test 11: handoff-lease refuses while another live owner holds the mode lock
echo "=== Test 11: handoff-lease refuses a live foreign lock ==="
TOK=0123456789abcdef0123456789abcdef
OTHER=ffffffffffffffffffffffffffffffff
LEASE=$TMP/base/mixed_handoff.lease
mkdir -p "$TMP/base/mode_switch.lock"
echo $$ > "$TMP/base/mode_switch.lock/pid"
set +e
run_script "handoff-lease '$PKG' $TOK 30" 2>"$TMP/err.log"
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "previous_generation_owns_lock" "$TMP/err.log"
[ ! -e "$LEASE" ]
rm -rf "$TMP/base/mode_switch.lock"

# Test 12: handoff-lease writes a valid 600 lease with exactly one line
echo "=== Test 12: handoff-lease writes a valid 600 lease ==="
out=$(run_script "handoff-lease '$PKG' $TOK 45")
[ "$out" = "handoff-lease token=$TOK expires_in=45" ]
[ -f "$LEASE" ]
[ "$(stat -c %a "$LEASE")" = 600 ]
[ "$(wc -l < "$LEASE")" -eq 1 ]
read -r ltok lpid lexp < "$LEASE"
[ "$ltok" = "$TOK" ]
case "$lpid" in ''|*[!0-9]*) echo "bad lease pid: $lpid" >&2; exit 1 ;; esac
case "$lexp" in ''|*[!0-9]*) echo "bad lease expiry: $lexp" >&2; exit 1 ;; esac
now=$(date +%s)
[ "$lexp" -gt "$now" ]
[ "$lexp" -le "$((now + 60))" ]

# Test 13: handoff-status reports presence and ownership without leaking a lease
echo "=== Test 13: handoff-status presence/ownership ==="
out=$(run_script "handoff-status '$PKG'")
case "$out" in "handoff present=1 ours=0 expires_in="*) ;; *) echo "bad status: $out" >&2; exit 1 ;; esac
out=$(run_script "handoff-status '$PKG' $TOK")
case "$out" in "handoff present=1 ours=1 expires_in="*) ;; *) echo "bad status: $out" >&2; exit 1 ;; esac
out=$(run_script "handoff-status '$PKG' $OTHER")
case "$out" in "handoff present=1 ours=0 expires_in="*) ;; *) echo "bad status: $out" >&2; exit 1 ;; esac
rm -f "$LEASE"
out=$(run_script "handoff-status '$PKG' $TOK")
[ "$out" = "handoff present=0 ours=0 expires_in=0" ]

# Test 14: handoff-release removes only the matching token
echo "=== Test 14: handoff-release only removes its own token ==="
run_script "handoff-lease '$PKG' $TOK 30" >/dev/null
out=$(run_script "handoff-release '$PKG' $OTHER")
[ "$out" = "handoff-release released=0" ]
[ -f "$LEASE" ]
out=$(run_script "handoff-release '$PKG' $TOK")
[ "$out" = "handoff-release released=1" ]
[ ! -e "$LEASE" ]

# Test 15: start with a token and no lease aborts before ANY artifact exists
echo "=== Test 15: start with a token and no lease creates nothing ==="
rm -f "$LEASE"
: > "$TRACE"
set +e
bash -c "BB=$TMP/bin/busybox BASE=$TMP/base USB=$TMP/usb bash '$SCRIPT' start '$PKG' 127.0.0.1:18190 session-lease0 127.0.0.1:18191 $TOK" 2>"$TMP/err.log"
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "handoff lease missing or expired" "$TMP/err.log"
assert_usb_untouched
[ ! -e "$PKG/template.session-lease0.bin" ]
[ ! -e "$PKG/favorites_fuse" ]
[ ! -e "$TMP/base/mode_switch.lock" ]
[ ! -e "$PKG/activation/handoff.token" ]
[ ! -s "$TRACE" ]

# Test 16: start with a matching lease enters the locked switch; the lock is
# held while the preflights run and the trap removes it (and our artifacts) on
# failure, leaving the previous LUN untouched.
echo "=== Test 16: start with a matching lease holds the lock through preflights ==="
run_script "handoff-lease '$PKG' $TOK 60" >/dev/null
: > "$TRACE"
touch "$NC_FAIL_LIVE"
set +e
bash -c "BB=$TMP/bin/busybox BASE=$TMP/base USB=$TMP/usb bash '$SCRIPT' start '$PKG' 127.0.0.1:18190 session-lease1 127.0.0.1:18191 $TOK" 2>"$TMP/err.log"
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "preflight failed" "$TMP/err.log"
grep -q "sha:lock=present" "$TRACE"
assert_usb_untouched
[ ! -e "$PKG/template.session-lease1.bin" ]
[ ! -e "$PKG/favorites_fuse" ]
[ ! -e "$PKG/activation/preflight.vod" ]
[ ! -e "$TMP/base/mode_switch.lock" ]
[ ! -e "$PKG/activation/handoff.token" ]
[ ! -e "$LEASE" ]
rm -f "$NC_FAIL_LIVE"

# Test 17: verify-active appends the guard state to its single line
echo "=== Test 17: verify-active reports lock_owned and lease ==="
cp /bin/sleep "$PKG/favorites_fuse"
"$PKG/favorites_fuse" 30 &
FAKE_PID=$!
echo "$FAKE_PID" > "$PKG/activation/candidate.pid"
echo "$FAKE_PID" > "$PKG/activation/supervisor.pid"
echo active > "$PKG/activation/result"
echo "$PKG/mnt/tv_stream.img" > "$TMP/usb/f_mass_storage/lun0/file"
mkdir -p "$TMP/base/mode_switch.lock"
echo "$FAKE_PID" > "$TMP/base/mode_switch.lock/pid"
out=$(run_script "verify-active '$PKG' 2 '$MOCK_MID'")
echo "$out" | grep -q "verify verified=1 generation=2 manifest_id=$MOCK_MID lock_owned=1 lease=none"
exp=$(( $(date +%s) + 60 ))
printf '%s %s %s\n' "$TOK" 4242 "$exp" > "$LEASE"
printf '%s\n' "$TOK" > "$PKG/activation/handoff.token"
out=$(run_script "verify-active '$PKG' 2 '$MOCK_MID'")
echo "$out" | grep -q "lock_owned=1 lease=ours"
printf '%s\n' "$OTHER" > "$PKG/activation/handoff.token"
out=$(run_script "verify-active '$PKG' 2 '$MOCK_MID'")
echo "$out" | grep -q "lock_owned=1 lease=foreign"
# A malformed lease is absent everywhere.
printf 'not-a-lease\n' > "$LEASE"
out=$(run_script "verify-active '$PKG' 2 '$MOCK_MID'")
echo "$out" | grep -q "lock_owned=1 lease=none"
rm -f "$PKG/activation/handoff.token" "$LEASE"
rm -rf "$TMP/base/mode_switch.lock"
kill "$FAKE_PID" 2>/dev/null || true
rm -f "$PKG/favorites_fuse" "$PKG/activation/candidate.pid" "$PKG/activation/supervisor.pid" "$PKG/activation/result"
echo "/existing/path" > "$TMP/usb/f_mass_storage/lun0/file"

echo "=== Test 18: concurrent handoff-lease — exactly one publisher wins ==="
rm -f "$LEASE"; rm -rf "$TMP/base/mixed_handoff.lease.lock"
: > "$TMP/lease.serial"
concurrent_lease() { # token -> appends "win" or "refused"
    _cl_tok=$1
    if out=$(run_script "handoff-lease '$PKG' $_cl_tok 30" 2>"$TMP/lease.$_cl_tok.err"); then
        case "$out" in "handoff-lease token=$_cl_tok expires_in=30") echo "win $_cl_tok" >> "$TMP/lease.serial" ;;
                        *) echo "odd $_cl_tok" >> "$TMP/lease.serial" ;; esac
    else
        echo "refused $_cl_tok" >> "$TMP/lease.serial"
    fi
}
# Ten racing acquisitions with distinct tokens: one winner, the lease is the
# winner's, and every loser was refused (never a silent overwrite).
for i in 1 2 3 4 5 6 7 8 9 a; do
    concurrent_lease "${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}" &
done
wait
wins=$(grep -c '^win ' "$TMP/lease.serial" || true)
refusals=$(grep -c '^refused ' "$TMP/lease.serial" || true)
odds=$(grep -c '^odd ' "$TMP/lease.serial" || true)
[ "$odds" -eq 0 ]
[ "$wins" -eq 1 ]
[ "$refusals" -eq 9 ]
read -r wtok wpid wexp < "$LEASE"
[ "$(grep '^win ' "$TMP/lease.serial" | awk '{print $2}')" = "$wtok" ]
[ -f "$LEASE" ]
[ "$(stat -c %a "$LEASE")" = 600 ]
# The flock file may exist; what matters is that nobody holds it.
"$TMP/bin/busybox" flock -n "$TMP/base/mixed_handoff.lease.lock" -c true 2>/dev/null \
    || { echo "FAIL: lease lock still held after the race" >&2; exit 1; }
echo "OK"

echo "=== Test 19: a live foreign lease is never overwritten ==="
# The lease from Test 18 is live: another publisher must be refused and the
# stored token must be byte-identical afterwards.
cp "$LEASE" "$TMP/lease.before"
set +e
run_script "handoff-lease '$PKG' $OTHER 30" 2>"$TMP/err.log"
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q 'lease-cs refused reason=lease_held_by_other' "$TMP/err.log"
cmp -s "$LEASE" "$TMP/lease.before"
"$TMP/bin/busybox" flock -n "$TMP/base/mixed_handoff.lease.lock" -c true 2>/dev/null \
    || { echo "FAIL: lease lock left held after a refusal" >&2; exit 1; }
echo "OK"

echo "=== Test 20: handoff-renew extends only the owner's lease ==="
# A different token can never be renewed.
set +e
run_script "handoff-renew '$PKG' $OTHER 30" 2>"$TMP/err.log"
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q 'lease-cs refused reason=lease_not_ours' "$TMP/err.log"
cmp -s "$LEASE" "$TMP/lease.before"
# The owner renews: same token, same owner pid, strictly later expiry.
read -r _wt _wp _we < "$TMP/lease.before"
out=$(run_script "handoff-renew '$PKG' $wtok 120")
[ "$out" = "handoff-renew token=$wtok expires_in=120" ]
read -r rtok rpid rexp < "$LEASE"
[ "$rtok" = "$_wt" ]
[ "$rpid" = "$_wp" ]
[ "$rexp" -gt "$_we" ]
[ "$(stat -c %a "$LEASE")" = 600 ]
"$TMP/bin/busybox" flock -n "$TMP/base/mixed_handoff.lease.lock" -c true 2>/dev/null \
    || { echo "FAIL: lease lock left held after a renewal" >&2; exit 1; }
# A malformed renewal never creates a lease out of thin air.
rm -f "$LEASE"
set +e
run_script "handoff-renew '$PKG' $wtok 30" 2>"$TMP/err.log"
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q 'lease-cs refused reason=lease_missing' "$TMP/err.log"
[ ! -e "$LEASE" ]
echo "OK"

echo "=== Test 21: an expired lease is treated as absent and is reclaimable ==="
rm -f "$LEASE"
printf '%s %s %s\n' "$TOK" "$$" "$(( $(date +%s) - 5 ))" > "$LEASE"
chmod 600 "$LEASE"
out=$(run_script "handoff-status '$PKG' $TOK")
[ "$out" = "handoff present=0 ours=0 expires_in=0" ]
set +e
run_script "handoff-renew '$PKG' $TOK 30" 2>"$TMP/err.log"
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q 'lease-cs refused reason=lease_missing' "$TMP/err.log"
# Another publisher can take it over once it expired.
out=$(run_script "handoff-lease '$PKG' $OTHER 30")
[ "$out" = "handoff-lease token=$OTHER expires_in=30" ]
read -r ntok npid nexp < "$LEASE"
[ "$ntok" = "$OTHER" ]
# Re-acquiring with our own token is idempotent, not a refusal.
out=$(run_script "handoff-lease '$PKG' $OTHER 30")
[ "$out" = "handoff-lease token=$OTHER expires_in=30" ]
rm -f "$LEASE"
echo "OK"

echo "=== Test 22: a dead holder's lock never blocks, a live flock does ==="
rm -f "$LEASE"
# A lock left by a process that died: the kernel already released it, so
# acquisition must succeed immediately (no reclaim logic needed).
LIVE_HOLDER=$TMP/lease.holder
bash -c "exec 9>'$TMP/base/mixed_handoff.lease.lock'; $TMP/bin/busybox flock -n 9; touch '$LIVE_HOLDER'; sleep 30" &
HOLDER_PID=$!
for _i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    [ -f "$LIVE_HOLDER" ] && break
    sleep 0.1
done
[ -f "$LIVE_HOLDER" ] || { echo "FAIL: holder did not take the lock" >&2; exit 1; }
set +e
out=$(run_script "handoff-lease '$PKG' $OTHER 30" 2>"$TMP/err.log")
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q 'being acquired by another publisher' "$TMP/err.log"
[ ! -e "$LEASE" ]
kill -9 "$HOLDER_PID" 2>/dev/null || true
wait "$HOLDER_PID" 2>/dev/null || true
# The holder is gone: nothing has to be cleaned up, the lock is free.
out=$(run_script "handoff-lease '$PKG' $TOK 30" 2>"$TMP/err.log")
[ "$out" = "handoff-lease token=$TOK expires_in=30" ]
[ -f "$LEASE" ]
rm -f "$LEASE"
echo "OK"

echo "=== Test 23: racing contenders — exactly one wins, no lock is stolen ==="
rm -f "$LEASE"; rm -f "$TMP/base/mixed_handoff.lease.lock"
: > "$TMP/base/mixed_handoff.lease.lock"   # leftover file from a dead holder
: > "$TMP/reclaim.serial"
reclaim_lease() {
    _rl_tok=$1
    if out=$(run_script "handoff-lease '$PKG' $_rl_tok 30" 2>"$TMP/reclaim.$_rl_tok.err"); then
        case "$out" in "handoff-lease token=$_rl_tok expires_in=30") echo "win $_rl_tok" >> "$TMP/reclaim.serial" ;;
                        *) echo "odd $_rl_tok" >> "$TMP/reclaim.serial" ;; esac
    else
        echo "refused $_rl_tok" >> "$TMP/reclaim.serial"
    fi
}
for i in 1 2 3 4 5 6 7 8; do
    reclaim_lease "${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}${i}" &
done
wait
[ "$(grep -c '^odd ' "$TMP/reclaim.serial" || true)" -eq 0 ]
[ "$(grep -c '^win ' "$TMP/reclaim.serial" || true)" -eq 1 ]
[ "$(grep -c '^refused ' "$TMP/reclaim.serial" || true)" -eq 7 ]
read -r _rtok _rpid _rexp < "$LEASE"
[ "$(grep '^win ' "$TMP/reclaim.serial" | awk '{print $2}')" = "$_rtok" ]
"$TMP/bin/busybox" flock -n "$TMP/base/mixed_handoff.lease.lock" -c true 2>/dev/null \
    || { echo "FAIL: lock left held after the race" >&2; exit 1; }
rm -f "$LEASE"
echo "OK"

echo "=== Test 24: takeover refuses a foreign owner and never TERMs it ==="
# A live lock owned by a process that is NOT the named previous session must be
# refused: the takeover may only stop what it can prove.
FOREIGN_ROOT=$TMP/foreign
mkdir -p "$FOREIGN_ROOT"
# Lease first (no lock yet), then a foreign lock appears: only the takeover
# sees it, and it must refuse.
run_script "handoff-lease '$PKG' $TOK 60" >/dev/null
sleep 300 &
FOREIGN_PID=$!
mkdir -p "$TMP/base/mode_switch.lock"
echo "$FOREIGN_PID" > "$TMP/base/mode_switch.lock/pid"
set +e
out=$(run_script "handoff-takeover '$PKG' $TOK other-session" 2>&1)
rc=$?
set -e
# The exit status is masked by adb/su on the real tablet, so the printed
# marker is the contract; the human reason travels on stderr.
grep -q 'takeover released=0' <<<"$out"
grep -q 'reason=lock_foreign' <<<"$out"
grep -q 'foreign lock owner; refusing to take over' <<<"$out"
kill -0 "$FOREIGN_PID" 2>/dev/null && echo "foreign owner untouched" || { echo "FAIL: killed a foreign process" >&2; exit 1; }
[ ! -e "$TMP/base/live-favorites/other-session/activation/takeover.request" ]
rm -rf "$TMP/base/mode_switch.lock"; kill -9 "$FOREIGN_PID" 2>/dev/null || true; wait "$FOREIGN_PID" 2>/dev/null || true
rm -f "$LEASE"
echo "OK"

echo "=== Test 25: takeover refuses when the previous activation dir is missing ==="
run_script "handoff-lease '$PKG' $TOK 60" >/dev/null
sleep 300 &
SAME_PID=$!
mkdir -p "$TMP/base/mode_switch.lock"
echo "$SAME_PID" > "$TMP/base/mode_switch.lock/pid"
set +e
out=$(run_script "handoff-takeover '$PKG' $TOK test-pkg" 2>&1)
rc=$?
set -e
grep -q 'takeover released=0' <<<"$out"
grep -q 'reason=lock_foreign' <<<"$out"
kill -0 "$SAME_PID" 2>/dev/null || { echo "FAIL: killed the lock owner" >&2; exit 1; }
rm -rf "$TMP/base/mode_switch.lock"; kill -9 "$SAME_PID" 2>/dev/null || true; wait "$SAME_PID" 2>/dev/null || true
rm -f "$LEASE"
echo "OK"

echo "=== Test 26: rollback restores legacy when there is NO handoff marker ==="
# The normal TERM path (watchdog, manual switch, crash) must keep working
# exactly as before: no marker means legacy restoration is attempted.
NO_HO_ROOT=$TMP/nohandoff
mkdir -p "$NO_HO_ROOT/live-favorites/prevpkg/activation"
# Each scenario gets its own BASE, so the legacy switch mock must live there:
# the script resolves SWITCH as $BASE/switch_tv_mode.sh.
cat > "$NO_HO_ROOT/switch_tv_mode.sh" <<'EOF'
#!/bin/sh
echo "switch:$*" >> "$TRACE"
exit 0
EOF
chmod +x "$NO_HO_ROOT/switch_tv_mode.sh"
NO_HO_PKG=$NO_HO_ROOT/live-favorites/prevpkg
cp -f "$PKG/transport.token" "$NO_HO_PKG/transport.token" 2>/dev/null || true
mkdir -p "$NO_HO_PKG/activation"
echo "ab27600d" > "$NO_HO_PKG/activation/previous_vod"
# Own the LUN, otherwise rollback correctly reports "stopped_before_activation".
echo "$NO_HO_PKG/mnt/tv_stream.img" > "$TMP/usb/f_mass_storage/lun0/file"
echo 1 > "$TMP/usb/enable"
: > "$TRACE"
bash -c "BB=$TMP/bin/busybox BASE=$NO_HO_ROOT USB=$TMP/usb bash '$SCRIPT' rollback '$NO_HO_PKG'" >/dev/null 2>&1 || true
grep -q "switch:vod ab27600d" "$TRACE" || { echo "FAIL: legacy restore did not run without a marker" >&2; cat "$TRACE" >&2; exit 1; }
grep -q "restored" "$NO_HO_PKG/activation/result"
echo "OK"

echo "=== Test 27: rollback suppresses legacy restore ONLY with a valid handoff marker ==="
HO_ROOT=$TMP/handoff
HO_PKG=$HO_ROOT/live-favorites/prevpkg
mkdir -p "$HO_PKG/activation"
cat > "$HO_ROOT/switch_tv_mode.sh" <<'EOF'
#!/bin/sh
echo "switch:$*" >> "$TRACE"
exit 0
EOF
chmod +x "$HO_ROOT/switch_tv_mode.sh"
cp -f "$PKG/transport.token" "$HO_PKG/transport.token" 2>/dev/null || true
echo "ab27600d" > "$HO_PKG/activation/previous_vod"
echo "$HO_PKG/mnt/tv_stream.img" > "$TMP/usb/f_mass_storage/lun0/file"
echo 1 > "$TMP/usb/enable"
# A live lease owned by an unrelated token must NOT authorise suppression.
_HO_OTHER=11111111111111111111111111111111
run_script "handoff-lease '$PKG' $_HO_OTHER 60" >/dev/null
printf '%s %s\n' "$TOK" "$$" > "$HO_PKG/activation/takeover.request"
: > "$TRACE"
bash -c "BB=$TMP/bin/busybox BASE=$HO_ROOT USB=$TMP/usb bash '$SCRIPT' rollback '$HO_PKG'" >/dev/null 2>&1 || true
grep -q "switch:vod ab27600d" "$TRACE" || { echo "FAIL: stale/foreign marker suppressed a legitimate restore" >&2; exit 1; }
[ ! -e "$HO_PKG/activation/takeover.request" ]
# Release the probe lease so the next scenario starts from a clean sheet.
rm -f "$LEASE"
echo "OK"

echo "=== Test 28: a takeover without the supervisor's ack is UNKNOWN, never success ==="
# The exact production failure: the previous supervisor is gone (possibly after
# restoring the legacy mode) but nothing proves the handoff was accepted.
# The takeover proves ownership with /proc/<pid>/cmdline against $BASE, so the
# previous package must live under the same BASE this script runs with.
ACK_ROOT=$TMP/base
ACK_PKG=$ACK_ROOT/live-favorites/ackprev
mkdir -p "$ACK_PKG/activation"
cp -f "$PKG/transport.token" "$ACK_PKG/transport.token" 2>/dev/null || true
run_script "handoff-lease '$PKG' $TOK 60" >/dev/null
# A supervisor of the previous session, alive and owning the lock. It dies on
# TERM (like a supervisor that restores the legacy mode) WITHOUT ever writing
# the handoff acceptance record.
bash -c "exec -a 'busybox sh $ACK_PKG/activate_live_favorites_tablet.sh start' sleep 300" &
ACK_OWNER=$!
sleep 0.3
mkdir -p "$TMP/base/mode_switch.lock"
echo "$ACK_OWNER" > "$TMP/base/mode_switch.lock/pid"
set +e
out=$(run_script "handoff-takeover '$PKG' $TOK ackprev" 2>&1)
rc=$?
set -e
wait "$ACK_OWNER" 2>/dev/null || true
rm -rf "$TMP/base/mode_switch.lock"
grep -q 'takeover released=unknown' <<<"$out" || { echo "FAIL: missing ack must be unknown: $out" >&2; exit 1; }
grep -q 'reason=no_ack' <<<"$out"
! grep -q 'takeover released=1' <<<"$out"
rm -f "$LEASE"
echo "OK"

echo "=== Test 29: a refusal before any signal is explicit and inert ==="
# Lease first (no lock yet), then the foreign lock appears at takeover time.
run_script "handoff-lease '$PKG' $TOK 60" >/dev/null
sleep 300 &
RR_PID=$!
mkdir -p "$TMP/base/mode_switch.lock"
echo "$RR_PID" > "$TMP/base/mode_switch.lock/pid"
set +e
out=$(run_script "handoff-takeover '$PKG' $TOK foreign-session" 2>&1)
set -e
grep -q 'takeover released=0' <<<"$out" || { echo "FAIL: expected explicit refusal: $out" >&2; exit 1; }
grep -q 'reason=lock_foreign' <<<"$out"
kill -0 "$RR_PID" 2>/dev/null || { echo "FAIL: refusal killed the owner" >&2; exit 1; }
rm -rf "$TMP/base/mode_switch.lock"; kill -9 "$RR_PID" 2>/dev/null || true; wait "$RR_PID" 2>/dev/null || true
rm -f "$LEASE"
echo "OK"

echo "=== Test 30: the marker is bound to the process incarnation, not just the pid ==="
# A marker naming a DIFFERENT incarnation of the same pid must not authorise
# suppressing the legacy restore.
INC_ROOT=$TMP/incarnation
INC_PKG=$INC_ROOT/live-favorites/prevpkg
mkdir -p "$INC_PKG/activation"
cp -f "$PKG/transport.token" "$INC_PKG/transport.token" 2>/dev/null || true
echo "ab27600d" > "$INC_PKG/activation/previous_vod"
echo "$INC_PKG/mnt/tv_stream.img" > "$TMP/usb/f_mass_storage/lun0/file"
echo 1 > "$TMP/usb/enable"
cat > "$INC_ROOT/switch_tv_mode.sh" <<'EOF'
#!/bin/sh
echo "switch:$*" >> "$TRACE"
exit 0
EOF
chmod +x "$INC_ROOT/switch_tv_mode.sh"
run_script "handoff-lease '$PKG' $TOK 60" >/dev/null
printf '%s %s %s\n' "$TOK" "$$" "999999-424242" > "$INC_PKG/activation/takeover.request"
mkdir -p "$INC_ROOT/mode_switch.lock"
echo "$$" > "$INC_ROOT/mode_switch.lock/pid"
: > "$TRACE"
bash -c "BB=$TMP/bin/busybox BASE=$INC_ROOT USB=$TMP/usb bash '$SCRIPT' rollback '$INC_PKG'" >/dev/null 2>&1 || true
grep -q "switch:vod ab27600d" "$TRACE" || { echo "FAIL: foreign incarnation suppressed the restore" >&2; exit 1; }
[ ! -e "$INC_PKG/activation/handoff.ack" ]
rm -rf "$INC_ROOT/mode_switch.lock"; rm -f "$LEASE"
echo "OK"

echo "=== Test 31: a handoff with no lock at all is free, not refused ==="
# The previous supervisor is already gone: activating is safe, and reporting a
# refusal here would block a switch that cannot hurt anyone.
rm -f "$LEASE"
run_script "handoff-lease '$PKG' $TOK 60" >/dev/null
rm -rf "$TMP/base/mode_switch.lock"
set +e
out=$(run_script "handoff-takeover '$PKG' $TOK nosuchprev" 2>&1)
rc=$?
set -e
grep -q 'takeover released=1' <<<"$out" || { echo "FAIL: expected a free handoff: $out" >&2; exit 1; }
grep -q 'already_free=1' <<<"$out"
rm -f "$LEASE"
echo "OK"

# Test 32: describe reports the running supervisor's identity, not the path
#
# Path is a location, not an identity: a generation recovered into an alternative
# directory supervises the same content from elsewhere, and correlating by path is
# exactly what refused publication 19. This action must therefore expose an
# incarnation the caller can compare.
echo "=== Test 32: describe exposes incarnation identity, read-only ==="
cp /bin/sleep "$PKG/favorites_fuse"
"$PKG/favorites_fuse" 30 &
DESC_PID=$!
echo "$DESC_PID" > "$PKG/activation/candidate.pid"
echo "$DESC_PID" > "$PKG/activation/supervisor.pid"
echo active > "$PKG/activation/result"
echo "$PKG/mnt/tv_stream.img" > "$TMP/usb/f_mass_storage/lun0/file"
mkdir -p "$TMP/base/mode_switch.lock"
echo "$DESC_PID" > "$TMP/base/mode_switch.lock/pid"
printf 'handoff.ack 1\n' > "$PKG/activation/handoff.protocol"
out=$(run_script "describe '$PKG'")
echo "$out" | grep -q "describe protocol=handoff.ack 1" \
    || { echo "FAIL: describe must report the protocol: $out" >&2; exit 1; }
echo "$out" | grep -q "generation=2" \
    || { echo "FAIL: describe must report the generation: $out" >&2; exit 1; }
echo "$out" | grep -q "manifest_id=$MOCK_MID" \
    || { echo "FAIL: describe must report the manifest id: $out" >&2; exit 1; }
echo "$out" | grep -q "lock_owned=1" \
    || { echo "FAIL: describe must prove lock ownership: $out" >&2; exit 1; }
# The incarnation is "<pid>-<starttime>", so a recycled pid is distinguishable.
echo "$out" | grep -qE "supervisor=${DESC_PID}-[0-9]+" \
    || { echo "FAIL: describe must report pid plus starttime: $out" >&2; exit 1; }
echo "$out" | grep -qE "candidate=${DESC_PID}-[0-9]+" \
    || { echo "FAIL: describe must report the candidate incarnation: $out" >&2; exit 1; }
echo "$out" | grep -q "lun=$PKG/mnt/tv_stream.img" \
    || { echo "FAIL: describe must report the LUN: $out" >&2; exit 1; }
# Read-only: a describe must not release the lock, change the result or move the LUN.
[ "$(cat "$PKG/activation/result")" = active ] \
    || { echo "FAIL: describe changed the activation result" >&2; exit 1; }
[ "$(cat "$TMP/base/mode_switch.lock/pid")" = "$DESC_PID" ] \
    || { echo "FAIL: describe touched the mode lock" >&2; exit 1; }
[ "$(cat "$TMP/usb/f_mass_storage/lun0/file")" = "$PKG/mnt/tv_stream.img" ] \
    || { echo "FAIL: describe moved the LUN" >&2; exit 1; }
kill "$DESC_PID" 2>/dev/null || true
rm -rf "$TMP/base/mode_switch.lock"
echo "OK"

# Test 33: describe on a package with no manifest still answers, saying none
echo "=== Test 33: describe reports none instead of inventing fields ==="
BARE=$TMP/base/live-favorites/bare-pkg
mkdir -p "$BARE/activation"
out=$(run_script "describe '$BARE'")
echo "$out" | grep -q "generation=none" \
    || { echo "FAIL: describe invented a generation: $out" >&2; exit 1; }
echo "$out" | grep -q "manifest_id=none" \
    || { echo "FAIL: describe invented a manifest id: $out" >&2; exit 1; }
echo "$out" | grep -q "protocol=none" \
    || { echo "FAIL: describe invented a protocol: $out" >&2; exit 1; }
echo "OK"

echo "ALL TUNNEL, INTEGRITY, PREFLIGHT AND VERIFICATION TESTS PASSED!"
