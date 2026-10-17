# Relatório de Auditoria Técnica: fix/return-continuity (Prompt 9)
**Auditor**: Gemini 4 Argon (Web) via `aider-argon`  
**Data**: 2026-10-16  
**Alvo**: `src/ntfs/fuse_ntfs.c`, `src/ntfs/ts_xform.h`, `scripts/tv_watchdog.sh` (commits Prompt 9)

---

### 1. Concorrência: Global Token Bucket, Fatiamento de Sleeps e Live-Edge Wait

* **Global Token Bucket & Sleep Slicing (`fuse_ntfs.c:1235`)**:  
  A implementação do token bucket mitiga com sucesso o efeito multiplicador de leituras concorrentes ao compartilhar `g_pace_win_ms` e `g_pace_win_bytes` entre todas as threads de leitura do FUSE.  
  O gerenciamento de locks em torno do fatiamento do `usleep` é robusto:
  ```c
  uint64_t slice_ms = (sleep_ms > max_clamp_ms) ? max_clamp_ms : sleep_ms;
  pthread_mutex_unlock(&g_mu);
  usleep((useconds_t)(slice_ms * 1000ULL));
  pthread_mutex_lock(&g_mu);
  if (g_epoch != saved_epoch || !g_base_valid) {
      pace_ok = 0;
      break;
  }
  ```
  Ao liberar `g_mu` durante a fase de sleep (`slice_ms <= 50ms`), o código estritamente evita travar outras threads do FUSE ou a `feeder_thread` (o que causaria deadlock ou congelamento do pipeline).  
  A revalidação imediata de `g_epoch` e `g_base_valid` ao readquirir o lock aborta corretamente o pacing caso um re-anchor ou reset da stream tenha ocorrido durante a troca de contexto do sleep.

* **Bounded Live-Edge Wait (`fuse_ntfs.c:1312`)**:  
  A espera na borda viva foi delimitada com segurança em 500 ms (`edge_wait_ms = 500`). Trata-se de uma correção crítica em relação ao bloqueio anterior de 2.8s, impedindo que o SoC MStar entre em timeout na camada SCSI USB (que provocava paradas de leitura de ~42 segundos).  
  O loop avalia corretamente `now_ms() < edge_lim && g_running` utilizando `wait_step()` (que libera e readquire o mutex via `pthread_cond_timedwait`). Nenhum risco de deadlock detectado nesta seção.

---

### 2. Invariantes MPEG-TS: Alinhamento de 188 Bytes, Checkpoints e Wrap-around

* **Paced NULL-fill e Alinhamento de 188 Bytes (Ponto de Atenção)**:  
  Ao lidar com `is_behind_probe` ou resync forçado, o código utiliza `fill_null(dst, c)` ou `fill_null(dst + fo, cc)`.  
  *Risco Identificado:* As requisições de leitura do FUSE no kernel Android têm tipicamente 64 KB (65.536 bytes) ou 128 KB (131.072 bytes). Nenhum desses tamanhos é múltiplo de 188 bytes (`65536 % 188 = 112`). Injetar exatamente `cc` bytes de NULLs quebra temporariamente o grid de alinhamento de 188 bytes para o hardware demuxer da TV.  
  O demuxer MStar busca o próximo sync byte `0x47` para ressincronizar, mas isso introduz um framing error transitório no decodificador.

* **Checkpoints em RAM (`g_ckpt_pin`)**:  
  A implementação de `ckpt_note` e `ckpt_lookup` é altamente eficiente para o cenário pós wrap-around. O uso de um anel/array de 64 slots (`CKPT_PIN_N = 64`) indexado por `(fend - 1ULL) / CKPT_PIN_STEP` (64 KB) garante inserções O(1) e busca O(N=64) desprezível.  
  A checagem `now >= g_ckpt_pin[i].t_ms` previne underflow no cálculo da idade do checkpoint (janela de 30 minutos).

