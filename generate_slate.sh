#!/bin/bash
# Gera vídeos de slate (placeholder "Sem Sinal") para proteção do ConnectShare
set -e

# Diretório base: onde o script está localizado ou argumento 1
BASE_DIR="${1:-$(dirname "$(readlink -f "$0")")}"
cd "$BASE_DIR"

# Detecta fonte disponível
FONT_BOLD=""
FONT_REG=""

for fb in \
    /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf \
    /usr/share/fonts/liberation/LiberationSans-Bold.ttf \
    /usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf \
    /usr/share/fonts/noto/NotoSans-Bold.ttf \
    /usr/share/fonts/truetype/freefont/FreeSansBold.ttf; do
    if [ -f "$fb" ]; then
        FONT_BOLD="$fb"
        break
    fi
done

for fr in \
    /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf \
    /usr/share/fonts/liberation/LiberationSans-Regular.ttf \
    /usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf \
    /usr/share/fonts/noto/NotoSans-Regular.ttf \
    /usr/share/fonts/truetype/freefont/FreeSans.ttf; do
    if [ -f "$fr" ]; then
        FONT_REG="$fr"
        break
    fi
done

if [ -n "$FONT_BOLD" ] && [ -n "$FONT_REG" ]; then
    echo "[*] Usando fontes: $FONT_BOLD e $FONT_REG"
    VF720="color=c=0x1a1a2e:s=1280x720:d=10:r=30,drawtext=text='Sem Sinal':fontsize=52:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2-50:fontfile=$FONT_BOLD,drawtext=text='Reconectando...':fontsize=32:fontcolor=0xaaaaaa:x=(w-text_w)/2:y=(h-text_h)/2+20:fontfile=$FONT_REG"
    VF1080="color=c=0x1a1a2e:s=1920x1080:d=10:r=30,drawtext=text='Sem Sinal':fontsize=72:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2-60:fontfile=$FONT_BOLD,drawtext=text='Reconectando...':fontsize=42:fontcolor=0xaaaaaa:x=(w-text_w)/2:y=(h-text_h)/2+30:fontfile=$FONT_REG"
elif [ -n "$FONT_BOLD" ]; then
    echo "[*] Usando fonte: $FONT_BOLD"
    VF720="color=c=0x1a1a2e:s=1280x720:d=10:r=30,drawtext=text='Sem Sinal':fontsize=52:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2-50:fontfile=$FONT_BOLD,drawtext=text='Reconectando...':fontsize=32:fontcolor=0xaaaaaa:x=(w-text_w)/2:y=(h-text_h)/2+20:fontfile=$FONT_BOLD"
    VF1080="color=c=0x1a1a2e:s=1920x1080:d=10:r=30,drawtext=text='Sem Sinal':fontsize=72:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2-60:fontfile=$FONT_BOLD,drawtext=text='Reconectando...':fontsize=42:fontcolor=0xaaaaaa:x=(w-text_w)/2:y=(h-text_h)/2+30:fontfile=$FONT_BOLD"
else
    echo "[WARN] Nenhuma fonte encontrada. Slate será apenas cor sólida."
    VF720="color=c=0x1a1a2e:s=1280x720:d=10:r=30"
    VF1080="color=c=0x1a1a2e:s=1920x1080:d=10:r=30"
fi

COMMON_ARGS="-c:v libx264 -preset ultrafast -tune zerolatency -profile:v main -level 4.1 \
  -g 30 -keyint_min 30 -sc_threshold 0 -x264-params repeat-headers=1:aq-mode=2:aq-strength=1.0 \
  -r 30 -c:a ac3 -b:a 384k -ar 48000 -ac 2 \
  -af aresample=async=1000:first_pts=0:min_hard_comp=0.100000 \
  -avoid_negative_ts make_zero -fflags +genpts+nobuffer -flags low_delay \
  -streamid 0:256 -streamid 1:257 -mpegts_pmt_start_pid 4096 \
  -mpegts_flags +resend_headers+pat_pmt_at_frames -muxdelay 0 -muxpreload 0 \
  -f mpegts -t 10"

echo "[*] Gerando slate.ts (720p)..."
ffmpeg -y -f lavfi -i "$VF720" -f lavfi -i "anullsrc=r=48000:cl=stereo" \
  -b:v 2200k -maxrate 2600k -bufsize 4400k $COMMON_ARGS slate.ts

echo "[*] Gerando slate_1080p.ts (1080p)..."
ffmpeg -y -f lavfi -i "$VF1080" -f lavfi -i "anullsrc=r=48000:cl=stereo" \
  -b:v 3800k -maxrate 4500k -bufsize 7600k $COMMON_ARGS slate_1080p.ts

echo "[✓] Slates gerados com sucesso:"
ls -lh slate.ts slate_1080p.ts
