#!/system/xbin/busybox sh
# /system/etc/install-recovery-2.sh — boot do USB-Stream-TV (arquitetura atual)
# Substitui a chamada ao legado start_tv_ntfs.sh (8 GB) por: switch_tv_mode.sh live (template 128 GiB).
# Tudo roda em background e o script retorna na hora, para nao atrasar o boot do Android.

BB=/system/xbin/busybox
BASE=/data/local/tmp
LOGB=$BASE/boot_tv.log

(
    trap '' HUP
    echo "=== boot $($BB date '+%F %T') ===" >> "$LOGB"

    # /data pronto?
    n=0
    while [ ! -d "$BASE" ] && [ "$n" -lt 60 ]; do
        sleep 1
        n=$((n + 1))
    done

    # Locks de antes do reboot: PIDs sao reaproveitados, entao limpa tudo
    rm -rf "$BASE/mode_switch.lock" "$BASE/tv_watchdog.lock"

    # Watchdog primeiro: garante controle remoto/Chisel mesmo se a troca de modo falhar
    $BB setsid $BB sh /system/xbin/tv_watchdog.sh >> "$BASE/tv_watchdog.log" 2>&1 &

    # Espera rede (ate ~90s) para o fetcher conseguir o pre-buffer
    n=0
    until $BB wget -q -O /dev/null "http://tv.smre.run.place/api/status" 2>/dev/null; do
        n=$((n + 1))
        [ "$n" -ge 45 ] && break
        sleep 2
    done
    echo "rede: tentativas=$n" >> "$LOGB"

    # Orquestrador atual (ja garante o watchdog vivo ao entrar e ao sair)
    if ! $BB sh /system/xbin/switch_tv_mode.sh live >> "$LOGB" 2>&1; then
        echo "switch live falhou - nova tentativa em 15s" >> "$LOGB"
        sleep 15
        $BB sh /system/xbin/switch_tv_mode.sh live >> "$LOGB" 2>&1
    fi
) >/dev/null 2>&1 &

exit 0
