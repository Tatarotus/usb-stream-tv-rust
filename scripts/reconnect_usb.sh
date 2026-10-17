#!/system/bin/sh
# reconnect_usb.sh - Ciclo manual de desconexao e reconexao USB para TV Samsung
# Compativel com SM-T110 (sysfs legado) e Xiaomi Mi A2 (configfs)

WAIT_SEC="${1:-1}"

echo "[*] Disparando reconexao USB (espera=${WAIT_SEC}s)..."

if [ -d "/sys/class/android_usb/android0" ]; then
    # SM-T110 (sysfs legado)
    CUR_INQ=$(cat /sys/class/android_usb/android0/f_mass_storage/inquiry_string 2>/dev/null)
    if [ "$CUR_INQ" = "LIVETV1" ]; then
        NEW_INQ="LIVETV2"
    else
        NEW_INQ="LIVETV1"
    fi

    # Detecta backing image e modo ativo
    if [ -f "/data/local/tmp/favorites_mode.flag" ]; then
        IMG="/data/local/tmp/ntfs_lab/mnt/tv_stream.img"
        INQ="CHANNELS"
        RO=0
    elif [ -f "/data/local/tmp/vod_mode.flag" ]; then
        IMG="/data/local/tmp/vfat_mnt/tv_stream.img"
        INQ="CINEMA"
        RO=1
    elif [ -f "/data/local/tmp/ntfs_lab/mnt/tv_stream.img" ]; then
        IMG="/data/local/tmp/ntfs_lab/mnt/tv_stream.img"
        INQ="$NEW_INQ"
        RO=0
    else
        IMG="/data/local/tmp/vfat_mnt/tv_stream.img"
        INQ="$NEW_INQ"
        RO=0
    fi

    # Signal FUSE to flush old buffer and randomize Volume Serial
    pkill -USR1 -f fuse_ntfs 2>/dev/null || touch /data/local/tmp/fuse_flush

    # Execute USB disconnect
    echo 0 > /sys/class/android_usb/android0/enable 2>/dev/null || true
    echo "" > /sys/class/android_usb/android0/f_mass_storage/lun0/file 2>/dev/null || true
    sleep "$WAIT_SEC"

    # Alternar nome do arquivo NTFS para forcar a TV a descartar cache de ponto de reproducao (com debounce de 4s)
    DEBOUNCE_FILE="/data/local/tmp/patch_trp.debounce"
    NOW=$(date +%s 2>/dev/null || busybox date +%s 2>/dev/null || echo 0)
    LAST=$(cat "$DEBOUNCE_FILE" 2>/dev/null || echo 0)
    DIFF=$((NOW - LAST))
    if [ "$DIFF" -ge 4 ] || [ "$LAST" -eq 0 ]; then
        echo "$NOW" > "$DEBOUNCE_FILE"
        if [ -x "/data/local/tmp/patch_trp" ] && [ -f "/data/local/tmp/ntfs_lab/ntfs_template.bin" ]; then
            /data/local/tmp/patch_trp /data/local/tmp/ntfs_lab/ntfs_template.bin >/dev/null 2>&1 || true
        fi
    else
        echo "[*] Debounce ativo ($DIFF seg desde ultima alternancia). Mantendo arquivo atual."
    fi

    # Execute USB reconnect with new inquiry string
    echo "$INQ" > /sys/class/android_usb/android0/f_mass_storage/inquiry_string 2>/dev/null || true
    echo "$RO" > /sys/class/android_usb/android0/f_mass_storage/lun0/ro 2>/dev/null || true
    echo "$IMG" > /sys/class/android_usb/android0/f_mass_storage/lun0/file 2>/dev/null || true
    echo 1 > /sys/class/android_usb/android0/enable 2>/dev/null || true
elif [ -d "/config/usb_gadget/g1" ] || [ -d "/sys/kernel/config/usb_gadget/g1" ]; then
    # Xiaomi Mi A2 (configfs)
    GADGET="/config/usb_gadget/g1"
    [ ! -d "$GADGET" ] && GADGET="/sys/kernel/config/usb_gadget/g1"
    UDC=$(getprop sys.usb.controller)
    [ -z "$UDC" ] && UDC="a800000.dwc3"
    echo "" > "$GADGET/UDC" 2>/dev/null || true
    sleep 1
    echo "$UDC" > "$GADGET/UDC" 2>/dev/null || true
fi

echo "[✓] USB reconectado com sucesso!"
