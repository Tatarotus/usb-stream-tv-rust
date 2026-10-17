# Briefing de Engenharia: Investigação do Comportamento da Tecla Return na TV Samsung

Este documento consolida o histórico completo, as hipóteses formuladas pelo auditor externo (Sonnet 5.5), as alterações implementadas no driver FUSE NTFS, as evidências empíricas coletadas e o diagnóstico de causa raiz do congelamento da transmissão ao vivo após o acionamento da tecla **Return** na TV Samsung Plasma PL51F4000.

---

## 1. Arquitetura e Contexto do Projeto

* **Repositório**: `usb-stream-tv-prod` (versão de produção).
* **Topologia**:
  * **Servidor (VPS)**: Executa `server.py` (FastAPI + pipeline FFmpeg customizado com restamper monotônico de PTS/DTS/PCR).
  * **Tablet (Host OTG)**: Samsung Galaxy Tab 3 Lite (SM-T110, Android 4.4.2 KitKat, ARMv7-A 32-bit). Roda o driver de espaço de usuário `fuse_ntfs` montado em `/data/local/tmp/ntfs_lab/mnt` e exportado como backing image para o kernel Linux via USB Gadget Mass Storage (`/sys/class/android_usb/android0/f_mass_storage/lun0/file`).
  * **Cliente (Sink)**: TV Samsung Plasma PL51F4000 (ano 2013, chip MStar SoC, reprodução via ConnectShare USB).
* **Arquivo Emulado**: Sistema de arquivos NTFS sintético de 128 GB com arquivo de vídeo virtual contínuo `TV AO VIVO.trp`.
* **Buffer Circular**: Anel de 128 MB (`RINGSZ = 128 * 1024 * 1024`) gerenciado por `feeder_thread` no `fuse_ntfs`.

---

## 2. Invariantes Rígidos do Decodificador Samsung MStar 2013

Qualquer solução técnica deve respeitar as restrições físicas do SoC MStar:
1. **Zero B-Frames (`bframes=0`)**: Transmissão emulada via USB exige `-tune zerolatency`. B-frames causam underflow instantâneo do buffer CPB/VBV quando lidos via SCSI USB.
2. **Áudio AC-3 Dolby Digital**: MPEG-TS com áudio AAC trava a decodificação no firmware 2013.
3. **PCR Estrito a cada 20 ms**: Trava o PLL de clock STC a 27 MHz da TV.
4. **Comportamento ConnectShare (Modo Arquivo)**: A TV trata o pendrive como um **arquivo gravado estático**, e não como demodulador de broadcast (DVB/ATSC). No modo de arquivo, o decodificador MStar espera timestamps contínuos e crescentes; saltos abruptos de PTS/PCR sem que o usuário faça SEEK interativo pelo controle remoto causam travamento instantâneo.

---

## 3. O Problema Original Relatado

1. O usuário abre `TV AO VIVO.trp` e assiste por cerca de 1 minuto.
2. Pressiona a tecla **Return** no controle da TV (a TV encerra a tela de vídeo e volta para a listagem do ConnectShare).
3. Aguarda ~45 segundos na listagem.
4. Clica novamente em `TV AO VIVO.trp`.
* **Comportamento antes da intervenção**: A transmissão recomeçava e **repetia todo o trecho anterior gravado no anel** (replay fiel de 1 minuto), em vez de retomar na borda ao vivo atual.

---

## 4. Histórico da Investigação com o Sonnet 5.5

### Hipótese Formulada pelo Sonnet 5.5
* A TV não reabre o arquivo em `foff = 0` porque ela já mantém o cabeçalho inicial (~1.2 MB a 1.9 MB) em memória cache.
* Após uma lacuna temporal (gap $> 20\text{ s}$), se a TV retoma a leitura em um deslocamento diferente do último lido (`foff != prev_Fend`), trata-se de um **reinício de reprodução** (Return) e não de uma pausa simples.
* **Proposta do Sonnet (Prompt 5)**:
  * Detectar essa retomada com `RESUME_AFTER_GAP` (após $K=4$ blocos ou 64 KB lidos sequencialmente no novo ponto).
  * Forçar `is_reopen = 1` com motivo `RESUME_AFTER_GAP` para reancorar o leitor na borda ao vivo (`g_s_write - LEADBACK`).
  * Injetar `discontinuity_indicator = 1` no primeiro pacote PCR subsequente para instruir a TV a resetar o relógio STC.

