#!/system/xbin/busybox sh
# tv_watchdog.sh — Daemon 24/7 de resiliencia USB-Stream-TV
# Monitora adbd, Chisel, conexao do Gadget USB e polling de comandos da Web

BB=/system/xbin/busybox
SELF="/system/xbin/tv_watchdog.sh"
[ ! -x "$SELF" ] && SELF="/data/local/tmp/tv_watchdog.sh"
BASE=/data/local/tmp
STATE=$BASE/tvwd
LOGF=$BASE/tv_watchdog.log
LOCKF=$BASE/tv_watchdog.lock
SERVER="http://tv.smre.run.place"
USBD=/sys/class/android_usb/android0
CHISEL_BIN=/system/xbin/chisel
[ ! -x "$CHISEL_BIN" ] && CHISEL_BIN=$BASE/chisel
CHISEL_LOG=$BASE/chisel.log
TOKEN_FILE=$BASE/tablet.token
TOKEN=""
[ -f "$TOKEN_FILE" ] && TOKEN=$(cat "$TOKEN_FILE" 2>/dev/null | $BB tr -d '\r\n ')

log() {
    echo "$($BB date '+%m-%d %H:%M:%S') $*" >> "$LOGF"
}

cooldown_ok() { # $1=chave $2=segundos
    now=$(cat /proc/uptime 2>/dev/null | cut -d. -f1)
    case "$now" in *[!0-9]*|"") now=$($BB date +%s) ;; esac
    f="$STATE/cd.$1"
    last=0
    [ -f "$f" ] && last=$(cat "$f" 2>/dev/null)
    case "$last" in *[!0-9]*|"") last=0 ;; esac
    if [ $((now - last)) -ge "$2" ]; then
        echo "$now" > "$f"
        return 0
    fi
    return 1
}

trunc_if_big() { # $1=arquivo $2=max_bytes
    [ -f "$1" ] || return 0
    sz=$(cat "$1" 2>/dev/null | $BB wc -c)
    case "$sz" in *[!0-9]*|"") sz=0 ;; esac
    if [ "$sz" -gt "$2" ]; then
        $BB tail -c "$(( $2 / 2 ))" "$1" > "$1.tmp" 2>/dev/null && mv -f "$1.tmp" "$1"
    fi
}

urlenc() {
    printf %s "$1" | $BB od -An -tx1 2>/dev/null | $BB tr ' ' '\n' | while read -r b; do
        [ -n "$b" ] && printf '%%%s' "$b"
    done
}

start_chisel() {
    [ -x "$CHISEL_BIN" ] || return 1
    $BB setsid "$CHISEL_BIN" client --max-retry-interval 15s "$SERVER/chisel" R:25555:127.0.0.1:5555 >> "$CHISEL_LOG" 2>&1 &
}

lock_active() {
    [ -d "$BASE/mode_switch.lock" ]
}

# CRÍTICO 4: Busy check com remocao automatica de PID morto ou stale (>90s)
busy() {
    [ -f "$STATE/cur.pid" ] || return 1
    p=$(cat "$STATE/cur.pid" 2>/dev/null)
    case "$p" in *[!0-9]*|"") rm -f "$STATE/cur.pid"; return 1 ;; esac
    if kill -0 "$p" 2>/dev/null; then
        cur_time=$($BB date +%s)
        pid_mtime=$($BB stat -c %Y "$STATE/cur.pid" 2>/dev/null || echo "$cur_time")
        if [ $((cur_time - pid_mtime)) -gt 90 ]; then
            log "AVISO: cur.pid ($p) expirado (>90s). Removendo."
            rm -f "$STATE/cur.pid"
            return 1
        fi
        return 0
    fi
    rm -f "$STATE/cur.pid"
    return 1
}

send_ack() { # $1=id
    cid="$1"
    [ -f "$STATE/rc.$cid" ] || return 0
    crc=$(cat "$STATE/rc.$cid" 2>/dev/null)
    case "$crc" in *[!0-9]*|"") crc=1 ;; esac
    out=""
    # MÉDIO 1: Trunca saida para maximo 2048 bytes para nao estourar tamanho de URL
    if [ -f "$STATE/out.$cid" ]; then
        out=$($BB head -c 2048 "$STATE/out.$cid" 2>/dev/null || true)
    fi
    body="id=$cid&rc=$crc&out=$(urlenc "$out")"
    if $BB wget -q -T 5 --post-data="$body" -O /dev/null "$SERVER/api/tablet_ack?k=$TOKEN" 2>/dev/null; then
        rm -f "$STATE/unacked.$cid"
        return 0
    fi
    return 1
}

