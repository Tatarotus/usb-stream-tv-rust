# Diretrizes de Governança para Agentes de IA — USB-Stream-TV Production

Este documento estabelece as regras obrigatórias e inegociáveis para qualquer agente de IA que opere, modifique ou mantenha a codebase de produção em `~/Code/usb-stream-tv-prod`.

---

## 1. Regra de Ouro: Higiene Estrita da Codebase

1. **Zero Arquivos Soltos na Raiz**:
   - É **terminantemente proibido** criar scripts temporários de teste, benchmarks pontuais, reproduções ou dumps na raiz do projeto (ex.: `test_*.py`, `reproduce_*.py`, `debug_*.sh`, `dump.bin`).
   - Todos os scripts e testes de diagnóstico pontuais devem residir exclusivamente no diretório temporário do agente (`scratch/`) ou ser executados via comandos one-liner inline no terminal.
2. **Estrutura de Diretórios Protegida**:
   - `server.py`, `Dockerfile`, `compose.yaml`, `channels.json`: Raiz do projeto.
   - `dashboard.html`, `manifest.json`, `sw.js`, ícones PWA: Raiz do projeto (servidos diretamente pelo servidor web).
   - `src/ntfs/`: Código-fonte C e binários do driver FUSE NTFS (`fuse_ntfs.c`, `fuse_ntfs.h`, `fuse_ntfs_arm32`).
   - `src/client/`: Ingestão de rede ultraleve e VOD Direct (`stream_fetcher.c`, `stream_fetcher_arm32`, `fuse_direct_v2.c`, `fuse_direct_arm32`).
   - `src/tools/`: Utilitários de sistema de arquivos e MFT (`patch_trp.c`, `patch_trp_arm32`, `sparse_unpack.c`, `sparse_unpack_arm32`).
   - `scripts/`: Automações operacionais do tablet (`deploy_tablet.sh`, `switch_tv_mode.sh`, `switch_live.sh`, `switch_vod.sh`, `reconnect_usb.sh`, `tv_watchdog.sh`, `pack_sparse_template.py`).
   - `templates/`: Templates compactados (`ntfs_template.sparse.gz`). **Nunca commitar arquivos `.bin` descompactados de 128 GB.**
   - `docs/`: Documentação técnica completa.
3. **Limpeza de Artefatos em Git**:
   - Antes de concluir qualquer tarefa, inspecione `git status`. Remova qualquer arquivo não rastreado que não faça parte formal da arquitetura de produção.
   - O `.gitignore` deve ser sempre respeitado.
4. **Segurança e Sigilo de Credenciais (Zero Secret Leaks)**:
   - É **estritamente proibido** commitar credenciais reais, tokens, senhas ou usuários em arquivos rastreados pelo Git.
   - O arquivo `.env` é reservado exclusivamente para credenciais locais e NUNCA deve ser commitado (protegido no `.gitignore`).
   - Forneça sempre `.env.example` com valores ilustrativos seguros para referência pública no GitHub.
   - Em `channels.json`, URLs de provedores privados devem utilizar estritamente os placeholders `{XTREAM_UPSTREAM}`, `{XTREAM_USER}` e `{XTREAM_PASS}`, que são resolvidos dinamicamente em tempo de execução pelo `server.py`.

---

## 2. Invariantes Técnicos Mandatórios (Decodificador Samsung MStar 2013)

Qualquer alteração no pipeline de vídeo (`server.py`), no driver FUSE (`fuse_ntfs.c`) ou nos scripts de USB deve respeitar estritamente as limitações físicas do decodificador de hardware da TV Samsung Plasma PL51F4000:

### 2.1. Pipeline de Vídeo e Áudio no FFmpeg (`server.py`)
- **Zero B-Frames (`bframes=0`)**: Transmissões ao vivo via emulação USB **devem** ser codificadas com `-tune zerolatency` (`bframes=0`). B-frames causam underflow instantâneo do buffer CPB/VBV no chip MStar quando lidos via SCSI USB com `muxdelay` baixo, congelando a imagem no primeiro frame.
- **Áudio AC-3 Dolby Digital Obrigatório**:
  ```bash
  -c:a ac3 -b:a 384k -ar 48000 -ac 2
  ```
  O firmware 2013 da Samsung não reproduz de forma estável streams de áudio AAC dentro de contêineres MPEG-TS.
- **Intervalo de PCR Estrito**:
  ```bash
  -pcr_period 20
  ```
  Garante que pacotes contendo PCR sejam emitidos a cada 20 ms para travar o PLL de clock STC a 27 MHz da TV.
- **Estrutura de Quadros (GOP)**:
  `-g 30 -keyint_min 30 -sc_threshold 0` com `-x264-params repeat-headers=1`. Um quadro IDR completo com SPS e PPS deve ser entregue exatamente a cada 1.0 segundo.
- **Fatia Única por Quadro**: Nunca repassar transmissões IPTV multi-slice (2 ou mais fatias por frame) sem transcodificar para `slices=1`.

