# Arquitetura de Borda Viva (Live Edge Lock) e Isolamento Estrito de Probes

> **Status**: Produção Estável  
> **Data de Homologação**: 05 de Outubro de 2026  
> **Alvo de Hardware**: Samsung Plasma PL51F4000 (MStar SoC 2013) & Samsung Galaxy Tab 3 Lite (SM-T110)  
> **Resultado do Teste de Bancada**: **64+ minutos ininterruptos** (2.38 GB transmitidos, 18.291 blocos, 0 erros, 0 pacotes nulos)

---

## 1. Visão Geral e Contexto

No ecossistema USB-Stream-TV, uma transmissão ao vivo de alta definição (1080p H.264 / AC-3 Dolby Digital a 5.0 Mbps) é transcodificada em tempo real na nuvem (VPS Oracle) e transmitida para um tablet Android antigo via rede local. O tablet emula um disco rígido USB de 8.00 GiB contendo um sistema de arquivos NTFS montado via daemon FUSE (`src/ntfs/fuse_ntfs.c`).

O decodificador da TV Samsung Plasma 2013 lê esse arquivo virtual (`TV AO VIVO.trp`) via comandos SCSI Mass Storage padrão através da interface ConnectShare.

Este documento detalha o diagnóstico forense, a auditoria multi-agente, a solução matemática e as métricas de validação da arquitetura que superou o congelamento histórico dos 20.7 minutos.

---

## 2. Diagnóstico Forense da Falha dos 20.7 Minutos

Durante os testes de estresse de longa duração, a reprodução congelava deterministicamente aos ~20.7 minutos (`foff = 800.047.104 bytes`). A análise dos logs de telemetria no kernel e no daemon FUSE revelou duas falhas arquiteturais interdependentes:

```
+-----------------------------------------------------------------------------------------+
|                                    CASCATA DE FALHA                                     |
+-----------------------------------------------------------------------------------------+
| 1. TV ConnectShare atinge 800 MB (10% do arquivo) ou consulta bookmark de sessão        |
|    anterior e realiza um seek para a frente.                                            |
|                                                                                         |
| 2. O driver FUSE classificava qualquer leitura > 8 MB com s_probe >= S_write + 2MB      |
|    como probe futuro, retornando pacotes MPEG-TS nulos (PID 0x1FFF) sem bloquear.        |
|                                                                                         |
| 3. O ConnectShare consumia esses pacotes nulos a velocidade máxima de barramento USB    |
|    (~14 a 20 MB/s), avançando cegamente pelo arquivo até atingir EOF (8 GB) aos 30 min. |
|                                                                                         |
| 4. DEADLOCK DO FEEDER: O feeder_thread possuía um loop de controle de fluxo artificial:  |
|    "if (lead > FLOW_CONTROL_LIMIT (64MB)) usleep(50000)". Como g_last_tv_stream_pos    |
|    ficou congelado em 800 MB durante as leituras de NULL, o feeder entrou em sono        |
|    perpétuo. O pipe de rede encheu, stream_fetcher bloqueou em write e a TV congelou.   |
+-----------------------------------------------------------------------------------------+
```

---

## 3. Auditoria Multi-Agente Concorrente

Conforme preconizado nas diretrizes de engenharia do projeto, a falha foi submetida a uma onda concorrente de especialistas:

### 3.1. Achados Críticos do Free Contrarian (Muse Spark 1.3)
1. **Risco de Sequestro de Âncora (Anchor Hijack)**: Se o caminho de entrega de pacotes nulos atualizar `g_prev_Fend = foff + c`, qualquer sondagem legítima de metadados do ConnectShare fragmentada em 2 leituras de 16 KB fará com que o 2º bloco seja falsamente reconhecido como sequencial (`foff == g_prev_Fend`). Isso dispararia um re-anchor espúrio, destruindo a reprodução ativa.
2. **Thrashing em Inanição de Rede**: Se a rede oscilar por >25 segundos, o avanço sequencial durante entrega de NULLs não pode disparar re-ancoragem para trás, evitando loops de scan PAT/PMT/SPS que seguram mutexes por centenas de milissegundos.
3. **Violação do Limite de Pacing SCSI**: Pacing com pausas superiores a 50 ms pode estourar os timeouts internos do chip MStar 2013.

### 3.2. Validação Unitária pelo Free Tester (Muse Spark 1.3)
Foi criado o simulador unitário em C [`tests/test_seek_reanchor.c`](../tests/test_seek_reanchor.c), que reproduziu exatamente o salto do log da TV (`foff=0` $\to$ `901120` $\to$ `799997952`).
- Resultado formal: **`35 PASS, 0 FAIL`**, validando que 3 blocos consecutivos confirmam o salto, enquanto probes isolados de 1 ou 2 blocos a 1.5 GB retornam NULL sem corromper a âncora ativa.

---

## 4. Pilares da Solução Arquitetural

### 4.1. Eliminação Total de Throttling no Feeder
Transmissões ao vivo são fluxos contínuos em tempo real que não podem ser pausados.
- O ring buffer em memória de 128 MB (`RINGSZ`) é circular e autossuficiente.
- O `feeder_thread` lê livremente da rede a taxa integral, depositando pacotes no anel.
- Se a TV parar ou desacelerar, o writer simplesmente sobrescreve os dados mais antigos.
- Se o leitor da TV ficar defasado além de `ring_old = g_s_write - RINGSZ`, a re-ancoragem para `live_target = g_s_write - LEADBACK` ocorre automaticamente e sem locks bloqueantes.