flush_unacked() {
    for u in "$STATE"/unacked.*; do
        [ -f "$u" ] || continue
        i=${u##*unacked.}
        send_ack "$i" || true
    done
}

run_cmd() { # $1=id $2=comando
    cid="$1"
    cmd="$2"
    log "CMD $cid: $cmd"
    set +e
    sh -c "$cmd" > "$STATE/out.$cid" 2>&1
    crc=$?
    set -e
    echo "$crc" > "$STATE/rc.$cid"
    rm -f "$STATE/run.$cid" "$STATE/cur.pid"
    : > "$STATE/unacked.$cid"
    send_ack "$cid"
    log "CMD $cid rc=$crc"
}

start_worker() {   # $1=id $2=comando
    : > "$STATE/run.$1"
    $BB setsid $BB sh "$SELF" --worker "$1" "$2" >/dev/null 2>&1 &
    wpid=$!
    echo "$wpid" > "$STATE/cur.pid"
}

handle_resp() {
    case "$1" in
        ''|none) return 0 ;;
        [0-9]*'|'*) ;;
        *) log "resposta inesperada: $(printf %s "$1" | dd bs=80 count=1 2>/dev/null)"
           return 0 ;;
    esac
    hid=${1%%|*}
    hcmd=${1#*|}
    case "$hid" in *[!0-9]*) return 0 ;; esac
    if [ -f "$STATE/rc.$hid" ]; then       # reentrega de comando ja executado
        : > "$STATE/unacked.$hid"
        return 0
    fi
    [ -f "$STATE/run.$hid" ] && return 0   # ja em execucao
    start_worker "$hid" "$hcmd"
    return 0
}

poll_and_dispatch() {
    USB_ST=$(cat $USBD/state 2>/dev/null)
    USB_PWR=$(cat /sys/class/power_supply/usb/online 2>/dev/null)
    VOD=$($BB head -n 1 "$BASE/vod_mode.flag" 2>/dev/null | $BB tr -d '\r')
    if [ -f "$BASE/favorites_mode.flag" ]; then
        MODE=favorites
    elif [ -f "$BASE/vod_mode.flag" ]; then
        MODE=vod
    else
        MODE=live
    fi
    X=""
    if busy || lock_active; then X="&nocmd=1"; fi
    RESP=$($BB wget -q -T 5 -O - \
        "$SERVER/api/tablet_cmd?v=2&k=$TOKEN&usb=${USB_ST:-DISCONNECTED}&pwr=${USB_PWR:-0}&vod=$(urlenc "${VOD:-none}")&mode=$MODE$X" 2>/dev/null)
    handle_resp "$RESP"
}

# ALTO 3: Contagem precisa de processos chisel (zero vs 1)
supervise_chisel() {
    pids=$($BB pidof chisel 2>/dev/null || true)
    if [ -z "$pids" ]; then
        n=0
    else
        set -- $pids
        n=$#
    fi
    if [ "$n" -eq 1 ]; then
        CH_FAILS=0
        return 0
    fi
    if [ "$n" -gt 1 ]; then
        log "chisel duplicado ($n) - reiniciando"
        killall chisel 2>/dev/null
        sleep 1
    fi
    now=$($BB date +%s)
    [ "$now" -lt "$CH_NEXT" ] && return 0
    start_chisel
    CH_FAILS=$((CH_FAILS + 1))
    d=$((CH_FAILS * 5))
    [ "$d" -gt 60 ] && d=60
    CH_NEXT=$((now + d))
    log "chisel iniciado (tentativa $CH_FAILS; proxima so apos ${d}s)"
    return 0
}

# MÉDIO 3: Housekeeping com limpeza limpa de registros antigos
housekeeping() {
    trunc_if_big "$LOGF" 262144
    trunc_if_big "$CHISEL_LOG" 524288
    $BB ls -t "$STATE"/rc.* 2>/dev/null | $BB tail -n +31 | while read -r f; do
        i=${f##*rc.}
        rm -f "$STATE/rc.$i" "$STATE/out.$i" "$STATE/unacked.$i" "$STATE/run.$i"
    done
    return 0
}

# ------------------------------------------------------- modo worker (--worker)
if [ "$1" = "--worker" ]; then
    run_cmd "$2" "$3"
    exit 0
fi

# ----------------------------------------------------------- singleton do loop
# CRÍTICO 3: NUNCA matar workers ativos de troca de modo (--worker)
for p in $($BB pgrep -f "tv_watchdog" 2>/dev/null || true); do
    case "$p" in ""|$$|"$PPID") continue ;; esac
    if $BB grep -q -- "--worker" "/proc/$p/cmdline" 2>/dev/null; then
        continue
    fi
    kill -9 "$p" 2>/dev/null || true
