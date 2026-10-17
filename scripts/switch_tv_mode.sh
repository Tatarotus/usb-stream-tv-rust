#!/system/xbin/busybox sh
# switch_tv_mode.sh — Alternador unificado e atomico de modos da TV
# Suporta: live (TV Ao Vivo NTFS), vod (Cinema FAT32 Cloud), favorites (Multi-Canais NTFS)
# Protegido contra set -e orfao, TOCTOU e backpressure do Host MStar
trap '' HUP

MODE="$1"
BASE="/data/local/tmp"
LOCK_DIR="$BASE/mode_switch.lock"
WD_LOCK="$BASE/tv_watchdog.lock"
WD_SCRIPT="/system/xbin/tv_watchdog.sh"
[ ! -x "$WD_SCRIPT" ] && WD_SCRIPT="$BASE/tv_watchdog.sh"
WD_LOG="$BASE/tv_watchdog.log"

MNT="$BASE/ntfs_lab/mnt"
FIFO="$BASE/live_pipe"
LIVE_TMPL="$BASE/ntfs_lab/ntfs_template.bin"
FAV_TMPL="$BASE/ntfs_lab/favorites_template.bin"
BACKING_IMG="$MNT/tv_stream.img"
FLAG_FILE="$BASE/vod_mode.flag"
VOD_FLAG="$FLAG_FILE"
FAV_FLAG="$BASE/favorites_mode.flag"
LOG="$BASE/ntfs_lab/fuse_ntfs.log"

FUSE_BIN="/system/xbin/fuse_ntfs"
[ ! -x "$FUSE_BIN" ] && FUSE_BIN="$BASE/fuse_ntfs"

pid_is() {   # $1=pid  $2=trecho esperado em /proc/PID/cmdline
    [ -n "$1" ] && kill -0 "$1" 2>/dev/null && /system/xbin/busybox grep -q -E "$2" "/proc/$1/cmdline" 2>/dev/null
}

ensure_watchdog() {
    wp=$(cat "$WD_LOCK/pid" 2>/dev/null || true)
    if pid_is "$wp" tv_watchdog; then
        return 0
    fi
    if [ ! -f "$WD_SCRIPT" ]; then
        echo "[!] $WD_SCRIPT nao encontrado - watchdog NAO iniciado"
        return 1
    fi
    echo "[!] tv_watchdog parado - reiniciando"
    /system/xbin/busybox setsid /system/xbin/busybox sh "$WD_SCRIPT" >> "$WD_LOG" 2>&1 &
    return 0
}

on_exit() {
    rc=$?
    rm -rf "$LOCK_DIR"
    ensure_watchdog
    exit $rc
}

# CRÍTICO 1: Rollback defensivo para que o gadget nunca fique em enable=0 orfao
fail_rollback() {
    echo "[!] FALHA CRÍTICA em '$1' — executando rollback de seguranca do USB Gadget"
    rm -rf "$LOCK_DIR"
    if [ -f "$BACKING_IMG" ]; then
        echo "$BACKING_IMG" > /sys/class/android_usb/android0/f_mass_storage/lun0/file 2>/dev/null || true
    fi
    echo 1 > /sys/class/android_usb/android0/enable 2>/dev/null || true
    ensure_watchdog
    exit 1
}

if [ "$MODE" != "status" ]; then
    [ -f "$LOCK_DIR" ] && rm -f "$LOCK_DIR"

    # CRÍTICO 2: Lock sem destruicao de dono ativo. Aguarda ate 45s antes de abortar de forma limpa
    waited=0
    while ! mkdir "$LOCK_DIR" 2>/dev/null; do
        p=$(cat "$LOCK_DIR/pid" 2>/dev/null || true)
        if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
            waited=$((waited + 1))
            if [ "$waited" -ge 45 ]; then
                echo "[!] Troca de modo bloqueada por processo ativo ($p) ha mais de 45s. Abortando com seguranca."
                exit 3
            fi
            sleep 1
            continue
        fi
        # Processo dono nao existe mais (crash ou kill anterior): remove lock orfao
        rm -rf "$LOCK_DIR"
    done
    echo $$ > "$LOCK_DIR/pid"
    trap on_exit EXIT
    trap 'exit 143' INT TERM

    ensure_watchdog
fi

# Performance e wakelock
echo "tv_stream" > /sys/power/wake_lock 2>/dev/null || true
for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo performance > "$g" 2>/dev/null || true
done
/system/xbin/iwconfig wlan0 power off 2>/dev/null || true

