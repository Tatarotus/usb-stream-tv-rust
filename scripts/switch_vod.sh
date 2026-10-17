#!/system/bin/sh
# switch_vod.sh — Alternador para Modo VOD (Cloud Virtual Remote Disk)
# Encaminha para o motor unificado e atomico em switch_tv_mode.sh
trap '' HUP

TASK_ID="$1"
if [ -z "$TASK_ID" ]; then
    echo "Uso: switch_vod.sh <task_id> [host_or_ip]"
    exit 1
fi

if [ -x "/system/xbin/switch_tv_mode.sh" ]; then
    exec /system/xbin/switch_tv_mode.sh vod "$@"
elif [ -x "/data/local/tmp/switch_tv_mode.sh" ]; then
    exec /data/local/tmp/switch_tv_mode.sh vod "$@"
else
    echo "[!] switch_tv_mode.sh nao encontrado!"
    exit 1
fi