* **Scar Skip Validation (`validate_anchor_head`)**:  
  A varredura de cicatriz respeita estritamente os invariantes do MPEG-TS:
  ```c
  uint64_t p = snapped_abs - (snapped_abs % 188ULL);
  for (; p + 188ULL <= end && collected < 24; p += 188ULL) {
  ```
  Forçar `p` a um múltiplo de 188 bytes impede o parser de ler bytes arbitrários no meio do pacote. O reancoramento via `SCAR_SKIP_AHEAD` (1 MB) salta com segurança sobre descontinuidades severas de PTS upstream.

---

### 3. Compatibilidade ARMv7-A 32-bit (Galaxy Tab 3 Lite KitKat Android 4.4)

* **Acesso Atômico a Variáveis de 64-bit**:  
  Na arquitetura ARM 32-bit, ler ou escrever inteiros de 64 bits (`uint64_t`) requer dois ciclos de instrução (`LDRD`/`STRD` ou dois `LDR`), criando risco de *torn reads* se não houver proteção.  
  No `fuse_ntfs.c`, todas as variáveis globais de 64 bits (`g_pace_win_ms`, `g_s_write`, `g_anchor_stream_pos`, etc.) são acessadas **estritamente sob a posse do mutex `g_mu`**, garantindo atomicidade e evitando corrupção de memória.

* **Casting e Prevenção de Overflows**:  
  O patch força promoção para 64 bits em todas as operações aritméticas críticas:
  ```c
  uint64_t allowed = target_rate_kbps * 1024ULL * (win_elapsed + 1ULL) / 1000ULL;
  uint64_t sleep_ms = excess * 1000ULL / (target_rate_kbps * 1024ULL);
  ```
  Sem o literal `ULL`, a multiplicação `target_rate_kbps * 1024` poderia sofrer overflow em registradores de 32 bits antes da atribuição. Os casts para `(unsigned long long)` nas macros de `LOG` cumprem os requisitos estritos do compilador ARM32.

---

### 4. Robustez do Fallback e Resync USB

* **Disparo do Gatilho (`fuse_ntfs.c:766`, `1178`)**:  
  O daemon FUSE solicita o resync tocando `/data/local/tmp/resync_usb` quando detecta sequências contínuas de NULLs (`g_behind_null_runs >= 4`) ou wraps de anel sem recuperação. A chamada `open(..., O_CREAT)` é atômica e segura.

* **Tratamento no Watchdog (`tv_watchdog.sh:276`)**:  
  ```bash
  if [ -f "$BASE/resync_usb" ] && [ ! -f "$BASE/vod_mode.flag" ] && [ ! -f "$BASE/favorites_mode.flag" ]; then
      if cooldown_ok resync 120; then
          rm -f "$BASE/resync_usb"
          # ... reinicia stack live ...
  ```
  O script restringe o resync ao ambiente de Live TV (não interfere em VOD/Favoritos).  
  O cooldown de 120 segundos protege contra loops infinitos de reinicialização da porta USB. O reinício via `$BB setsid` desacopla o processo sem travar o loop principal do watchdog.

---

### 5. Veredito Técnico e Recomendações Priorizadas

**Veredito:**  
As alterações do Prompt 9 são estruturalmente sólidas, altamente resilientes e rigorosas em termos de concorrência e thread-safety. O token bucket regula o fluxo sem bloquear leituras concorrentes, e os checkpoints em RAM resolvem o problema de repetição/congelamento no wrap do buffer circular.

**Recomendações Priorizadas:**

1. **Ajuste de Alinhamento no Preenchimento de NULLs (Prioridade Média)**:  
   *Contexto:* `fuse_ntfs.c:878` e `1178` (`fill_null(dst + fo, cc)`)  
   *Ação:* Ao preencher buffers FUSE com pacotes NULL, considere ajustar o preenchimento para que cada bloco seja alinhado a pacotes TS completos de 188 bytes (`0x47 0x1F 0xFF 0x10 ...`), evitando que o SoC da TV registre pequenos framing errors temporários ao sair do estado de fome.
2. **Reinício de Janela pós-Sleep (Otimização Baixa)**:  
   *Contexto:* `fuse_ntfs.c:1270`  
   *Ação:* Após o loop de fatiamento (`while (sleep_ms > 0)`), o tamanho `cc` é adicionado aos bytes da nova janela. Trata-se de um comportamento conservador aceitável que não prejudica a fluidez da transmissão.
