# USB-Stream-TV

> **Streaming de TV ao vivo (IPTV/HLS) e VOD diretamente para TVs legadas (não-smart) via emulação de pendrive USB Mass Storage (UMS) em hardware Android com root.**

---

## 📺 Visão Geral

O **USB-Stream-TV** soluciona o desafio de levar IPTV e conteúdo sob demanda (VOD) com qualidade digital Full HD (1080p) e áudio Dolby Digital para televisores antigos que possuem apenas porta USB com reprodutor de mídia embutido (ex.: **Samsung ConnectShare / MStar SoC de 2013**), sem necessidade de HDMI, Chromecast, TV Box ou Android TV.

O sistema transforma um dispositivo Android antigo com suporte a OTG/USB Gadget (como o **Samsung Galaxy Tab 3 Lite SM-T110**) em um **pendrive virtual pseudo-infinito**. Enquanto a TV lê continuamente blocos de setores do pendrive acreditando estar reproduzindo um arquivo estático `.ts`, `.trp` ou `.tp`, o motor em espaço de usuário (**FUSE**) injeta os pacotes da transmissão de rede ao vivo em tempo real.

```
┌─────────────────────────────────┐       HTTP Live TS       ┌────────────────────────────────────┐
│      Oracle Cloud VPS Hub       │ ───────────────────────> │  Android Tablet (SM-T110 / Mi A2)  │
│  (FFmpeg + Seamless Restamper)  │    (H.264 + AC-3 48kHz)  │  (/system/xbin/stream_fetcher)     │
└─────────────────────────────────┘                          └─────────────────┬──────────────────┘
                                                                               │
                                                                       FIFO / UNIX Pipe
                                                                               ▼
┌─────────────────────────────────┐    SCSI USB Mass Storage ┌────────────────────────────────────┐
│   Samsung Plasma PL51F4000      │ <─────────────────────── │        FUSE NTFS Driver            │
│ (ConnectShare / MStar SoC 2013) │     Emulated USB Gadget  │  (/system/xbin/fuse_ntfs)          │
└─────────────────────────────────┘   (TV AO VIVO.trp / .tp) └────────────────────────────────────┘
```

---

## ✨ Recursos Principais

- **Emulação USB Mass Storage Inteligente**: Apresenta à TV uma partição NTFS válida de 128 GiB thin-provisioned (ocupando apenas ~69 MB reais no filesystem ext4 do Android) contendo o arquivo de transmissão ao vivo (`TV AO VIVO.trp` ou `TV AO VIVO 2.tp`).
- **Mapeamento Circular em RAM com FUSE**: Ring buffer de 128 MiB em RAM que mapeia offsets infinitos de reprodução para a janela ao vivo atual, mantendo um colchão de segurança de 20–25 segundos.
- **Anti-Cache de Hardware Automatizado**:
  - Alternância de nomes no sistema de arquivos NTFS MFT (`TV AO VIVO.trp` ↔ `TV AO VIVO 2.tp`).
  - Randomização do *Volume Serial Number* do setor de boot NTFS a cada inicialização.
  - Alternância da *Inquiry String* SCSI USB (`LIVETV1` ↔ `LIVETV2`).
- **Pipeline de Transcodificação Calibrado para MStar**:
  - H.264 Constrained Baseline / Main Profile Level 4.1 a 1080p@30fps.
  - **Zero B-frames** (`bframes=0`), eliminando qualquer risco de underflow do buffer VBV/CPB.
  - Repetição de SPS/PPS antes de **cada** frame IDR (`repeat-headers=1`).
  - Intervalo de PCR cravado em 20 ms (`-pcr_period 20`), garantindo travamento estável do PLL de 27 MHz da TV.
  - Áudio normalizado em Dolby Digital AC-3 estéreo a 48 kHz (padrão nativo do chip MStar).