---

## 5. Implementação Realizada no FUSE (`src/ntfs/fuse_ntfs.c`)

No commit do Prompt 5 foram adicionados:
1. **B1 — Fix do underflow de uint64 no splice**:
   `g_splice_birth_ms = now;` aplicado em ambos os pontos de ancoragem (`is_reopen` e `RING_WRAP_IN_LOOP`), evitando cancelamento prematuro por clock reverso.
2. **B2 — Discontinuidade estrita em 1 PCR**:
   `mark_pcr_discontinuity()` ajustado para marcar apenas o primeiro pacote PCR encontrado (`return 1;`).
3. **B3 — Log de Stall aprimorado**:
   Threshold ajustado para 9000 ms, registrando `foff`, `prev_Fend`, `delta` e `lead_bytes`.
4. **B4 — Rastreamento de `[META_BURST]`**:
   Acumulação e telemetria de rajadas de leitura no `g_meta_fd` (MFT e setor de boot).
5. **B5 — Motor `RESUME_AFTER_GAP`**:
   Monitora se `now - g_last_file_read_ms > g_resume_gap_ms` (20.000 ms). Se houver salto não adjacente (`|g_resume_start - g_resume_prev_fend| > 262144`), debita $K=4$ leituras consecutivas (64 KB) e dispara a nova âncora viva (`epoch++`).

---

## 6. Resultados do Teste 1 em Produção

* **Procedimento**: Assistir por 1 minuto $\to$ Apertar Return $\to$ Aguardar 45 segundos $\to$ Reabrir `TV AO VIVO.trp`.
* **Resultado Observado na TV**:
  > A TV entrou direto (sem exibir pop-up "Deseja continuar?"), **repetiu exatamente os mesmos ~2 segundos de áudio e vídeo do início de 1 minuto atrás e congelou completamente a imagem e o som**.

---

## 7. Análise da Telemetria (`/tmp/test_return_run1.log`)

A análise de 12.747 linhas de log do tablet revelou a sequência exata de eventos:

```text
1. SESSÃO INICIAL (17:30:28 - 17:31:43):
   - Reprodução normal sob epoch=1 (base=261.779.096).
   - Lidos 51 MB contínuos até foff=51.003.392 (prev_Fend=51003392).

2. INATIVIDADE / RETURN (17:31:43 - 17:32:48):
   - Gap total de 65.5 segundos sem leituras SCSI de vídeo.
   - Probes transitórios da TV no fim do arquivo (foff=12.8 GB a 128 GB) respondidos com NULL.
   - Zero leituras de MFT ou boot sector (off < 94208 foi vazio).

3. REABERTURA PELA TV (17:32:49):
   - A TV NÃO leu de foff=0 a foff=1.900.543! (Menor foff requisitado = 1.900.544).
   - Read 1 (foff=1.900.544): Servido sob epoch=1 (stream_pos=263.6 MB, PTS ~1003)
   - Read 2 (foff=1.916.928): Servido sob epoch=1 (stream_pos=263.7 MB, PTS ~1003)
   - Read 3 (foff=1.933.312): Servido sob epoch=1 (stream_pos=263.7 MB, PTS ~1003)
   - Read 4 (foff=1.949.696): RESUME_AFTER_GAP atinge 64 KB e dispara!
     [RESUME] reinicio detectado gap_ms=66471 start=1900544 prev_Fend=51003392 run_bytes=65536
     [FUSE] Fresh playback anchor at foff=1949696 -> stream_pos=340886488! epoch=2 reason=RESUME_AFTER_GAP
     [SPLICE] discontinuity_indicator em 1 pacotes PCR, stream_pos=340886488

4. AVANÇO E CONGELAMENTO (17:32:49 - 17:33:01):
   - O stream_pos saltou instantaneamente de 263.7 MB para 340.9 MB (+77.2 MB = +139 segundos no futuro).
   - A TV leu sequencialmente 15.5 MB do USB em 12 segundos (até foff=17.53 MB) enchendo sua fila SCSI.
   - O decodificador MStar consumiu os primeiros ~2 segundos da RAM da TV (PTS ~1000..1002).
   - Às 17:33:01.852, ao alcançar os bytes de foff=1.95 MB com PTS ~1142, o decodificador MStar colapsou.
   - A TV parou totalmente de emitir comandos SCSI READ (congelamento final).
```