### 4.2. Isolamento Estrito de Probes e Imutabilidade de Cadeia
O offset de rastreamento de leitura sequencial (`g_prev_Fend`) representa exclusivamente a cadeia ativa de reprodução:
```c
if (g_base_valid && foff >= 8ULL * 1024 * 1024 && (is_future_probe || is_behind_probe) && !is_confirmed_jump) {
    fill_null(dst, c);
    /* Invariante: NUNCA alterar g_prev_Fend no caminho de entrega de NULLs */
    return;
}
```

### 4.3. Filtro de Debounce de Salto Confirmado ($K \ge 3$)
Para diferenciar sondagens de porcentagem (10%, 50%, 90%) de um comando intencional do usuário (seek, avanço rápido ou retomar bookmark):
```c
if (!is_seq_read) {
    if (g_probe_seq_end != (uint64_t)-1 && foff == g_probe_seq_end) {
        g_probe_consecutive_count++;
        g_probe_seq_end = foff + c;
    } else {
        g_probe_consecutive_count = 1;
        g_probe_seq_end = foff + c;
    }
} else {
    g_probe_consecutive_count = 0;
    g_probe_seq_end = (uint64_t)-1;
}

int is_confirmed_jump = (!is_seq_read && g_probe_consecutive_count >= 3);
```
- **1 ou 2 blocos (16 a 32 KB)**: Tratados como sondagem de metadados; recebem NULLs sem alterar o cursor ativo de playback.
- **3 ou mais blocos consecutivos**: Confirmam intenção real de reproduzir na nova posição; o driver aciona `is_reopen = 1`, busca a fronteira PAT/PMT/SPS mais próxima em `g_s_write - LEADBACK` e inicia a entrega de vídeo real imediatamente no novo offset.

### 4.4. Pacing Suave Clamped a $\le 50\text{ ms}$
Todas as pausas de regulação de vazão respeitam o limite de barramento:
```c
uint64_t diff_ms = target_ms - elapsed_ms;
if (diff_ms > 50) diff_ms = 50; /* AGENTS.md §2.2 */
pthread_mutex_unlock(&g_mu);
usleep((useconds_t)(diff_ms * 1000ULL));
pthread_mutex_lock(&g_mu);
```

### 4.5. Sincronismo de Borda Viva (Live Edge Lock)
Quando a TV atinge a margem de tempo real do stream (`start >= g_s_write`), ela entra em modo cooperativo:
```c
while (start >= g_s_write && now_ms() < deadline_ms && g_running) {
    wait_step(); /* Bloqueia na pthread_cond_t g_cv alimentada pelo feeder */
}
```
A entrega sincroniza com a chegada dos quadros do FFmpeg a cada $\sim 175\text{ ms}$, eliminando a inserção de pacotes nulos e assegurando zero atraso.

---

## 5. Telemetria Consolidada do Teste de 1 Hora

Durante o teste formal de 60 minutos com o canal **Record Rio FHD2** (1080p, AC-3, GOP 30, PCR 20ms), foram coletadas amostras periódicas a cada 6 minutos:

| Minuto | Offset Lido (`foff`) | Blocos 128KB | Folga Média | Taxa Efetiva | Near-Starve | Pacotes Nulos | Status |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **0** | `0 MB` | 0 | 27.2s | Inicial | 0 | 0 | Início |
| **6** | `269.3 MB` | 2.188 | 9.15s | 560 KB/s | 0 novos | 0 | Fluido |
| **12** | `504.1 MB` | 3.979 | 8.55s | 630 KB/s | 0 novos | 0 | Estável |
| **18** | `739.0 MB` | 5.771 | 0.21s (Live Lock) | 710 KB/s | 0 novos | 0 | Borda Viva |
| **24** | `965.5 MB` | 7.499 | 8.36s | 630 KB/s | 0 novos | 0 | **Ponto 20.7m Superado** |
| **30** | `1.20 GB` | 9.310 | 6.69s | 658 KB/s | 0 novos | 0 | Metade (50%) |
| **36** | `1.45 GB` | 11.179 | 9.20s | 680 KB/s | 0 novos | 0 | Estável |
| **42** | `1.68 GB` | 12.925 | 8.00s | 635 KB/s | 0 novos | 0 | Estável |
| **48** | `1.90 GB` | 14.639 | 4.84s | 624 KB/s | 0 novos | 0 | Estável |
| **54** | `2.14 GB` | 16.484 | 6.12s | 671 KB/s | 0 novos | 0 | Marca 2 GB |
| **60+** | **`2.38 GB`** | **`18.291`** | **`6.64s`** | **`616 KB/s`** | **`0 novos`** | **`0`** | **100% Concluído** |

---

## 6. Diretrizes para Manutenções Futuras

1. **Nunca reimplementar limitação de vazão no `feeder_thread`**: O anel de memória é o único buffer regulador necessário para Live TV.
2. **Preservar a imutabilidade de `g_prev_Fend` em probes**: Qualquer tentativa de registrar o fim de uma leitura em branches que retornam `fill_null` causará sequestro de âncora em decodificadores que sondam metadados.
3. **Respeitar o debounce $K \ge 3$**: Se novos players realizarem seeks maiores, o valor de $K$ pode ser mantido ou ajustado para tamanho acumulado em bytes ($\ge 64\text{ KB}$), mas nunca reduzido para 1 bloco.
4. **Manter o clamp de sleep $\le 50\text{ ms}$**: Essencial para a estabilidade do controlador USB do kernel 3.4 e do firmware MStar 2013.
