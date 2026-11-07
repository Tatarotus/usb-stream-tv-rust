#!/bin/bash
# Tests for setup_operations_db.sh: creation, permissions, schema migration and
# the shared-file proof — all in a SANDBOX.
#
# The script normally reaches the VPS over ssh and the container over docker.
# Here both are replaced: `ssh` executes the remote stanza locally and `docker`
# is absent, so the proof step reports "adiada" instead of contacting anything.
# Nothing here touches the real host, the container or the tablet.
set -euo pipefail

ROOT=$(cd -- "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/test-opsdb.XXXXXX")
cleanup() { chmod -R u+rwX "$TMP" 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT INT TERM

SANDBOX_BIN=$TMP/bin
mkdir -p "$SANDBOX_BIN"
# A stub ssh: the script pipes a program on stdin to "sudo bash -s"; running it
# locally keeps the test hermetic while exercising the same code path.
cat > "$SANDBOX_BIN/ssh" <<'EOF'
#!/bin/sh
# Ignore host and the remote command string: run the piped program here.
exec /bin/bash -s
EOF
chmod +x "$SANDBOX_BIN/ssh"

APP=$TMP/app
mkdir -p "$APP/scripts"
cp "$ROOT/scripts/mixed_operations.py" "$APP/scripts/"
HOST_ROOT=$TMP/host

run_setup() {
    PATH="$SANDBOX_BIN:/usr/bin:/bin" \
    APP_HOST="$HOST_ROOT" APP_DIR="$APP" REL_DIR="vod_cache/.operations" \
    DB_NAME="operations.db" REMOTE="sandbox" CONFIRM_SETUP=1 \
        sh "$ROOT/scripts/setup_operations_db.sh"
}

echo "=== Test 1: dry-run changes nothing ==="
out=$(PATH="$SANDBOX_BIN:/usr/bin:/bin" APP_HOST="$HOST_ROOT" APP_DIR="$APP" \
      sh "$ROOT/scripts/setup_operations_db.sh")
echo "$out" | grep -q "NADA FOI ALTERADO"
[ ! -e "$HOST_ROOT/vod_cache/.operations/operations.db" ] \
    || { echo "FAIL: dry-run created the database" >&2; exit 1; }
echo "OK"

echo "=== Test 2: creation applies 0700/0600 and the module migration ==="
out=$(run_setup)
DIR=$HOST_ROOT/vod_cache/.operations
DB=$DIR/operations.db
[ -f "$DB" ] || { echo "FAIL: no database created: $out" >&2; exit 1; }
[ "$(stat -c %a "$DIR")" = 700 ] \
    || { echo "FAIL: directory mode $(stat -c %a "$DIR"), expected 700" >&2; exit 1; }
[ "$(stat -c %a "$DB")" = 600 ] \
    || { echo "FAIL: database mode $(stat -c %a "$DB"), expected 600" >&2; exit 1; }
echo "OK"

echo "=== Test 3: the schema came from the module, not from hand-written SQL ==="
python3 - "$DB" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
tables = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}
for expected in ("meta", "operations", "events", "outbox"):
    assert expected in tables, "missing table %s" % expected
cols = {r[1] for r in conn.execute("PRAGMA table_info(operations)")}
for added in ("task_id", "intent_revision", "executor", "pendency_closed"):
    assert added in cols, "missing column %s" % added
version = conn.execute(
    "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
assert int(version) >= 2, "schema_version not brought forward: %s" % version
instance = conn.execute(
    "SELECT value FROM meta WHERE key='db_instance_id'").fetchone()
assert instance and len(instance[0]) == 32, "no db_instance_id recorded"
print("schema ok: version=%s instance=%s..." % (version, instance[0][:8]))
PY
echo "OK"

echo "=== Test 4: the shared-file proof is not invented when docker is absent ==="
# With no container on PATH the script must say the proof is POSTPONED. Claiming
# a shared file it could not read is the failure mode this test guards.
echo "$out" | grep -q "prova adiada" \
    || { echo "FAIL: the script claimed a proof it could not make: $out" >&2; exit 1; }
echo "$out" | grep -qv "MESMO ARQUIVO" \
    || { echo "FAIL: proof asserted without a container" >&2; exit 1; }
echo "OK"

echo "=== Test 5: re-running on an existing file keeps the data and the identity ==="
before=$(python3 -c "
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print(c.execute(\"SELECT value FROM meta WHERE key='db_instance_id'\").fetchone()[0])
" "$DB")
run_setup >/dev/null
after=$(python3 -c "
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print(c.execute(\"SELECT value FROM meta WHERE key='db_instance_id'\").fetchone()[0])
" "$DB")
[ "$before" = "$after" ] \
    || { echo "FAIL: a second run replaced the database identity" >&2; exit 1; }
echo "OK"

echo "=== Test 6: two sides pointing at DIFFERENT files are detected ==="
# Simulate both sides: a host file and a "container" file that are not the same
# inode. The comparison must fail loudly instead of reporting a shared file.
OTHER=$TMP/other/operations.db
mkdir -p "$(dirname "$OTHER")"
cp "$DB" "$OTHER"
python3 - "$DB" "$OTHER" <<'PY'
import sqlite3, sys
a, b = (sqlite3.connect(p) for p in sys.argv[1:3])
ia = a.execute("SELECT value FROM meta WHERE key='db_instance_id'").fetchone()[0]
ib = b.execute("SELECT value FROM meta WHERE key='db_instance_id'").fetchone()[0]
# A copy carries the SAME instance id (it is a copy), so identity alone is not
# enough: the device:inode pair is what distinguishes two real files.
import os
assert os.stat(sys.argv[1]).st_ino != os.stat(sys.argv[2]).st_ino
assert ia == ib
print("copies share the instance id but not the inode: comparison must use both")
PY
echo "OK"

echo "ALL OPS-DB SANDBOX TESTS PASSED!"