- **Controle Remoto Web / PWA**: Interface web responsiva instalável em qualquer smartphone, com troca de canais instantânea via *Make-Before-Break* (sem tela preta).
- **Arquitetura VOD V2 com Dual-Cache & Prefetch Assíncrono**:
  - **FUSE Head Pinning Cache (8 MB)**: Fixação em RAM dos primeiros 8 MB para resposta imediata (0 ms) de cabeçalhos e átomos MP4 (`moov`/`ftyp`), eliminando latência em seeks para a posição zero.
  - **Dedicated Media Sliding Cache (16 MB)**: Double buffering com thread assíncrona de prefetch em blocos de 4 MB e conexões TCP persistentes com `TCP_NODELAY`, prevenindo travamentos e *buffer underruns*.
  - **Fail-Safe & Watchdog Resiliente**: Transição atômica de modos com `fail_rollback()` para impedir gadget órfão em `enable=0`, timeouts de teardown calibrados para SCSI e watchdog com isolamento de workers.
- **Ingestão Ultraleve com Resiliência de Rede**:
  - Parser HTTP bulk de 4 KB eliminando milhares de transições user-kernel.
  - Timeouts de rede e socket clampados a 2.5s / 3.0s (estritamente abaixo do deadline de 3.5s do MStar).
  - Resolução DNS com cache e fallback estático.
  - Compactação de buffer MPEG-TS com preservação de alinhamento de 188 bytes para prevenção de descontinuidade de PCR/PTS.
- **Modos de Operação**:
  - **TV Ao Vivo (Live)**: Transmissão linear contínua.
  - **Cinema (VOD)**: Catálogo sob demanda integrado a torrents e filmes MP4 locais.
  - **Multi-Canais / Favoritos**: Exposição de diretório virtual com múltiplos canais.

---

## 📂 Estrutura do Repositório

```text
usb-stream-tv-prod/
├── .env.example            # Exemplo de configurações e credenciais (seguro para git)
├── .env                    # Suas credenciais reais e portas (ignorado pelo git)
├── .gitignore              # Proteção estrita contra vazamento de credenciais e caches
├── server.py               # Hub central de streaming (Python / FastAPI / FFmpeg)
├── Dockerfile              # Imagem do servidor para Docker
├── compose.yaml            # Configuração de implantação Docker Compose
├── channels.json           # Grade unificada e higienizada de canais (sem senhas)
├── generate_slate.sh       # Gerador das telas de standby/slate offline
├── gen_template.py         # Gerador de templates sintéticos VOD
├── dashboard.html          # Controle remoto Web & PWA
├── manifest.json           # Manifesto PWA
├── sw.js                   # Service Worker PWA
├── app-icon-512.png        # Ícone do PWA
├── app-icon.png            # Ícone do PWA
├── torrent_downloader.py   # Gerenciador de downloads torrent/magnet e pós-processamento
├── src/
│   ├── ntfs/               # Motor do driver FUSE NTFS
│   │   ├── fuse_ntfs.c     # Implementação em C do FUSE NTFS
│   │   ├── fuse_ntfs.h     # Definições de geometria e estruturas NTFS
│   │   ├── catalog_hierarchy.h # Definições de diretórios virtuais de canais
│   │   ├── catalog_favorites.h # Mapeamento de favoritos
│   │   └── fuse_ntfs_arm32 # Binário estático compilado para ARM32
│   ├── client/             # Ingestão de rede ultraleve e VOD Direct
│   │   ├── fuse_direct_v2.c # Motor FUSE VOD v2 com Head Pinning Cache de 8MB e prefetch
│   │   ├── fuse_direct_arm32 # Binário estático ARM32 para VOD v2
│   │   ├── stream_fetcher.c # Ingestor TCP socket bulk de alta velocidade
│   │   └── stream_fetcher_arm32 # Binário estático para ARM32
│   └── tools/              # Utilitários de sistema de arquivos e MFT
│       ├── patch_trp.c     # Patcher de MFT Inode 27 para alternar arquivos
│       ├── patch_trp_arm32 # Binário estático ARM32
│       ├── sparse_unpack.c # Descompactador de imagens esparsas thin-provisioned
│       └── sparse_unpack_arm32 # Binário estático ARM32
├── scripts/                # Scripts de controle e automação no tablet
│   ├── deploy_tablet.sh    # Deploy automático e atômico via ADB
│   ├── install-recovery-2.sh # Instalação de persistência no boot do tablet
│   ├── pack_sparse_template.py # Utilitário para empacotar templates ext4 esparsos
│   ├── switch_tv_mode.sh   # Alternador de modos com fail-safe rollback (Live/Cinema)
│   ├── switch_live.sh      # Atalho para retorno ao Live
│   ├── switch_vod.sh       # Alternador para Modo Cinema
│   ├── reconnect_usb.sh    # Soft-reset do barramento USB
│   ├── sync_youtube_cookies.sh # Sincronização de cookies YouTube do navegador
│   └── tv_watchdog.sh      # Daemon de monitoramento, wakelock e recuperação
├── tests/                  # Bateria de testes de validação unitária e stress
│   ├── test_fat32_integrity.py # Validação de integridade do template e geometria FAT32
│   ├── test_ntfs_extents_128g.c # Verificação de extents e runlists no NTFS de 128G
│   ├── test_probe_sar.py   # Testes do probe de aspect ratio / SAR anamórfico
│   ├── test_seek_reanchor.c # Simulação de ConnectShare probe isolation e re-ancoragem
│   ├── test_server_control_plane.py # Validação do CommandBus e despacho de comandos
│   ├── test_torrent_vod.py # Testes do pipeline VOD torrent
│   ├── test_torrent_vod_stress.py # Testes de estresse e resiliência de downloads VOD
│   └── test_vod_lock_and_elf.sh # Validação unitária de exclusão mútua e integridade ELF
├── templates/
│   └── ntfs_template.sparse.gz # Template esparso thin-provisioned de 128 GiB (~193 KB)
└── docs/                   # Documentação técnica detalhada
    ├── ARCHITECTURE.md     # Arquitetura do driver FUSE e pipeline
    ├── VOD_ARCHITECTURE.md # Pipeline de engenharia VOD Cinema V2 e prefetch
    ├── DEPLOYMENT_GUIDE.md # Guia passo a passo de instalação
    ├── HARDWARE_SPECS.md   # Especificações do decodificador Samsung MStar 2013
    ├── LIVE_EDGE_LOCK_AND_PROBE_ISOLATION.md # Teoria e testes de probe isolation
    └── TROUBLESHOOTING.md  # Diagnóstico e solução de problemas
```