# ALTO 1: Desconexao canonica do USB Gadget com tempos de guarda SCSI para o Host MStar
cleanup_stack() {
    echo "[*] Desconectando USB Gadget e limpando montagens..."
    echo 0 > /sys/class/android_usb/android0/enable 2>/dev/null || true
    sleep 0.8
    echo "" > /sys/class/android_usb/android0/f_mass_storage/lun0/file 2>/dev/null || true
    sleep 0.3

    while /system/xbin/busybox mount | grep -q "$MNT"; do
        /system/xbin/busybox umount -l "$MNT" 2>/dev/null || break
        sleep 0.2
    done
    while /system/xbin/busybox mount | grep -q "/data/local/tmp/vfat_mnt"; do
        /system/xbin/busybox umount -l "/data/local/tmp/vfat_mnt" 2>/dev/null || break
        sleep 0.2
    done

    killall -9 stream_fetcher fuse_ntfs fuse_direct fuse_direct_arm32 2>/dev/null || true
    sleep 1
}

case "$MODE" in
    vod|cinema)
        TASK_ID="$2"
        [ -z "$TASK_ID" ] && TASK_ID=$(cat "$FLAG_FILE" 2>/dev/null)
        if [ -z "$TASK_ID" ]; then
            echo "[!] Uso: $0 vod <task_id> [server_host]"
            exit 1
        fi
        SERVER_HOST="${3:-tv.smre.run.place}"

        echo "[*] ========================================="
        echo "[*]       ATIVANDO MODO CINEMA (VOD)         "
        echo "[*] Tarefa: $TASK_ID | Host: $SERVER_HOST"
        echo "[*] ========================================="
        # ALTO 2: Limpa ambas as flags para evitar estado ambiguo
        rm -f "$FLAG_FILE" "$FAV_FLAG" "$BASE/resync_usb"
        echo "$TASK_ID" > "$FLAG_FILE"

        cleanup_stack

        # 1. Baixar o template FAT32 da tarefa
        TMPL_URL="http://$SERVER_HOST/vod/$TASK_ID/template.bin"
        TMPL_LOCAL="/data/local/tmp/fat_template_vod.bin"
        echo "[*] Baixando template FAT32 de $TMPL_URL..."
        /system/xbin/busybox wget -q -T 10 -O "$TMPL_LOCAL" "$TMPL_URL" || true

        if [ ! -s "$TMPL_LOCAL" ]; then
            rm -f "$FLAG_FILE"
            fail_rollback "Download template FAT32 ($TMPL_URL)"
        fi
        echo "[✓] Template FAT32 carregado com sucesso."

        # 2. Selecionar binário FUSE Direct
        FUSE_DIRECT="/system/xbin/fuse_direct"
        [ ! -x "$FUSE_DIRECT" ] && FUSE_DIRECT="/data/local/tmp/fuse_direct_arm32"
        [ ! -x "$FUSE_DIRECT" ] && FUSE_DIRECT="/data/local/tmp/fuse_direct"

        if [ ! -x "$FUSE_DIRECT" ]; then
            rm -f "$FLAG_FILE"
            fail_rollback "Binario fuse_direct ausente"
        fi

        VOD_MNT="/data/local/tmp/vfat_mnt"
        VOD_BACKING="$VOD_MNT/tv_stream.img"
        VOD_STREAM_URL="http://$SERVER_HOST/vod/$TASK_ID/movie.mp4"

        echo "[*] Iniciando $FUSE_DIRECT em $VOD_MNT..."
        mkdir -p "$VOD_MNT"
        /system/xbin/busybox setsid "$FUSE_DIRECT" "$VOD_MNT" "$VOD_STREAM_URL" "$TMPL_LOCAL" > /data/local/tmp/fuse_vod.log 2>&1 &

        for i in $(/system/xbin/busybox seq 1 12); do
            [ -f "$VOD_BACKING" ] && break
            sleep 1
        done

        if [ ! -f "$VOD_BACKING" ]; then
            cat /data/local/tmp/fuse_vod.log 2>/dev/null || true
            fail_rollback "Montagem FUSE Direct ($VOD_BACKING)"
        fi

        # 3. Anexar LUN USB com identidade CINEMA e Read-Only com pausas SCSI
        echo "CINEMA" > /sys/class/android_usb/android0/f_mass_storage/inquiry_string 2>/dev/null || true
        sleep 0.2
        echo "" > /sys/class/android_usb/android0/f_mass_storage/lun0/file 2>/dev/null || true
        sleep 0.2
        echo 1 > /sys/class/android_usb/android0/f_mass_storage/lun0/ro 2>/dev/null || true
        sleep 0.2
        echo "$VOD_BACKING" > /sys/class/android_usb/android0/f_mass_storage/lun0/file 2>/dev/null || true
        sleep 0.3
        echo 1 > /sys/class/android_usb/android0/enable 2>/dev/null || true

        echo "[✓] Modo Cinema VOD ativo com sucesso na TV (CINEMA)!"
        ;;

    live|tv)
        echo "[*] ========================================="
        echo "[*]      RETORNANDO PARA TV AO VIVO          "
        echo "[*] ========================================="
        rm -f "$FLAG_FILE" "$FAV_FLAG" "$BASE/resync_usb"

        cleanup_stack
        rm -f "$LOG" /data/local/tmp/ntfs_lab/stream_fetcher.log

        # Recriar pipe limpo
        rm -f "$FIFO" /data/local/tmp/fetcher_heartbeat.ts /data/local/tmp/stream_fetcher.lock 2>/dev/null || true
        mkfifo "$FIFO" 2>/dev/null || true
        chmod 660 "$FIFO" 2>/dev/null || true
        chmod 666 /dev/fuse 2>/dev/null || true

        # Alternar nome do arquivo NTFS para invalidar cache da TV
        if [ -x "/data/local/tmp/patch_trp" ] && [ -f "$LIVE_TMPL" ]; then
            /data/local/tmp/patch_trp "$LIVE_TMPL" >/dev/null 2>&1 || true
        fi

        # Iniciar FUSE no modo Live TV
        mkdir -p "$MNT"
        /system/xbin/busybox setsid "$FUSE_BIN" "$MNT" "$FIFO" "$LIVE_TMPL" > "$LOG" 2>&1 &
        for i in $(/system/xbin/busybox seq 1 12); do
            [ -f "$BACKING_IMG" ] && break
            sleep 1
        done

        if [ ! -f "$BACKING_IMG" ]; then
            cat "$LOG" 2>/dev/null || true
            fail_rollback "Montagem FUSE NTFS Live"
        fi

        # Iniciar exatamente 1 stream_fetcher da Live TV
        /system/xbin/busybox setsid /system/xbin/stream_fetcher "$FIFO" tv.smre.run.place 80 > /data/local/tmp/ntfs_lab/stream_fetcher.log 2>&1 &

        # ALTO 4: Pre-buffering robusto sem crash de expressao numerica
        echo "[*] Aguardando pre-buffering de seguranca (>= 8MB)..."
        for i in $(/system/xbin/busybox seq 1 20); do
            if [ -f "$LOG" ]; then
                MB_STR=$(grep -o "S_write=[0-9]*MB" "$LOG" 2>/dev/null | tail -n 1 | sed 's/[^0-9]//g')
                MB=${MB_STR:-0}
                if [ "$MB" -ge 8 ] 2>/dev/null; then
                    echo "[*] Pre-buffer OK: ${MB}MB >= 8MB"
                    break
                fi
            fi
            sleep 1
        done

        # Reanexar LUN USB com alternancia LIVETV1/LIVETV2 com delays estritos
        CUR_INQ=$(cat /sys/class/android_usb/android0/f_mass_storage/inquiry_string 2>/dev/null || true)
        if [ "$CUR_INQ" = "LIVETV1" ]; then
            NEW_INQ="LIVETV2"
        else
            NEW_INQ="LIVETV1"
        fi
        echo "$NEW_INQ" > /sys/class/android_usb/android0/f_mass_storage/inquiry_string 2>/dev/null || true
        sleep 0.2
        echo "" > /sys/class/android_usb/android0/f_mass_storage/lun0/file 2>/dev/null || true
        sleep 0.2
        echo 0 > /sys/class/android_usb/android0/f_mass_storage/lun0/ro 2>/dev/null || true
        sleep 0.2
        echo "$BACKING_IMG" > /sys/class/android_usb/android0/f_mass_storage/lun0/file 2>/dev/null || true
        sleep 0.3
        echo 1 > /sys/class/android_usb/android0/enable 2>/dev/null || true

        echo "[✓] TV Ao Vivo restaurada com sucesso na TV ($NEW_INQ)!"
        ;;

    favorites|channels)
        echo "[*] ========================================="
        echo "[*] Switching to FAVORITES MULTI-CHANNEL Live NTFS mode..."
        echo "[*] ========================================="
        rm -f "$FLAG_FILE" "$FAV_FLAG" "$BASE/resync_usb"
        touch "$FAV_FLAG"
        cleanup_stack

        rm -f "$FIFO" /data/local/tmp/fetcher_heartbeat.ts /data/local/tmp/stream_fetcher.lock 2>/dev/null || true
        mkfifo "$FIFO" 2>/dev/null || true
        chmod 660 "$FIFO" 2>/dev/null || true
        chmod 666 /dev/fuse 2>/dev/null || true

        if [ ! -f "$FAV_TMPL" ]; then
            fail_rollback "Favorites template not found ($FAV_TMPL)"
        fi

        mkdir -p "$MNT"
        echo "[*] Launching fuse_ntfs with $FAV_TMPL..."
        /system/xbin/busybox setsid /system/xbin/fuse_ntfs "$MNT" "$FIFO" "$FAV_TMPL" > "$LOG" 2>&1 &

        for i in $(/system/xbin/busybox seq 1 12); do
            [ -f "$BACKING_IMG" ] && break
            sleep 1
        done

        if [ ! -f "$BACKING_IMG" ]; then
            cat "$LOG" 2>/dev/null || true
            fail_rollback "Montagem FUSE Favorites"
        fi

        /system/xbin/busybox setsid /system/xbin/stream_fetcher "$FIFO" tv.smre.run.place 80 > /data/local/tmp/ntfs_lab/stream_fetcher.log 2>&1 &

        echo "[*] Aguardando pre-buffering de seguranca (>= 8MB)..."
        for i in $(/system/xbin/busybox seq 1 20); do
            if [ -f "$LOG" ]; then
                MB_STR=$(grep -o "S_write=[0-9]*MB" "$LOG" 2>/dev/null | tail -n 1 | sed 's/[^0-9]//g')
                MB=${MB_STR:-0}
                if [ "$MB" -ge 8 ] 2>/dev/null; then
                    echo "[*] Pre-buffer OK: ${MB}MB >= 8MB"
                    break
                fi
            fi
            sleep 1
        done

        echo "CHANNELS" > /sys/class/android_usb/android0/f_mass_storage/inquiry_string 2>/dev/null || true
        sleep 0.2
        echo "" > /sys/class/android_usb/android0/f_mass_storage/lun0/file 2>/dev/null || true
        sleep 0.2
        echo 0 > /sys/class/android_usb/android0/f_mass_storage/lun0/ro 2>/dev/null || true
        sleep 0.2
        echo "$BACKING_IMG" > /sys/class/android_usb/android0/f_mass_storage/lun0/file 2>/dev/null || true
        sleep 0.3
        echo 1 > /sys/class/android_usb/android0/enable 2>/dev/null || true

        echo "[✓] Modo Favoritos / Multi-Canais ativo com sucesso na TV!"
        ;;

    status)
        echo "=== STATUS DO MODO TV ==="
        if [ -f "$FAV_FLAG" ]; then
            echo "[*] Modo ativo: FAVORITOS / MULTI-CANAIS ($FAV_FLAG presente)"
        elif [ -f "$FLAG_FILE" ]; then
            echo "[*] Modo ativo: CINEMA / VOD ($FLAG_FILE presente)"
        else
            echo "[*] Modo ativo: TV AO VIVO (PADRAO)"
        fi

        INQ=$(cat /sys/class/android_usb/android0/f_mass_storage/inquiry_string 2>/dev/null)
        LUN_FILE=$(cat /sys/class/android_usb/android0/f_mass_storage/lun0/file 2>/dev/null)
        LUN_EN=$(cat /sys/class/android_usb/android0/enable 2>/dev/null)
        echo "[*] USB Inquiry: ${INQ:-N/A}"
        echo "[*] Backing File: ${LUN_FILE:-nenhum}"
        echo "[*] USB Enable: ${LUN_EN:-0}"

        if pidof fuse_ntfs >/dev/null 2>&1 || killall -0 fuse_ntfs 2>/dev/null; then
            echo "[*] Processo FUSE NTFS: ATIVO"
        elif pidof fuse_direct >/dev/null 2>&1 || /system/xbin/busybox pgrep -f fuse_direct >/dev/null 2>&1; then
            echo "[*] Processo FUSE Direct: ATIVO"
        else
            echo "[*] Processo FUSE: PARADO"
        fi

        if pidof stream_fetcher >/dev/null 2>&1 || killall -0 stream_fetcher 2>/dev/null; then
            echo "[*] Processo Fetcher: ATIVO"
        else
            echo "[*] Processo Fetcher: PARADO"
        fi

        wp=$(cat "$WD_LOCK/pid" 2>/dev/null)
        if pid_is "$wp" tv_watchdog; then
            echo "[*] Watchdog: ATIVO (pid $wp)"
        else
            echo "[*] Watchdog: PARADO"
        fi
        ;;

    *)
        echo "Uso: $0 {vod|cinema|live|tv|favorites|channels|status}"
        exit 1
        ;;
esac
