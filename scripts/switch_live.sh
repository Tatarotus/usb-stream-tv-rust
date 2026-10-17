#!/system/bin/sh
# switch_live.sh — Retorna do Modo VOD para a TV Ao Vivo
# Encaminha para o motor unificado e atomico em switch_tv_mode.sh
trap '' HUP

if [ -x "/system/xbin/switch_tv_mode.sh" ]; then
    exec /system/xbin/switch_tv_mode.sh live "$@"
elif [ -x "/data/local/tmp/switch_tv_mode.sh" ]; then
    exec /data/local/tmp/switch_tv_mode.sh live "$@"
else
    echo "[!] switch_tv_mode.sh nao encontrado!"
    exit 1
fi