---

## 8. Diagnóstico de Causa Raiz

1. **A TV retém o cabeçalho de 1.9 MB na memória RAM**:
   Ao pressionar Return, o player descarrega a interface, mas preserva em RAM os primeiros ~1.900.543 bytes associados àquele descritor de arquivo. Ao reabrir, esses 2 segundos são tocados diretamente da RAM sem consulta ao USB.
2. **Incompatibilidade do Splice In-Stream no ConnectShare**:
   Como a TV inicia consumindo dados velhos da RAM (`PTS ≈ 1000`) e o FUSE entrega a borda ao vivo em `foff = 1.95 MB` (`PTS ≈ 1142`), forma-se um descompasso de +139 segundos. Em modo de arquivo local USB, o SoC MStar 2013 não aceita salto de timestamps sem solicitação interativa do usuário, paralisando a renderização.

---

## 9. Como o Projeto Já Resolve Isso (O Mecanismo Anti-Cache Triple-Tier)

No projeto de produção, a troca de canal e a reinicialização da TV ao vivo funcionam perfeitamente através de `scripts/switch_tv_mode.sh` porque acionam o **Anti-Cache Triple-Tier**:
1. `patch_trp`: Alterna o nome do arquivo no MFT (`TV AO VIVO.trp` $\leftrightarrow$ `TV AO VIVO 2.tp`).
2. Randomização do *Volume Serial Number* do setor de boot NTFS.
3. Alternância da *Inquiry String* SCSI USB (`LIVETV1` $\leftrightarrow$ `LIVETV2`) com ciclo rápido de reconexão do USB Gadget (`enable=0` $\to$ `enable=1`).

Quando o pendrive reconecta com um novo nome de arquivo, a TV é **obrigada a purgar toda a memória RAM**, lendo `foff = 0` limpo diretamente da borda ao vivo, eliminando qualquer salto de PTS.

---

## 10. Propostas e Caminhos para Discussão

### Caminho 1: Watchdog Anti-Cache no Tablet (Transparente para o Usuário)
* O FUSE ou o `scripts/tv_watchdog.sh` detecta inatividade de leituras de vídeo por $> 15\text{ s}$ (usuário apertou Return e está na tela de arquivos).
* Em segundo plano, o tablet executa a alternância de nome (`patch_trp`) e o ciclo rápido da LUN USB (`enable=0` $\to$ `enable=1`).
* Ao clicar no arquivo atualizado na tela, a TV lê a partir de `foff = 0` diretamente do ao vivo, sem travamentos.

### Caminho 2: Modo Timeshift Contínuo sem Reancoragem
* Não tentar reancorar para a borda ao vivo no Return caso o ponto requisitado esteja dentro do anel circular de 128 MB.
* O FUSE atende a leitura de onde ela parou mantendo a continuidade estrita de timestamps. A TV não trava, mas opera com o atraso acumulado da pausa (Timeshift).

---

## 12. Caminho C — Emenda Contínua no FUSE (IMPLEMENTADO)

Escolhido o Caminho C: em vez de marcar descontinuidade, o FUSE **reescreve a linha do tempo** do
trecho novo para continuar exatamente de onde a TV parou. Implementado em `src/ntfs/ts_xform.h`
(portado do `SeamlessRestamper` do `server.py`) e integrado ao `serve_live_backend()`.

