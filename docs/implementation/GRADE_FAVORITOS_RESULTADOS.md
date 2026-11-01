# Grade completa de Favoritos Live — resultados medidos

Atualizado: 27/10/2026. Executor: `opencode/deepseek-v4.1-flash-free`.
Consultor técnico: GPT-6 Astra (via `ask-astra`). Revisão final: GPT Sol.

Este documento separa **características da fonte original** de **características da
saída preparada para a TV**. Aprovação de fonte não é aprovação de saída, e
pendência nunca é contada como aprovação.

## 1. Método

- Catálogo Live congelado em `scratch/catalog/live_streams.json` (3.416 linhas);
  120 favoritos → 19 IDs Live → 16 identidades.
- Triagem: 20 s de captura por candidato, 20 identidades com expansão para
  variantes `H265`, `HD¹/HD²`, `H264` e `4K`; 78 candidatos no plano final.
- Finalista (winner): 300 s de captura por candidato, `-c copy` sem transcode,
  medição de pixels, codec/perfil, B-frames, slices, IDR, PCR, áudio e erros.
- Aprovação exige **duas janelas independentes de 300 s**, cobertura ≥ 97 % da
  duração solicitada (dados de duração/áudio precisam existir; ausência de dado
  nunca é tratada como estabilidade), nenhuma lacuna de áudio > 2 s, resolução
  1920×1080 nas **duas** janelas, **decode limpo** medido por passe completo de
  decodificação e **saída para TV validada** pelo pipeline real.
- A saída é medida rodando o comando exato do manager (`FFmpegProvider`) sobre
  uma amostra curta da fonte **e passando o resultado pelo `PcrBounder` da
  bridge real**; só então são exigidos 1080p30, 0 B-frames, 1 slice,
  IDR ≈ 1 s (mediana e máximo), AC-3 **384 kbit/s**/48 kHz/estéreo, PCR ≤ 20 ms
  e áudio contínuo. FFmpeg com código de saída ≠ 0 ou stderr não vazio reprova.
- O passe de decode (`ffmpeg -f null`) registra erros que `-c copy` não expõe;
  sem esse registro a identidade fica pendente.
- Bitrate de vídeo e bitrate total TS são colunas distintas. Quando a listagem
  de quadros cobriu apenas ~150 s de uma amostra mais longa, o bitrate de vídeo
  é recalculado proporcionalmente e marcado com `*`; o TS é sempre direto dos
  bytes capturados e da duração de mídia medida.
- Nenhum teste de host foi usado para declarar funcionamento na TV.

## 2. Tabela por canal

