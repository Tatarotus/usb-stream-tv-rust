#!/bin/bash
# Tests for scripts/start_mixed_vod_proxy_vps.sh: real VPS paths, credential
# handling (never on a command line), upstream validation and the proof that
# the origin serves the real allowlisted range before the proxy starts.
set -euo pipefail

ROOT=$(cd -- "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/test-vod-proxy-vps.XXXXXX")
trap 'rm -rf "$TMP"' EXIT INT TERM

APP=$TMP/app
PKG=$TMP/mixed-vod
BIN=$TMP/bin
mkdir -p "$APP/scripts" "$PKG" "$BIN"
cp "$ROOT/scripts/run_mixed_vod_proxy.py" "$APP/scripts/"
echo 'TV_PIN=p-1234-secret' > "$APP/.env"
printf '{"tasks": [{"task_id": "a1b2c3d4", "size": 1000, "revision": "md5:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]}\n' > "$PKG/vod_allowlist.json"
printf 'token-value-0123456789abcdef0123456789abcdef\n' > "$PKG/transport.token"

# Shims: record argv so the test can prove no credential leaks into it.
cat > "$BIN/curl" <<EOF
#!/bin/bash
printf '%s\n' "\$*" >> "$TMP/curl.args"
cat "\$TMP/curl.headers" 2>/dev/null | while read -r line; do printf '%s\n' "\$line" >> "$TMP/curl.headers.seen"; done
for a in "\$@"; do case "\$a" in @*) cat "\${a#@}" >> "$TMP/curl.headers.seen" ;; esac; done
printf '%s' "\${MOCK_CODE:-206}"
EOF
cat > "$BIN/python3" <<EOF
#!/bin/bash
printf '%s\n' "\$*" >> "$TMP/python.args"
env | grep '^TV_PIN=' >> "$TMP/python.env" 2>/dev/null || true
exit 0
EOF
chmod +x "$BIN/curl" "$BIN/python3"
export PATH="$BIN:$PATH"

run() {
    : > "$TMP/curl.args"; : > "$TMP/python.args"; : > "$TMP/curl.headers.seen"; : > "$TMP/python.env"
    env APP_DIR="$APP" PKG_DIR="$PKG" ENV_FILE="$APP/.env" PORT=18191 \
        UPSTREAM="${UPSTREAM:-http://127.0.0.1:8080}" MOCK_CODE="${MOCK_CODE:-206}" \
        PATH="$BIN:$PATH" sh "$ROOT/scripts/start_mixed_vod_proxy_vps.sh"
}

echo "=== Test 1: origin proven, proxy exec'd, PIN only in the environment ==="
run > "$TMP/out.log" 2>&1
grep -q "origin verified" "$TMP/out.log"
grep -q -- "--allowlist $PKG/vod_allowlist.json" "$TMP/python.args"
grep -q -- "--token-file $PKG/transport.token" "$TMP/python.args"
grep -q -- "--upstream http://127.0.0.1:8080" "$TMP/python.args"
# The PIN must reach the child only through the environment.
grep -q "^TV_PIN=p-1234-secret$" "$TMP/python.env"
! grep -q "p-1234-secret" "$TMP/python.args"
! grep -q "p-1234-secret" "$TMP/curl.args"
# The PIN and revision were sent to the origin from a protected header file.
grep -q "X-Auth-PIN: p-1234-secret" "$TMP/curl.headers.seen"
grep -q "X-Vod-Revision: md5:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" "$TMP/curl.headers.seen"
# The header file must not survive.
! ls "$TMP"/vod-proxy-headers.* >/dev/null 2>&1 || true

echo "=== Test 2: non-loopback plain HTTP upstream is refused ==="
set +e
UPSTREAM=http://172.20.0.5:8080 run > "$TMP/out.log" 2>&1
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "plain HTTP to another host is refused" "$TMP/out.log"
[ ! -s "$TMP/python.args" ]

echo "=== Test 3: a listener that cannot serve real bytes aborts the start ==="
set +e
MOCK_CODE=503 run > "$TMP/out.log" 2>&1
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "did not serve the real VOD range" "$TMP/out.log"
[ ! -s "$TMP/python.args" ]

echo "=== Test 4: missing TV_PIN aborts without touching anything ==="
echo 'PORT=8080' > "$APP/.env"
set +e
run > "$TMP/out.log" 2>&1
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "TV_PIN is not configured" "$TMP/out.log"
[ ! -s "$TMP/python.args" ]

echo "=== Test 5: missing allowlist/script aborts with a clear message ==="
echo 'TV_PIN=ok' > "$APP/.env"
rm -f "$PKG/vod_allowlist.json"
set +e
run > "$TMP/out.log" 2>&1
rc=$?
set -e
[ "$rc" -ne 0 ]
grep -q "allowlist not found" "$TMP/out.log"

echo "ALL VPS VOD PROXY LAUNCHER TESTS PASSED!"