### 12.1 Correções de causa raiz aplicadas

1. **Parte B — fronteira de pacote na emenda.** Antes de trocar a âncora, calcula-se
   `pad = (188 - old_pos % 188) % 188` a partir do mapeamento antigo; os `pad` bytes do anel são
   entregues crus e a nova âncora passa a ser `anchor_foff = foff + pad`. O byte imediatamente
   após a emenda é sempre `0x47` numa fronteira de pacote — o demux do MStar re-sincroniza sem
   perder os pacotes PAT/PMT/SPS. No teste com dados reais: `pad=80`, junção no packet 46712.
2. **Parte C — transformação de continuidade.** Um offset constante por época
   (`delta_pts`, `delta_pcr`, `cc_add[pid]`) é aplicado a PCR, PTS/DTS de PES de vídeo/áudio e ao
   CC de cada PID. Pacotes NULL (PID `0x1FFF`) nunca são tocados. A transformação é determinística
   por posição: reler o mesmo trecho devolve os mesmos bytes.
3. **Estado servido.** `g_served` guarda o último PTS/PCR/CC **entregue** à TV e só é atualizado em
   leituras sequenciais (ou na primeira da época) — probes jamais o alteram.
4. **Pacote atravessando leituras FUSE.** `131072 = 697*188 + 56`, então um pacote por leitura
   atravessa a fronteira. A cabeça é reescrita no momento em que é servida e o restante quando
   completa (`struct tsx_tail`). Sem isso, um pacote por leitura manteria PTS/PCR/CC crus.

### 12.2 Resultado da validação (`tests/test_capture_splice.c` + `src/tools/ts_check.py`)

Captura real de ~100 MB de `http://tv.smre.run.place/stream` (h264 + AC-3), 30 MB limpos no anel:

| Cenário | sync_loss | saltos PCR/PTS | erros CC | Veredito |
|---|---|---|---|---|
| Troca de canal (`switch`) | 0 | **0** | **0** | `[OK] timeline continuous` |
| Return → 66 s → reabrir (`return`) | 0 | 1 (+1,167 s, **no re-open**, packet 44620) | 6 (todos no re-open) | emenda limpa |

Na emenda em si: **delta de PTS de exatamente 3000 ticks (33 ms)** e junção em fronteira de pacote.
O salto remanescente no modo `return` é o *seek* do próprio TV ao reabrir 4000 pacotes à frente —
servido cru pela âncora antiga (leituras 1-3) e não pela emenda; é o mesmo tipo de salto que o
MStar já tolera em qualquer seek.

### 12.3 Controles de ambiente

* `FUSE_CONT_SPLICE=0` desliga a emenda contínua (volta ao marcador `discontinuity_indicator`).
* `FUSE_RESUME_GAP_MS=<ms>` ajusta o limiar do `RESUME_AFTER_GAP` (padrão 20000).
* `FUSE_SPLICE_FLAG=0` desliga o marcador de descontinuidade.

### 12.4 Testes

* `tests/test_cont_splice.c` — 102 checks: unidade do motor, splice puro A→B, replay do fluxo
  real do Return (`foff=1900544`, o mesmo do log), guarda de referência `[3b]`/`[3c]`, isolamento
  de probes, restart em `foff=0`, wrap de 33 bits e carry entre leituras.
* `tests/test_capture_splice.c` — ponta a ponta com captura real (modos `tvview`, `return` e `switch`).
* `src/tools/ts_check.py` — verificador de timeline sem dependências (saltos de PCR/PTS, CC, sync).

### 12.5 Prompt 7 — guarda de referência, cenário fiel à TV e auditoria 32 bits

1. **Higiene (T1).** Merge confirmado linha a linha: `!is_future_probe && !is_behind_probe` no
   `is_confirmed_jump`, early-return de NULLs sem `!is_confirmed_jump`, resposta de erro FUSE com
   `struct fuse_out_header` (sem `fill_null`), `g_splice_birth_ms = now` (2 pontos), `[STALL]` em
   9000 ms, ramo `RESUME_AFTER_GAP`, `[META_BURST]`. Branch local `fix/return-continuity` criada;
   binário implantado passa a ser reproduzível por commit (antes saía de stash).