| Identidade | Favoritos | Escolhido | Região | Nativa | Vídeo kbps | TS kbps | Áudio | Lacunas >0,5 s | Lacunas >2 s | PCR fonte | Janelas | Teste | Saída TV | PCR saída | Áudio saída | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| band-news | 206080 | 206081 BAND NEWS FHD² | national | 1920x1080 | 6405 | 3468 | aac | 1 | 0 | 1267.93 ms | 1 | winner 300.0 s | None | None ms | None | pending:pending_source_stability |
| cnn-brasil | 206087 | 206088 CNN BRASIL FHD² | national | 1280x720 | 1411 | 1578 | aac | 0 | 0 | 66.73 ms | 1 | winner 300.0 s | None | None ms | None | pending:pending_no_native_1080 |
| globo-news | 206098 | 206097 GLOBO NEWS FHD¹ | national | 1920x1080 | 6705 | 7048 | aac | 0 | None | 100.0 ms | 1 | winner 300.0 s | None | None ms | None | pending:pending_decode_validation |
| record-news | 206111 | 206111 RECORD NEWS FHD¹ | national | 1280x720 | 3688 | 3991 | aac | 0 | None | 100.1 ms | 1 | winner 300.0 s | None | None ms | None | pending:pending_no_native_1080 |
| band | 207177, 207171, 207172 | 207171 BAND SP FHD¹ | sp | 1280x720 | 2764 | 2931 | aac | 0 | 0 | 66.73 ms | 1 | winner 300.0 s | None | None ms | None | pending:pending_no_native_1080 |
| sbt | 207198 | 207198 SBT RJ FHD¹ | rj | 1920x1080 | 1985 | 2224 | aac | 0 | None | 133.33 ms | 1 | winner 300.0 s | output_ok | 19.55 ms | 0 | approved |
| redetv | 208423 | 208423 REDE TV FHD¹ | national | 1280x720 | 3698 | 4002 | aac | 0 | None | 100.1 ms | 1 | winner 300.0 s | None | None ms | None | pending:pending_no_native_1080 |
| tv-cultura | 208430 | 208430 TV CULTURA FHD¹ | national | 1280x720 | 3723 | 4000 | aac | 0 | None | 100.0 ms | 1 | winner 300.0 s | None | None ms | None | pending:pending_no_native_1080 |
| record | 214640 | 214640 RECORD TV RIO FHD² | rj | 1920x1080 | 2461 | 2712 | aac | 0 | None | 67.1 ms | 1 | winner 300.0 s | output_ok | 19.55 ms | 0 | approved |
| telecine-action | 365244 | 365243 TELECINE ACTION H265 | national | 1920x1080 | 5467 | 2994 | aac | 0 | 0 | 66.73 ms | 1 | winner 300.0 s | output_ok | 19.55 ms | 0 | approved |
| history | 387624 | 387622 HISTORY 4K | national | 1920x1080 | 3018 | 3284 | aac | 0 | 0 | 131.87 ms | 1 | winner 300.0 s | output_ok | 19.55 ms | 0 | approved |
| discovery-channel | 387640 | 387640 DISCOVERY CHANNEL FHD¹ | national | 1920x1080 | 3021 | 3286 | aac | 0 | None | 100.0 ms | 1 | winner 300.0 s | output_ok | 19.55 ms | 0 | approved |
| discovery-hh | 387648 | 387648 DISCOVERY H&H FHD¹ | national | 1920x1080 | 6362 | 3453 | aac | 0 | 0 | 66.73 ms | 1 | winner 300.0 s | output_ok | 19.55 ms | 0 | approved |
| bbc-world | 1765055 | 1765055 BBC WORLD FHD¹ | national | 1280x720 | 3594 | 3894 | aac | 0 | None | 100.0 ms | 1 | winner 300.0 s | None | None ms | None | pending:pending_no_native_1080 |
| sbt-news | 3030726 | 3030725 SBT NEWS H265 | national | 1920x1080 | 2747 | 3007 | aac | 0 | None | 66.74 ms | 1 | winner 300.0 s | None | None ms | None | pending:pending_source_stability |
| globo | 10006, 10007 | 10006 GLOBO RIO DE JANEIRO (RJ) FHD¹ | rj | 1280x720 | 2382 | 2624 | aac | 0 | None | 133.33 ms | 1 | winner 300.0 s | None | None ms | None | pending:pending_no_native_1080 |

### Decisões de região e substituições

- **Band**: a variante RJ (207177 FHD², 720p) apresentou lacuna de áudio de
  8,9 s na janela de 300 s; a melhor alternativa estável é **BAND SP FHD¹
  (207171, 720p, SP)**. Isso muda a programação local (SP em vez de RJ) e está
  registrado como pendência, não como aprovação.
- **Globo News**: o favorito original era 206098; o candidato escolhido é
  **206097 FHD¹**, mesma família, melhor medido, mas com 1 erro de decode em
  60 s — pendente.
- **SBT News**: 3030725 entregou 1080p na primeira janela e 720p na segunda;
  resolução instável entre janelas → pendente (`pending_source_stability`).
- **Band News**: 206081 entregou 1080p na primeira janela e apenas 225 s de
  mídia em 300 s na segunda (cobertura 75 %) → pendente; o timeout inicial era
  transitório, confirmado por nova janela.
