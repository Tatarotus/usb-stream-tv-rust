#!/bin/sh
# Start the mixed VOD bridge on the VPS with the PIN loaded from the protected
# environment file. The PIN is exported only to the child process environment:
# it never appears on a command line, in this script's argv, or in any log.
#
# Every precondition is verified before the proxy is started:
#   * the script, allowlist and token exist under the real VPS paths;
#   * the upstream origin is HTTPS or loopback HTTP (the proxy rejects a plain
#     HTTP origin on any other host);
#   * the first allowlisted task can be read from the origin with the exact
#     size/revision headers, proving the PIN and the content identity wiring.
#
# Usage (VPS):
#   APP_DIR=/opt/containers/apps/usb-stream-tv \
#   PKG_DIR=$APP_DIR/mixed-vod PORT=18191 \
#   UPSTREAM=http://127.0.0.1:8080 \
#   sh scripts/start_mixed_vod_proxy_vps.sh
set -eu
umask 077

APP_DIR=${APP_DIR:-/opt/containers/apps/usb-stream-tv}
ENV_FILE=${ENV_FILE:-$APP_DIR/.env}
PKG_DIR=${PKG_DIR:-$APP_DIR/mixed-vod}
PORT=${PORT:-18191}
UPSTREAM=${UPSTREAM:-http://127.0.0.1:8080}
PYTHON=${PYTHON:-python3}
CURL=${CURL:-curl}

die() { echo "[vod-proxy] $*" >&2; exit 1; }

[ -f "$APP_DIR/scripts/run_mixed_vod_proxy.py" ] \
    || die "proxy script not found at $APP_DIR/scripts/run_mixed_vod_proxy.py (confirme o caminho real na VPS)"
[ -f "$PKG_DIR/vod_allowlist.json" ] || die "allowlist not found: $PKG_DIR/vod_allowlist.json"
[ -f "$PKG_DIR/transport.token" ] || die "transport token not found: $PKG_DIR/transport.token"
[ -f "$ENV_FILE" ] || die "env file not found: $ENV_FILE"
command -v "$PYTHON" >/dev/null 2>&1 || die "$PYTHON not found"
command -v "$CURL" >/dev/null 2>&1 || die "$CURL not found"

TV_PIN=$(sed -n 's/^TV_PIN=//p' "$ENV_FILE" | head -n 1 | tr -d '\r' \
    | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
[ -n "$TV_PIN" ] || die "TV_PIN is not configured in $ENV_FILE"
export TV_PIN

_token_bytes=$(wc -c < "$PKG_DIR/transport.token" | tr -d ' ')
[ "$_token_bytes" -ge 32 ] || die "transport token is shorter than 32 bytes"

case "$UPSTREAM" in
    https://*) ;;
    http://127.0.0.1:*|http://127.0.0.1) ;;
    http://localhost:*|http://localhost) ;;
    *) die "upstream must be HTTPS or loopback HTTP (plain HTTP to another host is refused)" ;;
esac

_task=$(sed -n 's/.*"task_id"[[:space:]]*:[[:space:]]*"\([0-9a-f]\{8\}\)".*/\1/p' \
    "$PKG_DIR/vod_allowlist.json" | head -n 1)
_revision=$(sed -n 's/.*"revision"[[:space:]]*:[[:space:]]*"\([a-z0-9:]\{1,\}\)".*/\1/p' \
    "$PKG_DIR/vod_allowlist.json" | head -n 1)
[ -n "$_task" ] && [ -n "$_revision" ] \
    || die "allowlist has no verifiable task (task_id + revision required)"

_headers=$(mktemp "${TMPDIR:-/tmp}/vod-proxy-headers.XXXXXX")
trap 'rm -f "$_headers"' EXIT INT TERM
printf 'X-Auth-PIN: %s\nX-Vod-Revision: %s\nRange: bytes=0-63\n' \
    "$TV_PIN" "$_revision" > "$_headers"
chmod 600 "$_headers"

_code=$("$CURL" -sS -o /dev/null -w '%{http_code}' --max-time 30 \
    -H "@$_headers" "$UPSTREAM/api/internal/vod-range/$_task" 2>/dev/null || true)
rm -f "$_headers"
trap - EXIT INT TERM
[ "$_code" = "206" ] \
    || die "origin did not serve the real VOD range (HTTP ${_code:-none}); verifique a porta publicada, o PIN e a revisão do conteúdo"

echo "[vod-proxy] origin verified (206 real range on the first allowlisted task); starting proxy on 127.0.0.1:$PORT"
exec "$PYTHON" "$APP_DIR/scripts/run_mixed_vod_proxy.py" \
    --port "$PORT" \
    --allowlist "$PKG_DIR/vod_allowlist.json" \
    --token-file "$PKG_DIR/transport.token" \
    --upstream "$UPSTREAM"