2. **Cenário "o que a TV enxerga" (T2).** Novo modo `tvview`: sessão 1 de 12 MB em leituras de
   16 KB (o tamanho real do ConnectShare), RAM = `[0, 1900544)`, gap de 66 s, reabertura em
   `F_resume = 1900544` (não múltiplo de 188, como no log real), 3 leituras cruas + emenda
   (pad incluso) + 8 MB. Resultado com captura real de 43 MB:
   `sync_loss 0, saltos PCR/PTS 0, CC 0` — `[OK] timeline continuous`. Junção auditada
   pacote a pacote: fronteira `0x47`, **delta de PTS exatamente 3000 ticks**, 0 erros de CC antes
   e depois. `ffmpeg`: 9 avisos no `tv_stream` (classes de startup por corte no meio de GOP,
   as mesmas do original com 3) contra 3 no original; região da emenda decodifica limpa.
   `ffprobe` (pacotes de vídeo): 560/560 PTS monotônicos no `tv_stream`, 2408/2408 no original.
3. **Guarda de estado de referência (T3).** Novo `g_served_upd_pos` (stream_pos do último avanço
   de PTS observado; nunca probes). Na calibração, se `old_pos - upd_pos` sai de `[0, 256 KB]`,
   a referência vem do anel (`tsx_collect_last`: último PTS/PCR/CC até 512 KB para trás,
   convertido para valores entregues com o transform atual), com log
   `[XFORM] ref=served|ring age_bytes=%lld`. Correção junto: o pacote completado pelo `pad`
   **é** decodificado pela TV (byte stream contínuo), então sua cauda passou a ser reescrita
   com o transform antigo e o pacote dobrado ao `served` (referência mais fresca possível).
   Testes novos `[3b]` (leituras cruas sem PES) e `[3c]` (`served` envenenado 73 s à frente):
   ambos tomam `ref=ring` e entregam delta 3000; fluxo normal toma `ref=served age=0`.
   Bug real achado no caminho: `tsx_transform_fields` limitava o *parse* à janela de escrita,
   então um PTS na cauda de pacote nunca era reescrito (1 pacote cru por leitura partida);
   agora parse é total e só a escrita respeita a janela — sem duplo-shift (verificado byte a byte).
4. **32 bits (T4).** Auditoria manual completa: nenhum `long`/`unsigned`/`size_t`/`int`/`time_t`
   segura PTS/DTS (33 bits), PCR (até 2³³·300) ou deltas — tudo `uint64_t`/`int64_t`; `size_t`
   só em offsets/contagens de bytes, `int` só em flags/nibbles/índices; shifts precedidos de cast
   para `uint64_t` (sem UB); constantes exatas (2³³, 2³³·300, 3000). Execução 32-bit **inviável
   neste host** (sem `stubs-32.h`/multilib, sem `lib32`, sem `qemu-arm`, sem cross-compiler;
   nada instalado por restrição do prompt) — fica para o host de build junto ao ARM32.
   Comando exato para o host de build (mesmas flags do cabeçalho + estático):
   `arm-linux-gnueabihf-gcc -Wall -Wextra -Wconversion -Wsign-conversion -Werror -O2 -static src/ntfs/fuse_ntfs.c -lpthread -o src/ntfs/fuse_ntfs_arm32`
   (binário anterior gerado lá tinha 724.864 bytes; **não gerar aarch64 — o tablet é ARMv7**).
5. **Nota sobre `/tmp/test_return_run1.log`.** O log (12.747 linhas, em `/tmp`) foi removido na
   limpeza entre sessões; a análise da Parte A permanece registrada nos relatórios e acima
   (última leitura 17:33:01.852, zero leituras até 17:37:20+ → travamento permanente, não espera).

