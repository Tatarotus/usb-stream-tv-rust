#!/bin/bash
# sync_youtube_cookies.sh — Extrai cookies do Firefox local e envia para a VPS
set -e

SRC_DIR="$HOME/.mozilla/firefox"
COOKIE_DB=$(find "$SRC_DIR" -name "cookies.sqlite" 2>/dev/null | head -n 1)

if [ -z "$COOKIE_DB" ] || [ ! -f "$COOKIE_DB" ]; then
    echo "[!] Banco cookies.sqlite do Firefox não encontrado."
    exit 1
fi

TMP_DIR="/tmp/yt_cookies_sync"
mkdir -p "$TMP_DIR"
cp "$COOKIE_DB" "$TMP_DIR/cookies.sqlite"

python3 -c '
import sqlite3, os

src = "/tmp/yt_cookies_sync/cookies.sqlite"
dst = "/tmp/yt_cookies_sync/youtube_cookies.txt"

essential_names = {
    "LOGIN_INFO", "SID", "__Secure-1PSID", "__Secure-3PSID", "HSID", "SSID", 
    "APISID", "SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID", 
    "__Secure-1PSIDTS", "__Secure-3PSIDTS", "SIDCC", "__Secure-1PSIDCC", 
    "__Secure-3PSIDCC", "PREF", "VISITOR_INFO1_LIVE", "YSC", "GPS",
    "__Secure-ROLLOUT_TOKEN", "__Secure-BUCKET"
}

con = sqlite3.connect(src)
cur = con.cursor()
cur.execute("SELECT host, name, value, path, expiry, isSecure, isHttpOnly FROM moz_cookies WHERE host LIKE \"%youtube.com%\"")

with open(dst, "w") as f:
    f.write("# Netscape HTTP Cookie File\n")
    count = 0
    for host, name, value, path, expiry, isSecure, isHttpOnly in cur.fetchall():
        if name in essential_names:
            subdomain_flag = "TRUE" if host.startswith(".") else "FALSE"
            secure_flag = "TRUE" if isSecure else "FALSE"
            f.write(f"{host}\t{subdomain_flag}\t{path}\t{secure_flag}\t{expiry}\t{name}\t{value}\n")
            count += 1
print(f"[✓] {count} cookies essenciais do YouTube extraídos.")
'

scp -q "$TMP_DIR/youtube_cookies.txt" oracle:/opt/containers/apps/usb-stream-tv/vod_cache/youtube_cookies.txt
ssh oracle "chmod 644 /opt/containers/apps/usb-stream-tv/vod_cache/youtube_cookies.txt"
rm -rf "$TMP_DIR"

echo "[✓] Cookies atualizados com sucesso na Oracle VPS!"