### 2.2. Motor FUSE NTFS (`src/ntfs/fuse_ntfs.c`)
- **Zero Throttle no Feeder de Rede**:
  O ring buffer de 128 MB (`RINGSZ`) é circular e auto-recuperável. O `feeder_thread` **nunca deve sofrer estrangulamento artificial (`usleep`/lead limits)**; deve ler livremente do pipe de rede. Se o decodificador pausar, o writer simplesmente sobrescreve dados antigos. Se o leitor cair atrás de `ring_old`, a re-ancoragem para `live_target` (`g_s_write - LEADBACK`) ocorre de forma automática e transparente.
- **Condição de Re-ancoragem e Debounce de Salto ($K \ge 3$)**:
  Re-ancoragens ocorrem exclusivamente em:
  1. Abertura inicial ou reinício explícito em `foff == 0` com delta temporal $> 1500\text{ ms}$.
  2. Queda do leitor fora da janela do anel circular (`s0 < ring_old`).
  3. Salto para frente ou retomada de bookmark confirmados: exige $K \ge 3$ blocos sequenciais consecutivos fora da cadeia ativa de reprodução (`g_probe_consecutive_count >= 3`). Probes transitórios (1 a 2 blocos) jamais alteram a âncora ativa.
- **Isolamento Estrito de Probes e Imutabilidade de Cadeia**:
  Leituras não-sequenciais em `foff >= 8MB` que excedam a área escrita em mais de 2 MB retornam pacotes MPEG-TS nulos (`PID 0x1FFF: 0x47, 0x1F, 0xFF, 0x10, 0xFF...`) imediatamente. **É terminantemente proibido atualizar `g_prev_Fend` no caminho de entrega de NULLs**, preservando a continuidade ininterrupta do stream ativo.
- **Pacing Suave Clamped $\le 50\text{ ms}$**:
  Todas as pausas de regulação de taxa (`usleep`) no caminho de leitura FUSE devem ser estritamente clampeadas a $\le 50\text{ ms}$ com mutex `g_mu` destravado durante o sono, prevenindo timeouts do barramento SCSI USB do chip MStar.
- **Sincronismo de Borda Viva (Live Edge Lock)**:
  Quando o leitor alcança a borda do escritor (`start >= g_s_write`), ele deve aguardar cooperativamente em `wait_step()` via `pthread_cond_broadcast(&g_cv)`, sincronizando a entrega com o clock de rede do FFmpeg sem inserção desnecessária de pacotes nulos.

### 2.3. Subsistema Anti-Cache Triple-Tier
Qualquer reinicialização ou troca para Live TV deve garantir o acionamento dos 3 níveis anti-cache:
1. Executar `patch_trp` para alternar os metadados MFT entre `TV AO VIVO.trp` e `TV AO VIVO 2.tp`.
2. Randomizar o *Volume Serial Number* do setor de boot NTFS (offset `0x48`).
3. Alternar a *Inquiry String* SCSI USB entre `LIVETV1` e `LIVETV2`.

### 2.4. Medição e verificação de fontes (REGRA BÁSICA)

**Comando de referência**, aplicado ao **arquivo capturado** (nunca à conexão viva):

```bash
# vídeo e áudio decodificados para descarte; NUNCA `-f null`
ffmpeg -hide_banner -nostdin -v error -threads 1 -i <captura.ts> \
       -map 0:v:0 -f rawvideo -y /dev/null \
       -map 0:a:0 -f s16le   -y /dev/null
```

1. **Uma thread e saídas cruas.** `-threads 1` fixa o caminho de decodificação;
   `-f null` é proibido porque o **estágio de muxagem** emite por conta própria
   `non monotonically increasing dts`. Isso é **medido**: a linha desaparece quando
   o muxer sai do caminho; ela é ruído de ferramenta, não dano no fluxo.
   Atenção: `-f rawvideo` não carrega trilha de áudio — mapear áudio para rawvideo
   faz o ffmpeg falhar com `Encoder not found` (rc 8), erro de ferramenta que
   **não** é veredito sobre o stream. Áudio vai em `s16le`, em seu próprio
   `/dev/null`.
2. **Medir sempre de arquivo salvo**, com a captura crua preservada, para que a
   verificação seja repetível. Veredito tirado só da conexão viva não é evidência.
3. **Zero linhas em `-v error` é critério conservador de TRIAGEM**, não prova de
   qualidade nem de estabilidade. Uma amostra com `POC`/`SPS` repetidos é
   **reprovada por precaução enquanto se investiga** — a reprovação é da amostra,
   não uma conclusão sobre a origem do defeito.
4. **Classificar a falha pelo estágio**, separadamente: decodificação (ex.: `POC`,
   `SPS`, `corrupt`), demuxagem/contêiner (ex.: `PES`, `TS`, continuidade),
   timestamp (`dts`/`pts`) e muxagem de saída (ruído de `-f null`). Não somar
   estágios diferentes numa única contagem sem dizer de onde veio cada linha.