**Deploy bloqueado até 'ok' do usuário** (regra vigente): só após `tvview` com zero saltos/zero CC
(já obtido) **e** testes em 32 bits no host de build.

### 12.6 Prompt 9 — pacing global, checkpoints RAM, scar-skip e fallback (IMPLEMENTADO)

Motivação (teste real 18:33): sessão+gap de ~5 min fez o anel passar por cima da âncora
antiga (`old_pos=103M < ring_old=124M`) → salto de ~5,5 min travou em 2 s na RAM; mais
uma cicatriz de 56 s do upstream junto à âncora; mais o stall de 42 s na borda (TV leu
~1,1 MB/s contra feeder ~550 KB/s e zerou o colchão — o clamp de 50 ms por leitura
deixava leituras de 128 KB escaparem a 2,5 MB/s; a serving do FUSE é serial, então a
concorrência nunca foi o mecanismo, e sim o clamp).

1. **T1 (medição/auditoria, log-only no server):** botão CNN = `playChannel` → POST
   `/api/switch` → `HUB.switch_channel` (make-before-break com `start_new_channel()`,
   mesma conexão HTTP — por isso sem reconnect no fetcher); já-sintonizado retorna sem
   log (adicionado `SWITCH_NOOP`) e `SWITCH_OK` agora traz o tempo total. Deploy do
   server.py no VPS pendente (só log, mas exige restart do stream).
2. **T2 (token bucket global):** janela compartilhada de 250 ms sob `g_mu`; tetos por
   faixa de lead inalterados; sono em fatias ≤50 ms (cada uma respeita AGENTS.md §2.2,
   total por leitura pode passar para leituras grandes — longe de timeouts SCSI).
3. **T3 (borda limitada):** espera no live edge até ~500 ms, depois NULL-fill paced.
4. **T4 (checkpoints RAM):** 64 slots indexados `[0,4MB)@64KB` com `fend` armazenado
   (guarda contra overwrite pelo mesmo slot) e validade de 30 min; limpeza em
   open/FLUSH/FOFF0/troca de canal. Ordem: served-fresco → ckpt → scan → served-velho.
5. **T5 (scar-skip):** varre 2 MB do head; salto de PTS > 5 s → re-snap após +1 MB
   (`[XFORM] scar_skip`, uma tentativa).
6. **T6 (fallback Caminho A):** `fuse_should_resync()` (conteúdo sumido + sem checkpoint)
   + contador de behind-NULLs sustentados (≥4, só behind — future-probes nunca): tocam
   `/data/local/tmp/resync_usb`; watchdog recicla o stack live (cooldown 120 s, só live).
   Descoberto no caminho: reabertura ≥8 MB após wrap caía no early-return de NULLs para
   sempre sem pedir nada (deadlock silencioso pré-existente) — agora pede ressincronia.
7. **Testes (T7):** suíte 106 → 121 checks (`[8]` wrap+ckpt com 140 MB de filler,
   `[9]` cicatriz de 60 s injetada, `[10]` fallback com NULLs + `g_resync_requests≥1`);
   modo `mt` (4 threads: pacing compartilhado comprovado por piso de tempo, passthrough
   idêntico ao anel); `tvview`/`switch` com `[OK] timeline continuous`. ARM32 estático
   refeito no host de build (738.240 bytes, `-Werror` limpo).


---

## 11. Arquivos Relevantes para Inspeção

* Driver FUSE: `src/ntfs/fuse_ntfs.c`
* Motor de emenda contínua (Caminho C): `src/ntfs/ts_xform.h`
* Alternador de Modos e Anti-Cache: `scripts/switch_tv_mode.sh`
* Watchdog Operacional: `scripts/tv_watchdog.sh`
* Utilitário MFT: `src/tools/patch_trp.c`
* Verificador de timeline MPEG-TS: `src/tools/ts_check.py`
* Testes da emenda: `tests/test_cont_splice.c`, `tests/test_capture_splice.c`
* Log Completo do Teste 1: `/tmp/test_return_run1.log` (12.747 linhas)

