# AGENT HANDOFF: USB-STREAM-TV RUST MIGRATION & DEPLOYMENT

> **Data de Atualização**: 2026-10-17  
> **Workspace**: `/home/sam/Code/usb-stream-tv-rust`  
> **Branch / Commit**: `master` (`339d189` ou superior, working tree limpa)  
> **Target de Hardware**: Tablet Samsung Galaxy Tab 3 Lite (SM-T110, Exynos 3470 / PXA986 ARMv7-A 32-bit, Android 4.4.2 KitKat, Kernel Linux 3.4.5)  
> **Host USB**: TV Plasma Samsung 51" (PL51F4000, SoC MStar MSD6A801, ConnectShare Movie 2013)  
> **Modo de Conexão**: USB Mass Storage (UMS) Gadget via driver `/sys/class/android_usb/android0`  

---

## 1. Visão Geral Executiva

O projeto **usb-stream-tv** foi completamente reescrito de **C (legado)** para **Rust seguro e estático** para eliminar falhas de concorrência, deadlocks, vazamentos de memória e panics em tempo de execução no tablet Android que atua como emulador de pendrive para a TV Samsung de 2013.

### Status Atual:
- **100% dos 5 crates em Rust compilados estaticamente para ARMv7** (`armv7-unknown-linux-gnueabihf`, `-C target-feature=+crt-static`, `panic = "abort"`).
- **136/136 testes em Rust passando** (unitários e de integração de seek/re-anchor).
- **32/32 testes Python VOD passando**.
- **Binários e scripts operacionais instalados em `/system/xbin` no tablet**.
- **Modo TV Ao Vivo ATIVO e rodando em produção no tablet**:
  - `fuse_ntfs` ativo servindo imagem virtual de 128 GiB (`tv_stream.img`).
  - `stream_fetcher` ativo transferindo MPEG-TS de `tv.smre.run.place` com 0 drops de sync.
  - Anel de memória de 128 MB em buffer contínuo.
  - USB Gadget conectado com Inquiry String `LIVETV1`.

---

## 2. Topologia do Sistema e Infraestrutura

```
   [VPS Oracle Cloud] (129.146.5.64)
          ▲
          │ Chisel Reverse Tunnel (porta 25555)
          ▼
   [Tablet Samsung SM-T110] (Android 4.4.2 / Kernel 3.4.5)
      ├── /system/xbin/stream_fetcher ──► Baixa MPEG-TS via HTTP (porta 80)
      │                                       │
      │                                       ▼ FIFO (/data/local/tmp/live_pipe)
      ├── /system/xbin/fuse_ntfs ────────► Feeder Loop (128 MB Ring Buffer)
      │        │                               │
      │        ▼ /dev/fuse                     ▼ Caminho C (ts_xform + D12 Snapper)
      │   /data/local/tmp/ntfs_lab/mnt/tv_stream.img (128 GiB virtual)
      │        │
      │        ▼ UMS Driver (lun0/file)
      └── USB OTG Gadget (/sys/class/android_usb/android0)
               │
               ▼ Cabo USB Físico
   [TV Samsung PL51F4000] (MStar ConnectShare 2013)
```

---

## 3. Os 5 Crates em Rust (`crates/`)

