# Arquitetura do Sistema USB-Stream-TV

Este documento descreve detalhadamente a arquitetura de engenharia do **USB-Stream-TV**, cobrindo o motor de emulação de pendrive USB, o driver em espaço de usuário (FUSE NTFS), o subsistema anti-cache de hardware e o pipeline de transcodificação.

---

## 1. Visão Geral da Topologia

O sistema é dividido em três camadas desacopladas que se comunicam através de protocolos padrão:

```
┌─────────────────────────────────────────────────────────────┐
│ 1. NUVEM / SERVIDOR CENTRAL (Oracle Cloud VPS)              │
│    - Servidor HTTP assíncrono (FastAPI / Python 3.11)       │
│    - Gerenciador de Subprocessos FFmpeg sob demanda         │
│    - Normalizador de Áudio (AC-3 48kHz Dolby Digital)       │
│    - Normalizador de Vídeo (H.264 Main L4.1, 0 B-frames)    │
│    - SeamlessRestamper (PTS/DTS monotônicos entre canais)   │
└──────────────────────────────┬──────────────────────────────┘
                               │ HTTP Streaming (Porta 80/8080)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. RECEPTOR E EMULADOR USB (Tablet Android SM-T110)         │
│    - stream_fetcher: Cliente de rede TCP ultraleve          │
│    - FIFO IPC: Pipe de alta velocidade em RAM (/dev/pipe)   │
│    - fuse_ntfs: Driver FUSE NTFS com Ring Buffer Circular   │
│    - Linux USB Gadget: Controlador USB Mass Storage (UMS)   │
└──────────────────────────────┬──────────────────────────────┘
                               │ SCSI Mass Storage sobre USB 2.0
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. DISPOSITIVO DE APRESENTAÇÃO (TV Samsung PL51F4000)       │
│    - Controladora USB Host MStar SoC (ConnectShare 2013)    │
│    - Leitor de Sistema de Arquivos NTFS do Firmware         │
│    - Decodificador de Hardware H.264/AC-3                   │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. O Driver FUSE NTFS (`src/ntfs/fuse_ntfs.c`)

Ao contrário dos sistemas tradicionais que tentam gravar arquivos físicos no flash e truncá-los continuamente (o que destrói a memória flash do dispositivo móvel e corrompe tabelas FAT32), o **USB-Stream-TV** utiliza um driver em espaço de usuário (**FUSE**) que implementa uma partição NTFS virtual.

### 2.1. Geometria Esparsa e Mapeamento de Setores (128 GiB Thin-Provisioned)

O FUSE carrega um template estático de metadados NTFS de 128 GiB (`templates/ntfs_template.sparse.gz`), descompactado via `src/tools/sparse_unpack_arm32` diretamente para o arquivo esparso `/data/local/tmp/ntfs_lab/ntfs_template.bin`:
- **Setores Totais**: `268.435.456` setores de 512 bytes (~128.00 GiB).
- **Tamanho do Cluster**: 4.096 bytes (8 setores).
- **Pegada Física em Disco**: Graças ao suporte a arquivos esparsos no ext4 do Android, os 128 GiB virtuais consomem apenas **~69 MB reais** na partição flash `/data`.
- **MFT Inode 27**: Registro MFT reservado para o arquivo de streaming. O atributo `$DATA` contém uma *runlist* mapeando grandes extents contíguos que cobrem toda a área de dados da partição virtual de 128 GiB.

Quando a TV emite leituras SCSI para setores de metadados (Setor de Boot LBA 0, `$MFT`, `$Bitmap`, diretório raiz), o FUSE responde instantaneamente servindo os bytes correspondentes da imagem de metadados em RAM ou arquivo esparso.

### 2.2. Ring Buffer Circular em Memória RAM

Quando a TV lê setores correspondentes à área de dados do arquivo (`foff`), a requisição é interceptada por `serve_live_backend()`:

1. **Capacidade do Ring Buffer**: 128 MiB alocados em RAM (`RINGSZ = 134.217.728` bytes).
2. **Ponteiro de Escrita Monotônico (`g_s_write`)**: A cada chunk de dados recebido da rede pelo `feeder_thread`, `g_s_write` avança de forma estritamente crescente.
3. **Âncora de Reprodução (`g_anchor_foff` e `g_anchor_stream_pos`)**:
   - Mapeia o offset virtual do arquivo lido pela TV (`foff`) para a posição linear da transmissão ao vivo:
     $$\text{stream\_pos} = \text{g\_anchor\_stream\_pos} + (\text{foff} - \text{g\_anchor\_foff})$$
   - Permite que a TV leia do byte `0` até o byte `8.000.000.000` enquanto o buffer de memória RAM mantém apenas os últimos 128 MiB da transmissão.

### 2.3. Sincronização e Busca por Ponto de Partida (`snap_open_base_target`)

Quando a TV inicia a reprodução, o motor realiza um alinhamento estrito para garantir que o decodificador de hardware nunca receba fragmentos de frames no meio de uma GOP:
1. Recua `LEADBACK` bytes (12 MiB, ~20 segundos) a partir do ponteiro atual `g_s_write`.
2. Varre o ring buffer procurando o pacote MPEG-TS que contenha:
   - Identificador de sincronismo `0x47`.
   - PID `0x0000` (PAT - Program Association Table).
   - Início de Payload (PUSI).
   - Pacote de vídeo subsequente contendo NAL Tipo 7 (SPS) e Tipo 8 (PPS), garantindo um frame IDR completo.
3. Fixa a âncora nesse ponto (`g_anchor_stream_pos`), assegurando que o decodificador MStar da TV inicie sua reprodução em um ponto de entrada limpo.

### 2.4. Isolamento de Sondagem (Probe Isolation)

Durante a montagem da unidade ou abertura de diretório, o firmware da TV realiza leituras especulativas e fora de ordem (ex.: lê offset 0, depois lê offset 256 MB, 600 MB ou os últimos clusters do disco) para tentar detectar átomos MP4 ou metadados de outros contêineres:
- **Proteção Ativa**:
  ```c
  if (g_base_valid && foff >= 8ULL * 1024 * 1024) {
      uint64_t s_probe = foff_to_stream_pos(foff);
      if (s_probe >= g_s_write + 2ULL * 1024 * 1024) {
          fill_null(dst, c);
          return;
      }
  }
  ```
  Leituras além da cabeça de gravação ativa retornam pacotes MPEG-TS de preenchimento nulo (PID `0x1FFF`: `0x47, 0x1F, 0xFF, 0x10, 0xFF...`), sem alterar a âncora do fluxo principal.

### 2.5. Pacing e Controle de Fluxo

A controladora USB 2.0 do tablet tem capacidade de fornecer dados a ~25–35 MB/s, enquanto a transmissão ao vivo chega a ~0.6–1.0 MB/s. Se a TV ler a toda velocidade, esgota o buffer de segurança de 20 segundos em menos de 1 segundo.
- **Mecanismo de Pacing Suave**: Quando a margem de segurança entre `g_s_write` e o ponto de leitura da TV é menor que 8 MiB, o driver limita suavemente a taxa de entrega para ~1.0 MB/s.
- Durante o sono (`usleep`), o mutex global `g_mu` é liberado para garantir que a thread de ingestão de rede (`feeder_thread`) nunca seja bloqueada.

### 2.6. Ingestão Nativa e Resiliência de Rede (`src/client/stream_fetcher.c`)

O `stream_fetcher` é o daemon em C nativo executado no processador Cortex-A7 do tablet Android. Ele ingere o fluxo MPEG-TS contínuo do servidor HTTP e alimenta o FIFO `/data/local/tmp/live_pipe` lido pelo `fuse_ntfs`. Suas garantias de estabilidade 24/7 incluem:

1. **Parser HTTP Bulk de 4 KB (Zero-Stall)**:
   - Elimina loops de leitura byte a byte (`recv(sock, &c, 1, 0)`), substituindo-os por leitura em bloco (`g_header_buf[4096]`).
   - Qualquer payload de vídeo TS recebido junto aos cabeçalhos HTTP ou no delimitador chunked é preservado via ponteiro `body_start` e descarregado diretamente no pipeline sem perdas.
2. **Clamping de Timeouts (<3.5s)**:
   - O barramento SCSI do decodificador Samsung MStar aborta requisições após 3.5 a 4.0 segundos de silêncio.
   - O `stream_fetcher` crava `poll()` em 2.500 ms e `SO_RCVTIMEO` / `SO_SNDTIMEO` em 3.000 ms, detectando sockets mortos e reconectando antes que a TV entre em pânico de I/O.
3. **Resolução DNS Resiliente com Fallback**:
   - Cache de IP resolved via `getaddrinfo` com fallback estático para o IP de produção caso o servidor DNS local falhe durante oscilações Wi-Fi.
4. **Alinhamento e Compactação MPEG-TS de 188 Bytes**:
   - O buffer acumulador (`g_acc`) descarta bytes antigos apenas em múltiplos estritos de 188 (`discard_len = to_discard - (to_discard % 188)`), preservando o fragmento terminal `< 188B` e pacotes inteiros anteriores para evitar qualquer descontinuidade de PCR ou corrupção de sincronismo (`0x47`).
5. **Backpressure e Heartbeat Atômico**:
   - Abertura de FIFO não-bloqueante (`O_NONBLOCK`) com multiplexação `POLLOUT` (timeout de 2.5s) para tratar reinicializações do `fuse_ntfs`.
   - Atualização periódica de heartbeat via `futimens(hb_fd, NULL)` a cada 2.0s, eliminando 43.200 ciclos de abertura e truncamento (`O_TRUNC`) na memória flash por dia.

---

## 3. Arquitetura Anti-Cache de Hardware e Temporização SCSI

A TV Samsung ConnectShare 2013 possui cache agressivo em memória não-volátil (NVM). Se um pendrive for reconectado com o mesmo nome e serial, a TV tenta retomar a reprodução da última posição salva em segundos anteriores, travando a reprodução ao vivo.

Para eliminar completamente esse comportamento, o sistema implementa uma estratégia de três níveis independentes a cada troca de canal ou reinicialização:

```
┌─────────────────────────────────────────────────────────────┐
│ 1. ALTERNÂNCIA DE INQUIRY STRING SCSI USB                   │
│    - LIVETV1 ↔ LIVETV2                                      │
│    - Força o kernel da TV a desmontar e remontar o disco    │
└──────────────────────────────┬──────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. RANDOMIZAÇÃO DO VOLUME SERIAL NUMBER (NTFS VBR)          │
│    - Gerado aleatoriamente a cada boot no offset 0x48       │
│    - A TV detecta como um sistema de arquivos inédito       │
└──────────────────────────────┬──────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. PATCHING DE METADADOS MFT (patch_trp)                    │
│    - Alterna Inode 27 entre "TV AO VIVO.trp" e              │
│      "TV AO VIVO 2.tp"                                      │
│    - Descarta qualquer cache de arquivo e resume do zero    │
└─────────────────────────────────────────────────────────────┘
```

### 3.1. Temporizações Canônicas de Barramento USB (`switch_tv_mode.sh`)
Para que o host USB da TV processe os eventos de desconexão e conexão sem travar o stack SCSI:
- **Teardown do Gadget**: Pausa de `0.8s` após desabilitar o gadget (`echo 0 > enable`).
- **Desvinculação do LUN**: Pausa de `0.3s` após esvaziar o arquivo backing (`echo "" > lun0/file`).
- **Inquiry Setup**: Pausa de `0.2s` após escrever a nova Inquiry String antes de reabilitar o gadget (`echo 1 > enable`).
- **Fail-Safe Rollback (`fail_rollback`)**: Qualquer falha inesperada durante a troca de modo aciona o rollback imediato, restaurando o gadget USB com uma imagem válida para que a TV nunca permaneça em *"Dispositivo não reconhecido"*.

---

## 4. O Pipeline de Transcodificação no Servidor (`server.py`)

Para que o decodificador MStar 2013 processe a transmissão sem travar, os parâmetros de saída do FFmpeg no servidor são estritamente calibrados:

| Parâmetro FFmpeg | Valor | Finalidade Técnica |
| :--- | :--- | :--- |
| `-c:v` | `libx264` | Transcodificador de referência |
| `-preset` | `ultrafast` | Mínima latência de processamento na VPS |
| `-tune` | `zerolatency` | **Elimina B-frames (`bframes=0`)**, evitando atrasos de reordenação |
| `-g` e `-keyint_min` | `30` | Crava exatamente 1 frame IDR a cada 1.0s (30 fps) |
| `-x264-params` | `repeat-headers=1` | Repete NAL SPS e PPS antes de **todos** os frames IDR |
| `-pcr_period` | `20` | Emite PCR a cada 20 ms, travando o clock de 27 MHz do PLL da TV |
| `-c:a` | `ac3` | Dolby Digital AC-3 estéreo 48 kHz (padrão nativo do chip MStar) |
| `-b:a` | `384k` | Alta fidelidade sonora com conformidade ATSC/DVB |
| `-muxdelay` | `0` (transcode) / `0.7` (eco) | Margem temporal adequada para o buffer de decodificação CPB |
| `-streamid` | `0:256, 1:257` | PIDs fixos e imutáveis por canal (Vídeo: 0x100, Áudio: 0x101) |

---

## 5. Troca de Canais Contínua (SeamlessRestamper)

Quando o usuário troca de canal através do controle remoto PWA:
1. **Make-Before-Break**: O canal anterior continua transmitindo até que o novo canal estabeleça conexão de rede, decodifique o primeiro frame e entregue os primeiros pacotes TS.
2. **SeamlessRestamper**: O módulo analisa e reescreve os valores de **PCR**, **PTS** e **DTS** do novo canal em tempo real, garantindo que o contador de tempo avance de forma estritamente monotônica sem nunca saltar para trás ou reiniciar em zero.
3. A TV não percebe a troca física do fluxo; a imagem muda suavemente como em uma transmissão de TV aberta tradicional.

---

## 6. Arquitetura VOD Cinema V2 (`src/client/fuse_direct_v2.c`)

Para suportar conteúdos sob demanda (filmes MP4, gravações e vídeos do YouTube) mantendo a TV Samsung ConnectShare 2013 imune a *buffer underrun* e travamentos SCSI, o cliente de emulação introduz a arquitetura **VOD V2 Dual-Cache com Prefetch Assíncrono**:

### 6.1. FUSE Head Pinning Cache (8 MB)
- **Zero Latency para Metadados e Átomos**: Os primeiros 8 MB (`0..8388608` bytes) do arquivo de vídeo sob demanda são mantidos permanentemente fixados em RAM (`vod_head_cache`).
- **Imunidade a Seeks para o Início**: Sempre que o firmware da TV realiza leitura do átomo `moov`/`ftyp` ou retrocesso para o início do arquivo, a resposta é entregue instantaneamente (0 ms) a partir da memória, sem disparar requisições HTTP redundantes para o servidor ou VPS.

### 6.2. Dedicated Media Sliding Cache (16 MB) e Double Buffering
- **Prefetch Assíncrono com Background Worker**: Uma thread dedicada em background (`vod_prefetch_worker`) antecipa a leitura da mídia em blocos de 4 MB (`VOD_CHUNK_SZ`), preenchendo o buffer secundário enquanto a TV lê do buffer primário (`vod_media_cache` e `vod_prefetch_buf`).
- **Troca de Ponteiros O(1)**: Quando o offset da TV alcança o chunk pré-carregado, ocorre uma alternância atômica de buffers sem cópia de memória (*double buffering*).
- **Conexão TCP Persistente (`TCP_NODELAY`)**: Conexão keep-alive dedicada com socket persistente e parser HTTP bulk em lote, eliminando o overhead de handshake e loops de leitura de 1 byte.
- **Cancelamento Rápido em Seek**: Se o usuário realizar seek arbitrário além da janela pré-carregada, uma flag volátil `vod_prefetch_abort` sinaliza o encerramento do chunk corrente para sincronizar imediatamente com a nova posição.

### 6.3. Orquestração de Produção e Alternância Segura de Modos
- **`scripts/deploy_tablet.sh`**: Implantação e atualização automatizada e atômica via ADB, instalando o template esparso de 128 GiB (`sparse_unpack_arm32`), binários estáticos compilados para ARM32 e scripts de watchdog.
- **`scripts/switch_tv_mode.sh`**: Alternador atômico de modos de exibição (Live TV vs. VOD Cinema) com mecanismo `fail_rollback()` para recuperação automática em caso de erro, locks de concorrência com detecção de stale e atrasos calibrados de barramento SCSI.
- **`scripts/tv_watchdog.sh`**: Daemon contínuo de supervisão que monitora o túnel reverso, estado do gadget USB, locks e processos essenciais (`fuse_ntfs`, `stream_fetcher`, `fuse_direct`), garantindo imunidade de workers durante trocas de canal.
- Para especificações completas de engenharia e pipelines de transcodificação de VOD, consulte [docs/VOD_ARCHITECTURE.md](file:///home/sam/Code/usb-stream-tv-prod/docs/VOD_ARCHITECTURE.md).
