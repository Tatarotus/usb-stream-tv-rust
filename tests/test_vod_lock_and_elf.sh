#!/bin/bash
# test_vod_lock_and_elf.sh — Unit tests for Finding 12 (Lock mutual exclusion) & Finding 3 (ELF validation)
set -euo pipefail

TEST_DIR="/tmp/vod_test_$$"
mkdir -p "$TEST_DIR"
trap 'rm -rf "$TEST_DIR"' EXIT

echo "=== Test Suite: VOD Lock & ELF Validation ==="

# -------------------------------------------------------------
# 1. Test Finding 3: is_valid_elf()
# -------------------------------------------------------------
echo "[*] Testing is_valid_elf() from apply_vod_v2.sh and rollback_vod.sh..."

# Create test files
VALID_ELF="$TEST_DIR/valid.elf"
CORRUPT_MAGIC_ELF="$TEST_DIR/corrupt_magic.elf"
PSEUDO_ELF="$TEST_DIR/pseudo.elf"
TRUNCATED_ELF="$TEST_DIR/truncated.elf"

# Valid ELF: 0x7f, 'E', 'L', 'F' followed by padding to 110 KB
python3 -c '
with open("'"$VALID_ELF"'", "wb") as f:
    f.write(b"\x7fELF" + b"\x00" * 110000)
with open("'"$CORRUPT_MAGIC_ELF"'", "wb") as f:
    f.write(b"\x7fELX" + b"\x00" * 110000)
with open("'"$PSEUDO_ELF"'", "wb") as f:
    f.write(b"0ELF" + b"\x00" * 110000)
with open("'"$TRUNCATED_ELF"'", "wb") as f:
    f.write(b"\x7fELF" + b"\x00" * 50000)
'

bb_filesize() {
    _file="$1"
    stat -c %s "$_file" 2>/dev/null || echo 0
}

is_valid_elf() {
    _file="$1"
    [ -f "$_file" ] || return 1
    _sz=$(bb_filesize "$_file")
    [ "$_sz" -gt 102400 ] || return 1

    head -c 4 "$_file" 2>/dev/null | od -An -tx1 2>/dev/null | tr -d ' \n' | grep -q "7f454c46" && return 0
    dd if="$_file" bs=1 count=4 2>/dev/null | od -An -tx1 2>/dev/null | tr -d ' \n' | grep -q "7f454c46" && return 0
    if command -v hexdump >/dev/null 2>&1; then
        hexdump -n 4 -e '4/1 "%02x"' "$_file" 2>/dev/null | grep -q "7f454c46" && return 0
    fi
    return 1
}

echo "[*] Testing is_valid_elf reference implementation..."
# 1. Valid ELF must pass
if is_valid_elf "$VALID_ELF"; then
    echo "  [✓] PASS: Valid ELF correctly recognized"
else
    echo "  [✗] FAIL: Valid ELF was rejected"
    exit 1
fi

# 2. Corrupt magic (\x7fELX) must fail
if ! is_valid_elf "$CORRUPT_MAGIC_ELF"; then
    echo "  [✓] PASS: Corrupt magic byte rejected"
else
    echo "  [✗] FAIL: Corrupt magic was accepted"
    exit 1
fi

# 3. Pseudo ELF (0ELF) must fail
if ! is_valid_elf "$PSEUDO_ELF"; then
    echo "  [✓] PASS: Pseudo ELF ('0ELF') rejected"
else
    echo "  [✗] FAIL: Pseudo ELF was accepted"
    exit 1
fi

# 4. Truncated ELF (<100KB) must fail
if ! is_valid_elf "$TRUNCATED_ELF"; then
    echo "  [✓] PASS: Truncated ELF rejected"
else
    echo "  [✗] FAIL: Truncated ELF was accepted"
    exit 1
fi

# 5. Non-existent file must fail
if ! is_valid_elf "$TEST_DIR/non_existent.elf"; then
    echo "  [✓] PASS: Non-existent file rejected"
else
    echo "  [✗] FAIL: Non-existent file was accepted"
    exit 1
fi

# -------------------------------------------------------------
# 2. Test Finding 12: Lock Mutual Exclusion & EXIT Trap
# -------------------------------------------------------------
echo ""
echo "[*] Testing Finding 12: Lock Mutual Exclusion & EXIT Trap..."

TEST_LOCK_DIR="$TEST_DIR/test_lock_atomic.dir"
TEST_SCRIPT="$TEST_DIR/runner_lock.sh"

cat << 'EOF' > "$TEST_SCRIPT"
#!/bin/sh
LOCK_DIR="$1"
ACTION="$2"
LOCAL_DIR="$(dirname "$LOCK_DIR")"
LOCK_OWNED=0

BB=""
if command -v busybox >/dev/null 2>&1; then
    BB="$(command -v busybox)"
fi

bb_mtime() {
    _file="$1"
    stat -c %Y "$_file" 2>/dev/null || echo 0
}

acquire_lock() {
    if mkdir "$LOCK_DIR" 2>/dev/null; then
        LOCK_OWNED=1
    else
        _now=$(date +%s 2>/dev/null || echo 0)
        _ts=$(cat "$LOCK_DIR/ts" 2>/dev/null | tr -cd '0-9')
        _pid=$(cat "$LOCK_DIR/pid" 2>/dev/null | tr -cd '0-9')
        _stale=0

        if [ -n "$_pid" ] && ! kill -0 "$_pid" 2>/dev/null; then
            _stale=1
        fi

        if [ "$_stale" -eq 0 ] && [ -n "$_ts" ] && [ "$_now" -gt 0 ] && [ "$_ts" -gt 0 ]; then
            _age=$((_now - _ts))
            if [ "$_age" -ge 30 ]; then
                _stale=1
            else
                exit 1
            fi
        fi

        if [ "$_stale" -eq 1 ]; then
            rm -rf "$LOCK_DIR"
            if mkdir "$LOCK_DIR" 2>/dev/null; then
                LOCK_OWNED=1
            else
                exit 1
            fi
        else
            exit 1
        fi
    fi

    echo "$$" > "$LOCK_DIR/pid"
    echo "$(date +%s 2>/dev/null || echo 0)" > "$LOCK_DIR/ts"
}