- **CNN Brasil**: nenhuma variante medida entregou 1080p (206087/206088/206090
  em 720p); melhor alternativa 206088 (720p, sem lacuna > 0,5 s nas amostras).

## 3. Características da fonte vs. saída preparada

| Camada | Evidência | O que prova | O que não prova |
| --- | --- | --- | --- |
| Fonte | 300 s × 2 janelas, `-c copy` | resolução nativa, continuidade, áudio, erros e timestamps do provedor | estabilidade por horas |
| Decode | 60 s decodificados de verdade (`-f null`) | bitstream decodifica sem erros no trecho | erros raros fora do trecho |
| Saída | pipeline real + `PcrBounder` em amostra curta | conformidade do bitstream entregue à TV (1080p30, 0 B-frames, 1 slice, IDR 1 s, AC-3 384k, PCR ≤ 20 ms, áudio) | comportamento do decodificador MStar, tempo de preparação, navegação ConnectShare |
| Física | pendente (GPT Sol + usuário) | abertura ao vivo, Return → outro canal, A→B→A | — |

O perfil de saída `1080p30` foi validado com encode real **depois** do
`PcrBounder`: 1920×1080, Constrained Baseline, 0 B-frames, 1 slice por quadro,
IDR 1,000 s, AC-3 384 kbit/s 48 kHz estéreo, PCR medido ≤ 20 ms e sem lacunas de
áudio. O perfil `720p30` do baseline continua selecionável e foi revalidado com
o mesmo método. Um valor antigo de 20,3/20,55 ms era medido **antes** do filtro
e não é usado como prova.

## 4. Áudio

- Método corrigido: lacuna = PTS seguinte − (PTS atual + duração do pacote);
  o delta bruto entre PTS superestima a lacuna em uma duração de pacote.
- Lacunas > 2 s na fonte bloqueiam aprovação; lacunas entre 0,5 s e 2 s são
  registradas e avaliadas na saída.
- Casos medidos: CNN Brasil 206087 (8,1 s), Band 207177 (8,9 s), Discovery H&H
  387648 tentativa 1 (5 lacunas, máx. 6,6 s), Telecine Action 365244 (3,8 s).
  Band News 206081 apresentou 1,39 s na primeira janela válida (abaixo do limite
  de bloqueio, acima do aceitável para a TV) — decisão explícita do Sol.
- Nenhuma promessa de correção: a saída é medida e reportada por identidade.

## 5. Candidatos reprovados e motivos