| Crate | Função | Localização | Testes | Binário no Tablet |
|---|---|---|---|---|
| `fuse_ntfs` | Daemon `/dev/fuse` para TV Ao Vivo e Favoritos. Monta sistema de arquivos NTFS virtual de 128 GiB, implementa motor Caminho C, D12 Snapper, Live Edge Lock e anel circular de 128 MB. | [`crates/fuse_ntfs`](file:///home/sam/Code/usb-stream-tv-rust/crates/fuse_ntfs) | 108 pass | `/system/xbin/fuse_ntfs` (772 KB) |
| `fuse_direct` | Daemon `/dev/fuse` para Cinema / VOD. Emula FAT32 com Direct-I/O sobre arquivo MP4 remoto, com alinhamento de fase MPEG-TS e anti-cache VSN. | [`crates/fuse_direct`](file:///home/sam/Code/usb-stream-tv-rust/crates/fuse_direct) | 9 pass | `/system/xbin/fuse_direct` (841 KB) |
| `stream_fetcher` | Cliente HTTP resiliente. Conecta no stream da VPS, sincroniza pacotes de 188 bytes (`0x47`), descarta cabeçalhos/lixo e alimenta o FIFO `/data/local/tmp/live_pipe`. | [`crates/stream_fetcher`](file:///home/sam/Code/usb-stream-tv-rust/crates/stream_fetcher) | 7 pass | `/system/xbin/stream_fetcher` (821 KB) |
| `patch_trp` | Ferramenta in-place para alternar carimbos e nomes de arquivo TRP no template NTFS, forçando a TV Samsung a invalidar o cache de diretório. | [`crates/patch_trp`](file:///home/sam/Code/usb-stream-tv-rust/crates/patch_trp) | 5 pass | `/system/xbin/patch_trp` (673 KB) |
| `sparse_unpack` | Utilitário para descompactar imagens esparsas ext4 do Android em arquivos brutos. | [`crates/sparse_unpack`](file:///home/sam/Code/usb-stream-tv-rust/crates/sparse_unpack) | 7 pass | `/system/xbin/sparse_unpack` (650 KB) |

---

## 4. Invariantes Críticos e Regras do Chip MStar 2013

Qualquer agente que for modificar o código Rust DEVE respeitar estes invariantes de hardware:

1. **Alinhamento Estrito de 8 Bytes no ARMv7-A (`AlignedFuseBuf`)**:
   - No kernel Linux 3.4 de 32-bit, buffers FUSE devem estar alinhados a 8 bytes (`#[repr(C, align(8))]`).
   - Alocações devem usar `AlignedFuseBuf::new_boxed()` (via `std::alloc::alloc_zeroed`) diretamente na heap para evitar estourar a stack de 128 KB da thread.
2. **Higiene de Memória no Protocolo FUSE (`out_buf.fill(0)`)**:
   - Todas as estruturas de resposta (`fuse_entry_out`, `fuse_attr_out`, etc.) devem ser zeradas antes do envio para evitar vazamento de bytes de vídeo para campos reservados/padding do kernel.
3. **Handshake FUSE_INIT Legado (Kernel 3.4 / ABI 7.18)**:
   - Se `minor < 23`, a resposta deve ser truncada para `OUT_HDR_SZ + 24` bytes. Respostas maiores causam rejeição do handshake pelo kernel Android 4.4.
4. **Caminho C de Transformação MPEG-TS (`ts_xform.rs` & `snapper.rs`)**:
   - Reescrita determinística de PTS/DTS (módulo $2^{33}$), PCR (módulo $2^{33} \times 300$) e Continuity Counter (módulo 16 por PID).
   - Mantém `tail carry` entre blocos de leitura de 128 KiB ($131072 \pmod{188} = 56$ bytes).
5. **Invariante `last_fend` em Falsos Probes e Starvation**:
   - É terminantemente proibido avançar `last_fend` durante starvation (`to_copy == 0`) ou preenchimento com pacotes NULL. `last_fend` só avança quando `to_copy > 0`.
6. **Live Edge Lock sem "Efeito Borracha"**:
   - Se o leitor da TV encostar na borda de escrita (`mapped_spos >= cur_write`), o motor FUSE bloqueia cooperativamente no `wait_edge` com timeout de 500ms (`EDGE_WAIT_BUDGET_MS = 500`). Nunca deve re-ancorar para trás se o offset estiver à frente de `s_write`.
7. **Sem `pkill -USR1` que mate o processo**:
   - `fuse_ntfs` ignora `SIGUSR1` (`libc::signal(libc::SIGUSR1, libc::SIG_IGN)`). Sinais de flush USB são feitos via arquivo `/data/local/tmp/fuse_flush`.

---

## 5. Scripts Operacionais (`scripts/` e `/system/xbin/`)

- [`scripts/switch_tv_mode.sh`](file:///home/sam/Code/usb-stream-tv-rust/scripts/switch_tv_mode.sh): Orquestrador principal de modos.
  - `switch_tv_mode.sh live`: Ativa TV Ao Vivo (NTFS 128 GiB).
  - `switch_tv_mode.sh vod <task_id>`: Ativa Cinema / VOD (FAT32 Direct-I/O).
  - `switch_tv_mode.sh favorites`: Ativa modo Multi-Canais.
  - `switch_tv_mode.sh status`: Exibe status do UMS, FUSE, Fetcher e Watchdog.
- [`scripts/switch_live.sh`](file:///home/sam/Code/usb-stream-tv-rust/scripts/switch_live.sh): Wrapper rápido para TV Ao Vivo.
- [`scripts/switch_vod.sh`](file:///home/sam/Code/usb-stream-tv-rust/scripts/switch_vod.sh): Wrapper para modo Cinema.
- [`scripts/reconnect_usb.sh`](file:///home/sam/Code/usb-stream-tv-rust/scripts/reconnect_usb.sh): Força re-enumeração SCSI no host MStar alternando `enable` e `inquiry_string` (`LIVETV1` $\leftrightarrow$ `LIVETV2`).
- [`scripts/tv_watchdog.sh`](file:///home/sam/Code/usb-stream-tv-rust/scripts/tv_watchdog.sh): Daemon em background que monitora a saúde dos daemons FUSE e recria pipes se necessário.
- [`scripts/install-recovery-2.sh`](file:///home/sam/Code/usb-stream-tv-rust/scripts/install-recovery-2.sh): Script de persistência de boot do Android para inicialização automática no tablet.

---

## 6. Procedimento de Acesso e Operação no Tablet

### Túnel SSH / ADB
O tablet não possui IP público direto; ele se conecta à VPS Oracle através de um túnel reverso Chisel na porta `25555`.

Para executar comandos no tablet a partir da máquina local:
```bash
# Verificar status do modo TV
ssh oracle "adb -s 127.0.0.1:25555 shell \"su -c '/system/xbin/switch_tv_mode.sh status'\""

# Ver telemetria do daemon NTFS
ssh oracle "adb -s 127.0.0.1:25555 shell \"tail -n 25 /data/local/tmp/ntfs_lab/fuse_ntfs.log\""

# Ver telemetria do stream fetcher
ssh oracle "adb -s 127.0.0.1:25555 shell \"tail -n 20 /data/local/tmp/ntfs_lab/stream_fetcher.log\""

# Forçar alternância para Live TV
ssh oracle "adb -s 127.0.0.1:25555 shell \"su -c '/system/xbin/switch_tv_mode.sh live'\""

# Forçar alternância para VOD
ssh oracle "adb -s 127.0.0.1:25555 shell \"su -c '/system/xbin/switch_tv_mode.sh vod <task_id>'\""
```

> **Atenção ao ADB Double-Tunnel Trap**:
> Nunca inicie um servidor ADB local conectando via encaminhamento de porta SSH se o servidor ADB da VPS já estiver conectado a `127.0.0.1:25555`. Comunique-se sempre executando `adb` dentro da sessão da VPS (`ssh oracle "adb -s 127.0.0.1:25555 ..."`).

---

## 7. Como Compilar os Binários Rust (Cross-Compilation)

A compilação cruzada para o ARMv7-A é feita na máquina local:

```bash
cd /home/sam/Code/usb-stream-tv-rust

# Toolchain oficial configurado em .cargo/config.toml:
# target = "armv7-unknown-linux-gnueabihf"
# linker = "arm-none-linux-gnueabihf-gcc"

# Compilar todos os crates em release estático:
cargo build --release --target armv7-unknown-linux-gnueabihf

# Os binários gerados ficam em:
# target/armv7-unknown-linux-gnueabihf/release/{fuse_ntfs,fuse_direct,stream_fetcher,patch_trp,sparse_unpack}
```

---

## 8. Testes Automatizados

```bash
cd /home/sam/Code/usb-stream-tv-rust

# Rodar todos os testes de unidade e integração Rust (136 testes):
cargo test --workspace

# Rodar os testes de integração do FUSE NTFS:
cargo test -p fuse_ntfs --test integration_seek_reanchor

# Rodar os testes da suíte Python VOD (32 testes):
pytest tests/
```

---

## 9. Próximos Passos Imediatos e Tarefas Pendentes

1. **Validação Física na TV Samsung**:
   - Ligar a TV PL51F4000.
   - Acessar o menu **ConnectShare / Media Play** $\rightarrow$ dispositivo **`LIVETV1`**.
   - Abrir **`TV AO VIVO.trp`**.
   - Testar pausas/retomadas com o botão **RETURN** do controle remoto (validando se a emenda de época Caminho C ocorre sem saltos visíveis ou re-buffering).
2. **Sincronização com VPS Oracle / OpenRig** (agendada pelo usuário após o teste da TV):
   - Configurar o OpenRig na VPS com `aider-argon` + cookies de autenticação + `pi` configurado com `opencraft` usando Claude Sonnet 3.5/Sonnet 5.
   - Sincronizar scripts e dashboard web atualizados.
3. **Observância de Diretórios**:
   - Manter `/home/sam/Code/usb-stream-tv-prod` estritamente somente-leitura (legado C intocado para fins de comparação histórica).
   - Todo o desenvolvimento deve permanecer confinado a `/home/sam/Code/usb-stream-tv-rust`.
