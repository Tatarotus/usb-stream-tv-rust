#!/usr/bin/env bash
# deploy_tablet.sh — Automação de deploy dos binários e scripts no tablet (SM-T110)
set -euo pipefail

ADB_TARGET="${1:-127.0.0.1:25555}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "============================================================"
echo " Deploying USB-Stream-TV to Tablet (${ADB_TARGET})"
echo "============================================================"

# 1. Verificar conexão ADB
echo "[1/6] Verificando conexão ADB..."
adb connect "$ADB_TARGET" || true
adb -s "$ADB_TARGET" wait-for-device

# 2. Criar diretórios de trabalho e parar serviços ativos antes de modificar arquivos
echo "[2/6] Preparando tablet e garantindo que serviços anteriores estejam parados..."
adb -s "$ADB_TARGET" shell "su -c '
    mkdir -p /data/local/tmp/ntfs_lab/mnt
    echo \"\" > /sys/class/android_usb/android0/f_mass_storage/lun0/file 2>/dev/null || true
    killall -9 fuse_ntfs stream_fetcher 2>/dev/null || true
    sleep 1
'"

# 3. Transferir template NTFS 128 GiB se não existir ou se for versão antiga de 8 GB
echo "[3/6] Verificando template NTFS de 128 GiB no tablet..."
TEMPLATE_SIZE=$(adb -s "$ADB_TARGET" shell "stat -c %s /data/local/tmp/ntfs_lab/ntfs_template.bin 2>/dev/null || echo 0" | tr -d '\r\n ')
if [ "$TEMPLATE_SIZE" != "137438953472" ]; then
    echo "       Instalando template NTFS de 128 GiB (ext4 thin-provisioned ~69MB)..."
    adb -s "$ADB_TARGET" push "$ROOT_DIR/src/tools/sparse_unpack_arm32" /data/local/tmp/sparse_unpack
    adb -s "$ADB_TARGET" shell "chmod 755 /data/local/tmp/sparse_unpack"
    adb -s "$ADB_TARGET" push "$ROOT_DIR/templates/ntfs_template.sparse.gz" /data/local/tmp/ntfs_lab/
    adb -s "$ADB_TARGET" shell "su -c '
        busybox gunzip -c /data/local/tmp/ntfs_lab/ntfs_template.sparse.gz | /data/local/tmp/sparse_unpack /data/local/tmp/ntfs_lab/ntfs_template.bin &&
        rm -f /data/local/tmp/ntfs_lab/ntfs_template.sparse.gz /data/local/tmp/sparse_unpack
    '"
else
    echo "       Template NTFS 128 GiB já presente e atualizado."
fi

# 4. Transferir ferramentas para /data/local/tmp
echo "[4/6] Enviando utilitário patch_trp..."
adb -s "$ADB_TARGET" push "$ROOT_DIR/src/tools/patch_trp_arm32" /data/local/tmp/patch_trp
adb -s "$ADB_TARGET" shell "chmod 755 /data/local/tmp/patch_trp"

# 5. Instalar binários e scripts em /system/xbin (Root)
echo "[5/6] Instalando executáveis e scripts em /system/xbin/..."
adb -s "$ADB_TARGET" push "$ROOT_DIR/src/ntfs/fuse_ntfs_arm32" /data/local/tmp/fuse_ntfs
adb -s "$ADB_TARGET" push "$ROOT_DIR/src/client/stream_fetcher_arm32" /data/local/tmp/stream_fetcher
adb -s "$ADB_TARGET" push "$ROOT_DIR/src/client/fuse_direct_arm32" /data/local/tmp/fuse_direct

for s in switch_tv_mode.sh switch_live.sh switch_vod.sh reconnect_usb.sh tv_watchdog.sh install-recovery-2.sh; do
    adb -s "$ADB_TARGET" push "$SCRIPT_DIR/$s" "/data/local/tmp/$s"
done

adb -s "$ADB_TARGET" shell "su -c '
    mount -o remount,rw /system || exit 1
    rm -f /system/xbin/fuse_ntfs /system/xbin/stream_fetcher /system/xbin/patch_trp /system/xbin/fuse_direct
    cp /data/local/tmp/fuse_ntfs /system/xbin/fuse_ntfs
    cp /data/local/tmp/stream_fetcher /system/xbin/stream_fetcher
    cp /data/local/tmp/patch_trp /system/xbin/patch_trp
    cp /data/local/tmp/fuse_direct /system/xbin/fuse_direct
    chmod 755 /system/xbin/fuse_ntfs /system/xbin/stream_fetcher /system/xbin/patch_trp /system/xbin/fuse_direct
    for f in switch_tv_mode.sh switch_live.sh switch_vod.sh reconnect_usb.sh tv_watchdog.sh; do
        cp /data/local/tmp/\$f /system/xbin/\$f
        chmod 755 /system/xbin/\$f
    done
    cp /system/xbin/switch_live.sh /data/local/tmp/switch_live.sh
    chmod 755 /data/local/tmp/switch_live.sh
    if [ -f /data/local/tmp/install-recovery-2.sh ]; then
        cp /data/local/tmp/install-recovery-2.sh /system/etc/install-recovery-2.sh
        chmod 755 /system/etc/install-recovery-2.sh
    fi
    mount -o remount,ro /system
'"

if [ -f "$ROOT_DIR/.env" ]; then
    TOKEN_VAL=$(grep -E '^TABLET_TOKEN=' "$ROOT_DIR/.env" | cut -d'=' -f2- | tr -d '"'\'' ' || true)
    if [ -n "$TOKEN_VAL" ]; then
        echo "       Configurando tablet.token..."
        adb -s "$ADB_TARGET" shell "su -c 'echo -n \"$TOKEN_VAL\" > /data/local/tmp/tablet.token && chmod 600 /data/local/tmp/tablet.token'"
    fi
fi

# 6. Reiniciar o serviço de TV Ao Vivo
echo "[6/6] Reiniciando serviço Live TV no tablet..."
adb -s "$ADB_TARGET" shell "su -c '/system/xbin/switch_tv_mode.sh live'"

echo "============================================================"
echo " [✓] Deploy no tablet concluído com sucesso!"
echo "============================================================"