---

## 🚀 Início Rápido

### 1. Servidor (Oracle VPS / Linux Server)

```bash
# 1. Copiar variáveis de ambiente e preencher credenciais
cp .env.example .env
nano .env

# 2. Iniciar o container Docker
docker compose up -d

# 3. Verificar logs
docker logs -f usb-stream-tv
```

Acesse o painel de controle pelo navegador: `http://<IP_DA_VPS>:8080` (ou via proxy reverso com SSL).

### 2. Dispositivo Android (Tablet / SBC)

Com o tablet conectado ao computador via ADB:

```bash
# Executar o deploy automatizado para o tablet
./scripts/deploy_tablet.sh 127.0.0.1:25555
```

O script cuidará de:
1. Enviar o template NTFS esparso de 128 GiB (`ntfs_template.sparse.gz`) e descompactar via `sparse_unpack_arm32` em `/data/local/tmp/ntfs_lab/ntfs_template.bin`.
2. Instalar os binários ARM32 estáticos (`fuse_ntfs`, `fuse_direct_arm32`, `stream_fetcher`, `patch_trp`) em `/system/xbin/`.
3. Configurar scripts de inicialização, wakelocks e governador de CPU para `performance`.
4. Conectar o LUN USB com identidade `LIVETV1` e expor `TV AO VIVO 2.tp` para a TV.

---

## 📖 Documentação Completa

- [Arquitetura Detalhada](docs/ARCHITECTURE.md)
- [Arquitetura VOD Cinema & Dual-Cache Prefetch](docs/VOD_ARCHITECTURE.md)
- [Guia de Implantação Passo a Passo](docs/DEPLOYMENT_GUIDE.md)
- [Especificações de Hardware da TV (MStar 2013)](docs/HARDWARE_SPECS.md)
- [Sincronismo de Borda Viva e Isolamento de Sondagem](docs/LIVE_EDGE_LOCK_AND_PROBE_ISOLATION.md)
- [Guia de Diagnóstico e Resolução de Problemas](docs/TROUBLESHOOTING.md)

---

## 📄 Licença

Uso pessoal e experimental dedicado à preservação e extensão de vida útil de televisores legados.