done

if ! mkdir "$LOCKF" 2>/dev/null; then
    oldpid=$(cat "$LOCKF/pid" 2>/dev/null || true)
    if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then
        exit 0
    fi
    rm -rf "$LOCKF"
    mkdir "$LOCKF" 2>/dev/null || exit 0
fi
echo $$ > "$LOCKF/pid"

cleanup_lock() {
    rm -rf "$LOCKF"
}
trap cleanup_lock EXIT INT TERM

mkdir -p "$STATE"
log "watchdog v2 iniciado (pid $$; wget_T='-T 5')"
[ -z "$TOKEN" ] && log "AVISO: /data/local/tmp/tablet.token ausente"

# Loop Principal 24/7
CH_FAILS=0
CH_NEXT=0
TICK=0

while true; do
    TICK=$((TICK + 1))
    
    # 1. Manter tela desligada e CPU em performance
    if ! $BB grep -q tv_stream /sys/power/wake_lock 2>/dev/null; then
        echo tv_stream > /sys/power/wake_lock 2>/dev/null
    fi
    
    # 2. Checar adbd listener na porta 5555
    if ! $BB grep -q " 00000000:15B3 " /proc/net/tcp 2>/dev/null; then
        if cooldown_ok adbd 30; then
            log "adbd sem listener na 5555 - reiniciando"
            setprop persist.adb.tcp.port 5555
            setprop service.adb.tcp.port 5555
            stop adbd 2>/dev/null
            start adbd 2>/dev/null
        fi
    fi

    # 3. Supervisionar túnel Chisel
    supervise_chisel

    # 4. Polling de comandos da Web
    poll_and_dispatch
    flush_unacked

    # 4b. Ressincronia USB pedida pelo FUSE (T6, Prompt 9, revisto pós-auditoria):
    # a emenda seria fútil (conteúdo antigo sumiu do anel e sem checkpoint), então
    # recicla o stack live (anti-cache triple-tier: a TV purga a RAM e reabre
    # limpo no ao vivo). Só no modo live. Breaker: no máximo 3 a cada 15 min
    # (oscilação de upstream não pode ciclar o USB sem parar); cooldown 120 s.
    if [ -f "$BASE/resync_usb" ] && [ ! -f "$BASE/vod_mode.flag" ] && [ ! -f "$BASE/favorites_mode.flag" ]; then
        rm -f "$BASE/resync_usb"
        SW_BIN=""
        [ -x /system/xbin/switch_tv_mode.sh ] && SW_BIN="/system/xbin/switch_tv_mode.sh"
        [ -z "$SW_BIN" ] && [ -x "$BASE/switch_tv_mode.sh" ] && SW_BIN="$BASE/switch_tv_mode.sh"
        if [ -z "$SW_BIN" ]; then
            log "resync_usb ignorado: switch_tv_mode.sh ausente/nao executavel"
        elif cooldown_ok resync 120; then
            now=$(cat /proc/uptime 2>/dev/null | cut -d. -f1)
            case "$now" in *[!0-9]*) now=0 ;; esac
            n=0; first=0
            [ -f "$STATE/rs.count" ] && n=$(cat "$STATE/rs.count" 2>/dev/null)
            [ -f "$STATE/rs.first" ] && first=$(cat "$STATE/rs.first" 2>/dev/null)
            case "$n" in *[!0-9]*) n=0 ;; esac
            case "$first" in *[!0-9]*) first=0 ;; esac
            if [ $((now - first)) -gt 900 ]; then n=0; first=$now; echo "$now" > "$STATE/rs.first"; fi
            if [ "$n" -ge 3 ]; then
                log "resync breaker: 3 em 15min, adiando ciclo USB"
            else
                echo $((n + 1)) > "$STATE/rs.count"
                log "resync_usb pedido pelo FUSE - reciclando stack live (anti-cache)"
                $BB setsid $BB sh "$SW_BIN" live >> "$LOGF" 2>&1 &
            fi
        fi
    fi

    # 5. Housekeeping periódico a cada ~60 ticks (60s)
    if [ $((TICK % 60)) -eq 0 ]; then
        housekeeping
    fi

    sleep 1
done