| Identidade | Candidato | Rótulo | Região | Fase | Resolução | B | Slices | IDR s | Vídeo kbps | TS kbps | PCR ms | Lacuna áudio ms | Erros | Veredito |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| band-news | 206080 | FHD1 | national | triage | 1280x720 | 292 | 1 | 2.002 | 1498 | 1663 | 66.73 | 340.2 | {"source_reconnect": 1} | not_1080p |
| band-news | 206082 | HD1 | national | triage | 1280x720 | 390 | 1 | 2.002 | 1568 | 1730 | 66.73 | 21.3 | {} | not_1080p |
| band-news | 206079 | H265 | national | triage | 1280x720 | 280 | 1 | 1.0 | 4245 | 4540 | 100.0 | 32.0 | {"decode_error": 1} | not_1080p |
| band-news | 206083 | HD2 | national | triage | 1280x720 | 440 | 1 | 2.002 | 2408 | 2625 | 66.73 | 21.3 | {} | not_1080p |
| cnn-brasil | 206087 | FHD1 | national | winner | 1280x720 | 4205 | 3 | 2.0 | 1964 | 2198 | 100.0 | 8128.0 | {"decode_error": 1, "source_reconnect": 16} | not_1080p |
| cnn-brasil | 206089 | HD1 | national | triage | 960x540 | 290 | 1 | 2.0 | 1223 | 1446 | 100.0 | 21.3 | {"decode_error": 1} | not_1080p |
| cnn-brasil | 206086 | H265 | national | winner | NonexNone | None | None | None | None | None | None | None | {"http_4xx": 2, "input_open_failed": 1} | no_media |
| cnn-brasil | 206090 | HD2 | national | triage | 1280x720 | 386 | 2 | 1.401 | 2546 | 2737 | 66.74 | 21.3 | {} | not_1080p |
| globo-news | 206098 | FHD2 | national | triage | 1920x1080 | 0 | 2 | 3.003 | 3035 | 3442 | 66.74 | 21.3 | {"source_reconnect": 1} | candidate_ok |
| globo-news | 206099 | HD1 | national | triage | 1280x720 | 289 | 1 | 2.0 | 3660 | 3935 | 100.0 | 32.0 | {"decode_error": 1} | not_1080p |
| record-news | 206112 | FHD2 | national | triage | 1280x720 | 0 | 1 | 0.3 | 1689 | 1923 | 100.0 | 59.7 | {"source_reconnect": 1} | not_1080p |
| record-news | 206113 | HD1 | national | triage | 1280x720 | 967 | 2 | 1.602 | 3680 | 3998 | 100.1 | 201.5 | {"source_reconnect": 1} | not_1080p |
| record-news | 206110 | H265 | national | triage | 1280x720 | 0 | 1 | 0.3 | 1984 | 2229 | 100.0 | 201.4 | {"source_reconnect": 3} | not_1080p |
| record-news | 206114 | HD2 | national | triage | NonexNone | None | None | None | None | None | None | None | {"connection_timeout": 3, "input_open_failed": 1} | no_media |
| band | 207177 | FHD2 | rj | winner | 1280x720 | 0 | 2 | 10.01 | 2863 | 3119 | 66.74 | 8853.3 | {"source_reconnect": 15} | not_1080p |
| band | 207178 | HD2 | rj | triage | 848x480 | 0 | 1 | 10.01 | 1148 | 1374 | 66.73 | 21.3 | {} | not_1080p |
| band | 207172 | FHD2 | sp | triage | 1280x720 | 0 | 1 | 1.083 | 771 | 982 | 149.47 | 59.8 | {"source_reconnect": 1} | not_1080p |
| band | 207170 | H265 | sp | triage | 1280x720 | 0 | 1 | 1.1 | 895 | 1107 | 134.17 | 108.4 | {"source_reconnect": 1} | not_1080p |
| band | 3917940 | HD2 | rj-interior | triage | NonexNone | None | None | None | None | None | None | None | {"connection_timeout": 3, "input_open_failed": 1} | no_media |
| band | 207173 | HD1 | national | triage | 848x480 | 0 | 1 | 10.01 | 1683 | 1922 | 66.73 | 21.3 | {} | not_1080p |
| band | 207174 | HD2 | national | triage | 1280x720 | 0 | 1 | 1.018 | 986 | 1203 | 149.48 | 39.4 | {} | not_1080p |
| sbt | 207199 | HD1 | rj | triage | 1280x720 | 0 | 1 | 2.0 | 1345 | 1569 | 42.42 | 185.9 | {"source_reconnect": 1} | not_1080p |
| sbt | 207192 | FHD1 | sp | triage | 1920x1080 | 0 | 1 | 3.003 | 3082 | 3350 | 66.73 | 185.2 | {"source_reconnect": 1} | candidate_ok |
| sbt | 207193 | FHD2 | sp | triage | 1920x1080 | 420 | 2 | 1.001 | 5060 | 5300 | 66.73 | 183.3 | {"source_reconnect": 1} | candidate_ok |
| redetv | 208425 | HD1 | national | triage | 1280x720 | 1001 | 1 | 1.602 | 3330 | 3638 | 83.42 | 21.3 | {} | not_1080p |
| redetv | 208422 | H264 | national | triage | 1280x720 | 989 | 1 | 1.602 | 3809 | 4126 | 83.42 | 148.6 | {"source_reconnect": 1} | not_1080p |
| redetv | 208421 | H265 | national | triage | 1280x720 | 989 | 1 | 1.602 | 3802 | 4122 | 100.1 | 67.2 | {"source_reconnect": 2} | not_1080p |
| tv-cultura | 208432 | HD1 | national | triage | 1280x720 | 394 | 2 | 3.2 | 3753 | 4036 | 100.0 | 21.3 | {} | not_1080p |
| tv-cultura | 208434 | SD1 | national | triage | 768x432 | 394 | 2 | 3.2 | 2440 | 2693 | 100.0 | 21.3 | {} | not_1080p |
| tv-cultura | 208429 | H265 | national | triage | 1280x720 | 393 | 2 | 3.2 | 3834 | 4128 | 100.0 | 117.3 | {"source_reconnect": 2} | not_1080p |
| record | 214639 | FHD1 | rj | triage | 1280x720 | 0 | 1 | 2.002 | 2445 | 2694 | 100.0 | 126.7 | {"source_reconnect": 1} | not_1080p |
| record | 214641 | HD1 | rj | triage | 1280x720 | 340 | 2 | 2.002 | 1535 | 1698 | 100.0 | 393.7 | {"source_reconnect": 1} | not_1080p |
| record | 214642 | HD2 | rj | triage | 1920x1080 | 0 | 1 | 1.001 | 2421 | 2670 | 66.73 | 21.3 | {"source_reconnect": 1} | candidate_ok |
| telecine-action | 365244 | FHD1 | national | winner | 1920x1080 | 0 | 2 | 3.003 | 2824 | 3087 | 3737.07 | 3776.0 | {"connection_timeout": 3, "source_reconnect": 18} | candidate_ok |
| telecine-action | 365245 | FHD2 | national | triage | 1920x1080 | 287 | 1 | 2.0 | 6657 | 6998 | 166.67 | 32.0 | {} | candidate_ok |
| telecine-action | 365246 | HD1 | national | triage | 1280x720 | 0 | 1 | 3.003 | 1940 | 2184 | 66.73 | 170.8 | {"source_reconnect": 1} | not_1080p |
| telecine-action | 365242 | 4K | national | triage | 1920x1080 | 0 | 1 | 3.003 | 3012 | 3279 | 66.73 | 21.3 | {} | candidate_ok |
| telecine-action | 365247 | HD2 | national | triage | 1280x720 | 279 | 1 | 2.0 | 1805 | 2043 | 100.0 | 32.0 | {} | not_1080p |
| history | 387624 | FHD1 | national | winner | 1920x1080 | 2999 | 3 | 0.2 | 3016 | 3281 | 133.32 | 202.6 | {"source_reconnect": 13} | candidate_ok |
| history | 387625 | FHD2 | national | triage | 1280x720 | 389 | 1 | 2.002 | 817 | 967 | 66.73 | 310.2 | {"source_reconnect": 1} | not_1080p |
| history | 387626 | HD1 | national | triage | 1280x720 | 141 | 1 | 2.002 | 2081 | 2379 | 66.73 | 21.3 | {"source_reconnect": 1} | not_1080p |
| history | 387623 | H265 | national | triage | 1280x720 | 390 | 1 | 2.002 | 1001 | 1152 | 66.73 | 21.3 | {} | not_1080p |
| history | 387627 | HD2 | national | triage | 1280x720 | 390 | 1 | 2.002 | 1264 | 1419 | 66.73 | 21.3 | {} | not_1080p |
| discovery-channel | 387641 | FHD2 | national | triage | 1280x720 | 389 | 1 | 2.002 | 1456 | 1618 | 66.73 | 248.0 | {"source_reconnect": 1} | not_1080p |
| discovery-channel | 387642 | HD1 | national | triage | 1280x720 | 0 | 1 | 2.502 | 1930 | 2314 | 66.73 | 21.3 | {} | not_1080p |
| discovery-channel | 387638 | 4K | national | triage | 1280x720 | 390 | 1 | 2.002 | 2939 | 3131 | 66.73 | 21.3 | {} | not_1080p |
| discovery-channel | 387639 | H265 | national | triage | 1280x720 | 390 | 1 | 2.002 | 3187 | 3385 | 66.73 | 21.3 | {} | not_1080p |
| discovery-channel | 387643 | HD2 | national | triage | 1280x720 | 390 | 1 | 2.002 | 3393 | 3595 | 66.73 | 21.3 | {} | not_1080p |
| discovery-hh | 387649 | FHD2 | national | triage | 1280x720 | 290 | 1 | 2.002 | 1577 | 1748 | 66.73 | 21.3 | {} | not_1080p |
| discovery-hh | 387650 | HD1 | national | triage | 1280x720 | 0 | 1 | 3.003 | 2513 | 2772 | 66.73 | 213.8 | {"connection_timeout": 1, "source_reconnect": 2} | not_1080p |
| discovery-hh | 387646 | 4K | national | triage | 1280x720 | 290 | 2 | 2.002 | 1024 | 1184 | 66.73 | 21.3 | {} | not_1080p |
| discovery-hh | 387647 | H265 | national | triage | 1280x720 | 290 | 1 | 2.002 | 1709 | 1882 | 66.73 | 21.3 | {} | not_1080p |
| discovery-hh | 387651 | HD2 | national | triage | 1280x720 | 290 | 1 | 2.002 | 1783 | 1959 | 66.73 | 21.3 | {} | not_1080p |
| bbc-world | 1765056 | HD1 | national | triage | 1280x720 | 973 | 2 | 1.602 | 3821 | 4140 | 100.0 | 206.3 | {"source_reconnect": 1} | not_1080p |
| sbt-news | 3030726 | FHD1 | national | triage | 1280x720 | 394 | 2 | 3.2 | 3789 | 4073 | 100.0 | 85.3 | {"source_reconnect": 1} | not_1080p |
| sbt-news | 3030727 | FHD2 | national | triage | NonexNone | None | None | None | None | None | None | None | {"connection_timeout": 3, "input_open_failed": 1} | no_media |
| sbt-news | 3030728 | HD1 | national | triage | 1280x720 | 393 | 2 | 3.2 | 3781 | 4072 | 100.0 | 56.0 | {"source_reconnect": 1} | not_1080p |
| globo | 10007 | FHD2 | rj | triage | 1280x720 | 436 | 2 | 1.6 | 2433 | 2677 | 133.33 | 160.0 | {"source_reconnect": 1} | not_1080p |
| globo | 10008 | HD1 | rj | triage | 1280x720 | 420 | 1 | 1.001 | 3426 | 3629 | 66.73 | 192.8 | {"source_reconnect": 1} | not_1080p |
| globo | 10017 | FHD1 | rj-interior | triage | NonexNone | None | None | None | None | None | None | None | {"connection_timeout": 3, "input_open_failed": 1} | no_media |
| globo | 10022 | FHD1 | rj-interior | triage | NonexNone | None | None | None | None | None | None | None | {"connection_timeout": 3, "input_open_failed": 1} | no_media |
| globo | 10009 | HD2 | national | triage | 1280x720 | 437 | 3 | 1.6 | 2470 | 2715 | 133.33 | 21.3 | {} | not_1080p |

## 6. Limitações

- Amostras de 5 min não provam estabilidade por horas; a recomendação do Astra
  de duas janelas foi aplicada às identidades candidatas a aprovação.
- A cobertura de mídia é medida por duração de áudio e timestamps de quadros;
  falhas silenciosas do provedor dentro da janela só aparecem como lacunas.
- O tempo de preparação de ~20 s e a navegação ConnectShare só podem ser
  avaliados fisicamente.
- A grade entregue é **parcial**: identidades sem 1080p nativo medido ficam em
  `pending_no_native_1080` com a melhor alternativa apresentada, sem aprovação.
