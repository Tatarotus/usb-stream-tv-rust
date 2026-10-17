# Guia de Diagnóstico e Resolução de Problemas

Este guia reúne diagnósticos rápidos, causas-raiz conhecidas e soluções práticas para eventuais falhas durante a operação do **USB-Stream-TV**.

---

## 1. Problemas Frequentes e Soluções

### 1.1. Vídeo Congela no Primeiro Frame ao Abrir a Stream
- **Sintoma**: O reprodutor ConnectShare abre o arquivo, exibe o primeiro segundo ou quadro do vídeo e a imagem congela permanentemente, enquanto a barra de progresso continua ou trava.
- **Causa-Raiz Técnica**:
  1. *Underflow do Buffer CPB/VBV no chip MStar*: A transmissão original de IPTV continha quadros B (`has_b_frames > 0`) e foi enviada via cópia direta (`-c:v copy`) sem margem de avanço (`-muxdelay 0`). O decodificador da TV exige que quadros de referência cheguem com antecedência em relação ao clock de exibição.
  2. *Salto de Âncora no FUSE*: O driver FUSE detectou uma pausa de 1.5s entre a leitura do cabeçalho e o início do streaming e reancorou o fluxo para frente, omitindo os cabeçalhos SPS/PPS e frames IDR.
- **Solução**:
  - Garanta que o canal está configurado em **Modo Transcode** (`is_eco: false`), que crava `has_b_frames = 0`, GOP de 1.0s e fatia única.
  - Verifique se a versão de `fuse_ntfs` em execução contém a correção da linha 455 (`foff == 0`).
  - No tablet, reinicie a stack: `su -c '/system/xbin/switch_tv_mode.sh live'`.

---

### 1.2. Mensagem "Arquivo Não Suportado" ou Vídeo Sem Áudio
- **Sintoma**: A TV se recusa a reproduzir o arquivo ou exibe mensagem de áudio incompatível.
- **Causa-Raiz**:
  - Áudio em formato AAC dentro do contêiner `.ts` (o decodificador ConnectShare 2013 tem suporte limitado e instável a AAC em transport streams).
  - Resolução superior a 1080p (ex.: streams 4K/UHD) ou taxa de quadros superior a 30 fps (ex.: 60 fps em 1080p).
  - Codec de vídeo em HEVC/H.265 ou cor em 10-bit.
- **Solução**:
  - Confirme se o pipeline do FFmpeg está aplicando a normalização de áudio para **AC-3 Dolby Digital estéreo a 48 kHz**:
    ```bash
    -c:a ac3 -b:a 384k -ar 48000 -ac 2
    ```
  - Verifique o endpoint da VPS:
    ```bash
    ffprobe -v error -show_entries stream=codec_name https://tv.seudominio.com/live.ts
    ```
    Deve retornar estritamente `h264` e `ac3`.

---

### 1.3. A TV Não Reconhece o Pendrive ou Diz "Nenhum Dispositivo Conectado"
- **Sintoma**: Ao plugar o cabo USB na TV, nenhuma notificação de disco conectado é exibida.
- **Causa-Raiz**:
  - Cabo Micro-USB defeituoso ou cabo exclusivo de carga (sem linhas de dados D+ e D-).
  - Controlador USB Gadget desligado no Android.
  - Imagem de backing file não vinculada ao LUN.
- **Solução**:
  1. No tablet via ADB, verifique se a função UMS está ativa:
     ```bash
     cat /sys/class/android_usb/android0/functions
     # Deve conter: mass_storage,adb
     cat /sys/class/android_usb/android0/f_mass_storage/lun0/file
     # Deve apontar para: /data/local/tmp/ntfs_lab/mnt/tv_stream.img
     ```
  2. Dispare uma reconexão suave:
     ```bash
     su -c '/system/xbin/reconnect_usb.sh 2'
     ```
  3. Troque o cabo USB e utilize a porta da TV rotulada como **USB HDD (5V 1A)**.

---

### 1.4. O Arquivo na TV Não Muda de Nome Após Clicar em "TV Ao Vivo"
- **Sintoma**: Mesmo clicando repetidamente no botão "TV Ao Vivo", a lista da TV continua mostrando o mesmo arquivo antigo (ex.: apenas `TV AO VIVO.trp`).
- **Causa-Raiz**:
  - O utilitário `/data/local/tmp/patch_trp` não estava sendo invocado pelo script de troca antes de inicializar o FUSE.
- **Solução**:
  - O script [`scripts/switch_tv_mode.sh`](file:///home/sam/Code/usb-stream-tv-prod/scripts/switch_tv_mode.sh) atualizado executa o `patch_trp` automaticamente a cada ciclo:
    ```bash
    su -c '/data/local/tmp/patch_trp /data/local/tmp/ntfs_lab/ntfs_template.bin'
    ```
  - Isso garante a alternância matemática entre `TV AO VIVO.trp` e `TV AO VIVO 2.tp`.

---

## 2. Comandos de Diagnóstico Rápido (Cheat Sheet)

### 2.1. Telemetria do Tablet (SM-T110 via ADB)
```bash
# Inspecionar as últimas 20 leituras feitas pela TV em tempo real
adb -s 127.0.0.1:25555 shell "tail -n 20 /data/local/tmp/ntfs_lab/fuse_ntfs.log"

# Verificar status dos processos essenciais (FUSE e Fetcher)
adb -s 127.0.0.1:25555 shell "ps | grep -E 'fuse|stream|watchdog'"

# Verificar taxa de dados recebidos da rede
adb -s 127.0.0.1:25555 shell "tail -n 10 /data/local/tmp/ntfs_lab/stream_fetcher.log"

# Verificar dmesg para erros de barramento USB
adb -s 127.0.0.1:25555 shell "su -c dmesg | grep -iE 'usb|gadget|lun' | tail -n 20"
```

### 2.2. Telemetria do Servidor (VPS)
```bash
# Consultar status da API e canal ativo
curl -s https://tv.seudominio.com/api/status | jq .

# Inspecionar codecs, B-frames e perfis do fluxo ativo
ffprobe -v error -show_entries stream=codec_name,profile,level,has_b_frames,width,height,r_frame_rate -of json https://tv.seudominio.com/live.ts

# Inspecionar os logs do container Docker
ssh usuario@vps "docker logs --tail 50 -f usb-stream-tv"
```