5. **Evidência × hipótese.** Reprodutibilidade de um comando prova que **aquele
   comando** é determinístico; **não** identifica causa nem culpado. Em particular:
   - a flag `K` num pacote **não** prova NAL IDR nem que SPS/PPS precedam o quadro;
   - o transporte **TCP** garante entrega ordenada dos bytes **recebidos**, e não
     que o defeito nasceu no encoder ou no provedor — captura, segmentação e
     qualquer etapa intermediária continuam candidatas;
   - **não** declarar incompatibilidade física com o decodificador da TV
     (MStar 2013) sem teste físico: comportamento de hardware não se infere de
     `stderr`.
   Escreva sempre `medido:` para o que foi observado e `hipótese:` para o resto.
6. **Rota importa.** Propriedades da fonte (resolução, codec, bitrate, fatias,
   PCR) medem igual em qualquer rota; **estabilidade e erros podem variar por
   rota**. Medição feita de host diferente daquele que alimenta a TV prova a
   fonte, não substitui o teste físico na TV.
7. **Prioridade do usuário**: um alvo por **canal distinto**; não sondar outra
   variante de canal já aprovado, para a grade ganhar diversidade.

### 2.5. Quando escalar para revisão externa (aider-argon)

Use `aider-argon` (Gemini 4 Argon, via `--read <arquivo> -m "<pergunta>"`) quando:

1. a **mesma falha resistir a duas tentativas** de correção;
2. a tarefa passar de **~40 minutos** de execução contínua sem convergir;
3. houver **dúvida metodológica** (o que medir, qual critério, o que é artefato de
   ferramenta) ou contradição entre dois dados medidos;
4. envolver concorrência, invariante físico do decodificador ou segurança.

Regra de uso: mande o **dado cru sanitizado** (nunca credenciais, URLs ou PIN) e
exija resposta com **critério numérico e comando exato**; **verifique a resposta
contra medição própria antes de adotar**. Duas correções já vieram dessa
verificação, e ambas devem ficar registradas como limite da revisão externa:

- `medido:` o comando sugerido (`-f rawvideo` com a trilha de áudio mapeada)
  falhava com `Encoder not found` (rc 8) e produzia "erros" que eram da ferramenta;
  a forma correta leva o áudio em `s16le`.
- `medido:` a hipótese "a captura começa antes de um IDR" **não se sustentou** nos
  dados disponíveis. `hipótese:` o primeiro pacote tinha flag `K`, mas isso **não
  prova** NAL IDR nem que SPS/PPS venham antes — a contradição foi observada, a
  explicação do mecanismo continua aberta.

---

## 3. Padrão de Compilação e Deploy no Tablet

1. **Compilação Cruzada ARM32 Estática**:
   Os binários em `src/` devem ser compilados para ARM32 (ARMv7-A) com ligação estática contra glibc/musl:
   ```bash
   arm-linux-gnueabihf-gcc -Wall -Wextra -O2 -static <arquivo.c> -lpthread -o <binario_arm32>
   ```
2. **Deploy Automatizado**:
   Sempre utilize o script mestre de deploy para instalar no tablet:
   ```bash
   ./scripts/deploy_tablet.sh [IP:PORTA_ADB]
   ```
   Nunca deixe processos órfãos (`killall -9 fuse_ntfs stream_fetcher` antes de substituir binários).

---

## 4. Política de Engenharia Multi-Agente

- **Orquestração Primeiro**: Para qualquer diagnóstico, refatoração ou auditoria não-trivial, atue como Líder Técnico. Decomponha o problema em subagentes especializados concorrentes.
- **Posse Disjunta de Arquivos**: Se múltiplos subagentes implementarem alterações em paralelo, eles devem possuir diretórios/arquivos estritamente separados.
- **Orquestração paralela no Pi**: Quando `subagent` estiver disponível e houver trabalho independente, decomponha a tarefa em frentes específicas e acione múltiplos subagentes em uma única chamada paralela (`tasks`), por exemplo `verifier` para suítes independentes, `reviewer` para revisão e `scout` para investigação. Descreva objetivo, escopo, arquivos e resultado esperado de cada frente. Enquanto os subagentes executam, o agente principal deve avançar em trabalho independente quando o ambiente permitir; se a ferramenta bloquear até todos retornarem, inclua as frentes independentes na mesma chamada paralela e prossiga assim que ela concluir. Não crie subagentes desnecessários para tarefas simples.
- **Isolamento de testes**: Antes de paralelizar suítes, verifique se compartilham arquivos temporários, portas, bancos de dados, dispositivos ou outros recursos mutáveis. Execute sequencialmente as que possam interferir entre si; não use `--test-threads=1` automaticamente sem verificar a necessidade. Consolide os resultados, trate falhas e informe as verificações realizadas. Se `subagent` não estiver disponível, execute a verificação diretamente.
- **Verificação Crítica e Antialucinação**: Nunca assuma que uma alteração funcionou sem validar:
  - Compilação estática sem warnings graves.
  - Telemetria em tempo real no tablet (`fuse_ntfs.log` com `near_starve=0`, `nulls=0`).
  - Bitstream inspecionado via `ffprobe` (0 B-frames, áudio AC-3, PCR estável).