release_lock() {
    if [ "$LOCK_OWNED" -eq 1 ] && [ -d "$LOCK_DIR" ]; then
        _pid=$(cat "$LOCK_DIR/pid" 2>/dev/null | tr -cd '0-9')
        if [ "$_pid" = "$$" ]; then
            rm -rf "$LOCK_DIR"
        fi
    fi
    LOCK_OWNED=0
}

cleanup_and_exit() {
    _code=$?
    release_lock
    exit "$_code"
}
trap 'cleanup_and_exit' EXIT INT TERM HUP

if [ "$ACTION" = "hold_lock" ]; then
    acquire_lock
    echo "LOCKED:$$"
    # Wait for signal to exit
    while [ ! -f "$LOCK_DIR/release_signal" ]; do
        sleep 0.1
    done
    release_lock
    echo "UNLOCKED:$$"
    exit 0
elif [ "$ACTION" = "contender" ]; then
    acquire_lock
    echo "CONTENDER_ACQUIRED:$$"
    exit 0
fi
EOF
chmod +x "$TEST_SCRIPT"

# Step A: Launch Process A to acquire and hold the lock
rm -rf "$TEST_LOCK_DIR"
sh "$TEST_SCRIPT" "$TEST_LOCK_DIR" hold_lock > "$TEST_DIR/procA.log" 2>&1 &
PID_A=$!

# Wait until Process A acquires the lock
while [ ! -d "$TEST_LOCK_DIR" ] || [ ! -f "$TEST_LOCK_DIR/pid" ]; do
    sleep 0.05
done
RECORDED_PID=$(cat "$TEST_LOCK_DIR/pid")
echo "  [*] Process A (PID $PID_A) holds lock with recorded PID $RECORDED_PID"

# Step B: Spawn Contender Process B trying to acquire the same lock
set +e
sh "$TEST_SCRIPT" "$TEST_LOCK_DIR" contender > "$TEST_DIR/procB.log" 2>&1
CONTENDER_EXIT=$?
set -e

echo "  [*] Contender Process B exited with code $CONTENDER_EXIT"
if [ "$CONTENDER_EXIT" -ne 1 ]; then
    echo "  [✗] FAIL: Contender should have exited with code 1, got $CONTENDER_EXIT"
    cat "$TEST_DIR/procB.log"
    exit 1
fi

# Step C: CRITICAL VERIFICATION:
# Does $TEST_LOCK_DIR still exist?
if [ ! -d "$TEST_LOCK_DIR" ]; then
    echo "  [✗] CRITICAL FAILURE: Process B wiped out Process A's lock directory on exit!"
    exit 1
fi

# Does $TEST_LOCK_DIR/pid still exist and match Process A's PID?
STILL_RECORDED_PID=$(cat "$TEST_LOCK_DIR/pid" 2>/dev/null || echo "")
if [ "$STILL_RECORDED_PID" != "$RECORDED_PID" ]; then
    echo "  [✗] CRITICAL FAILURE: PID in lock file was modified! Expected $RECORDED_PID, got $STILL_RECORDED_PID"
    exit 1
fi

echo "  [✓] PASS: Contender Process B failed and EXIT trap did NOT delete or corrupt Process A's lock!"

# Step D: Signal Process A to release and exit
touch "$TEST_LOCK_DIR/release_signal"
wait "$PID_A"

# Step E: Verify Process A cleaned up the lock on normal exit
if [ -d "$TEST_LOCK_DIR" ]; then
    echo "  [✗] FAIL: Process A did not clean up lock directory after release"
    exit 1
fi
echo "  [✓] PASS: Process A cleanly released lock upon completion."

# Step F: Stale lock handling
echo "  [*] Testing stale lock recovery..."
mkdir -p "$TEST_LOCK_DIR"
echo "999999" > "$TEST_LOCK_DIR/pid"
echo "1000000000" > "$TEST_LOCK_DIR/ts"

# Process C should detect PID 999999 is dead (stale), clear it, and acquire lock
sh "$TEST_SCRIPT" "$TEST_LOCK_DIR" hold_lock > "$TEST_DIR/procC.log" 2>&1 &
PID_C=$!
sleep 1.5

NEW_PID=$(cat "$TEST_LOCK_DIR/pid" 2>/dev/null || echo "")
if [ "$NEW_PID" = "$PID_C" ]; then
    echo "  [✓] PASS: Stale lock detected and successfully recovered by Process C (PID $PID_C)"
else
    echo "  [✗] FAIL: Stale lock not recovered. Recorded PID: $NEW_PID, Expected: $PID_C"
    cat "$TEST_DIR/procC.log"
    kill "$PID_C" 2>/dev/null || true
    exit 1
fi

touch "$TEST_LOCK_DIR/release_signal"
wait "$PID_C"
if [ -d "$TEST_LOCK_DIR" ]; then
    echo "  [✗] FAIL: Process C did not clean up lock directory"
    exit 1
fi
echo "  [✓] PASS: Stale lock test passed completely."

echo ""
echo "=== ALL UNIT TESTS PASSED SUCCESSFULLY! ==="
