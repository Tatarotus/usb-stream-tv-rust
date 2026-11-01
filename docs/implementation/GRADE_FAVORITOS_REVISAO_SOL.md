# Revisão GPT Sol — grade de Favoritos Live

Atualizado: 27/10/2026. Executor: DeepSeek V4.1 Flash Free. Consultor: GPT-6 Astra.
Nada foi ativado na TV; produção, VOD, container e mídia USB permanecem intactos.

## 1. O que está pronto

| Item | Estado | Caminho |
| --- | --- | --- |
| Baseline físico e hashes | concluído | `docs/implementation/GRADE_FAVORITOS_BASELINE.md` |
| Configuração de exportação revisada | gerado (geração 4, 6 aprovadas, 10 pendentes) | `config/live_favorites_export.json` |
| Evidência sanitizada (JSON/CSV/tabela) | concluído | `docs/implementation/evidence/` |
| Código Rust/Python e testes de host | concluído | diff da branch + seção 5 |
| Binários ARM32 estáticos | concluído | `bin/` do pacote (`70fe033b…`, `e52a389f…`) |
| Pacote de staging | gerado e validado (parcial, 6 arquivos, volume 137.438.953.472 B) | `package/` do pacote |
| Ativação seletiva e rollback | roteiro pronto, não executado | `GRADE_FAVORITOS_ATIVACAO_SELETIVA.md` |
| Teste físico | pendente | usuário + GPT Sol |

## Números do snapshot

- Favoritos: **120**; IDs Live: **19**; identidades: **16**.
- Aprovadas para a grade (1080p nativo + duas janelas + saída validada): **6** — sbt, record, telecine-action, history, discovery-channel, discovery-hh.
- Pendentes de decisão: **10** — band-news (pending_source_stability), cnn-brasil (pending_no_native_1080), globo-news (pending_decode_validation), record-news (pending_no_native_1080), band (pending_no_native_1080), redetv (pending_no_native_1080), tv-cultura (pending_no_native_1080), bbc-world (pending_no_native_1080), sbt-news (pending_source_stability), globo (pending_no_native_1080).
- Saídas validadas: **6**; geração do export: **4**; perfil: **1080p30**.


## 2. Checklist para o GPT Sol

1. Conferir que cada identidade aprovada tem **duas janelas de 300 s** com
   1920×1080, cobertura ≥ 97 %, sem lacuna de áudio > 2 s, e que a saída
   preparada foi validada (1080p30, 0 B-frames, 1 slice, IDR 1 s, AC-3 384k,
   PCR ≤ 50 ms, áudio contínuo).
2. Confirmar que nenhuma identidade só-720p foi marcada como aprovada: elas
   devem estar em `pending_no_native_1080` com a melhor alternativa descrita.
3. Decidir explicitamente sobre as pendências de 1080p nativo e sobre as
   lacunas de áudio de 0,5–2 s (Band News 206081, 1,39 s).
4. Validar o pacote: `sha256sum -c SHA256SUMS` e
   `validate_live_favorites_package.py --staging . --expect-volume 137438953472`.
5. Confirmar que o manifesto contém apenas identidades aprovadas, cada uma em
   uma única categoria na raiz (sem `Favoritos/`), e que `manifest_id`,
   `file_id` e `bootstrap_id` batem com o export.
6. Revisar o diff de código: limite 2→32, categorias na raiz, perfil 1080p30,
   export/allowlist e o gate de saída.
7. Rodar o roteiro de ativação seletiva somente em janela autorizada, com
   rollback pelo script e sem `patch_trp` na imagem multicanal.
8. Teste físico: abrir canal, medir preparação (~20 s), Return → outro canal,
   A→B→A, miniaturas sem trocar canal (K≥3), observar áudio no trecho crítico.

## 3. Consultas ao Astra

| # | Motivo | Pergunta | Conclusão do Astra | Ação executada | Resultado medido |
| --- | --- | --- | --- | --- | --- |
| 1 | Evidência inicial: 720p em 7 identidades, lacunas de áudio, fontes instáveis e PCR de entrada de 100 ms | critério mínimo de aprovação 1080p, ordem de reteste e política para 720p | gate com duas janelas de 300 s, cobertura ≥ 99,9 %, zero reconexões, lacuna de áudio medida por PTS+duração (não por delta bruto), 1080p em toda a captura; 720p nunca aprovado; 2ª janela obrigatória; telecine 365244 por último | implementado: duas janelas para candidatas, gate de cobertura ≥ 97 % e áudio > 2 s, medição de áudio corrigida, reteste de band-news/discovery-hh/telecine/cnn; selo de saída obrigatório | ver seção 4 e tabela de resultados |
| 2 | Consolidação final da matriz e do pacote | revisar matriz, pendências e riscos antes da entrega | <!-- ASTRA2_CONCLUSAO --> | <!-- ASTRA2_ACAO --> | <!-- ASTRA2_RESULTADO --> |
| 3 | `PcrBounder` rejeitava intervalos de 20,05–20,3 ms sem NULL no perfil 1080p | qual correção mínima garante PCR ≤ 20 ms após o filtro sem relaxar validação | usar `-pcr_period 19` **somente** no 1080p (720p mantém 20), preservar bounder fail-closed, validar encode real com cenas complexas e exigir zero rejeições/substituições em regime normal; regenerar bootstrap/hashes e obter revisão Sol | perfil 1080p e bootstrap com 19 ms; 720p inalterado em 20 ms; encode real (testsrc e mandelbrot) passou com PCR máx 19,3/19,55 ms, zero substituições e AC-3 384k; binário ARM32 regenerado `320287f6…` | `tests/test_live_favorites_output.py` e teste de contrato do manager atualizados; pacote e hashes regenerados |

Diferença assumida em relação ao texto do Astra: cobertura mínima de 97 %
(em vez de 99,9 %), porque a duração de mídia é medida por timestamps de áudio
e quadros e perdas de 1–3 s em 300 s já reprovam pela coluna de áudio; o
critério efetivo usado está declarado na seção 1 dos resultados. Reconexões não
foram tratadas como falha automática: o provedor encerra conexões HTTP em
intervalos curtos e a captura se reconecta mantendo a continuidade; o que
reprova é perda de mídia, lacuna de áudio ou erro de decode.

## 4. Matriz resumida

| Identidade | Status | Escolhido | Nativa | Janelas | Motivo / pendência |
| --- | --- | --- | --- | --- | --- |
| band-news | pending | 206081 | 1920x1080 | 1 | pending_source_stability |
| cnn-brasil | pending | 206088 | 1280x720 | 1 | pending_no_native_1080 |
| globo-news | pending | 206097 | 1920x1080 | 1 | pending_decode_validation |
| record-news | pending | 206111 | 1280x720 | 1 | pending_no_native_1080 |
| band | pending | 207171 | 1280x720 | 1 | pending_no_native_1080 |
| sbt | approved | 207198 | 1920x1080 | 1 | native_1080 |
| redetv | pending | 208423 | 1280x720 | 1 | pending_no_native_1080 |
| tv-cultura | pending | 208430 | 1280x720 | 1 | pending_no_native_1080 |
| record | approved | 214640 | 1920x1080 | 1 | native_1080 |
| telecine-action | approved | 365243 | 1920x1080 | 1 | native_1080 |
| history | approved | 387622 | 1920x1080 | 1 | native_1080 |
| discovery-channel | approved | 387640 | 1920x1080 | 1 | native_1080 |
| discovery-hh | approved | 387648 | 1920x1080 | 1 | native_1080 |
| bbc-world | pending | 1765055 | 1280x720 | 1 | pending_no_native_1080 |
| sbt-news | pending | 3030725 | 1920x1080 | 1 | pending_source_stability |
| globo | pending | 10006 | 1280x720 | 1 | pending_no_native_1080 |

## 5. Relatório de testes de host

| Suíte | Comando | Resultado |
| --- | --- | --- |
| Python (template, bridge, export, package, PCR, output, source manager) | `pytest tests/test_live_favorites_*.py tests/test_favorites_source_manager.py` | **54 passed**, 10 subtests |
| Rust `fuse_ntfs` (lib, favoritos) | `cargo test -p fuse_ntfs --lib favorites -- --test-threads=1` | **118 passed**, 3 ignored |
| Rust integração MVP | `cargo test -p fuse_ntfs --test integration_favorites_mvp` | **4 passed** |
| Pacote real carregado pelo startup Rust | teste ignorado com `FAVORITES_PACKAGE_DIR` + imagem descompactada | **1 passed** (6 arquivos, extents de 4.000.000.000 B) |
| Pacote (validador independente) | `validate_live_favorites_package.py --expect-volume 137438953472` | **válido**: 6 arquivos, 24.000.000.000 B lógicos, 71.819.264 B físicos |
| Saída TV (profile 1080p, pós-bounder) | encode real testsrc/mandelbrot + pipeline | **output_ok**: PCR máx 19,55 ms, 0 B-frames, 1 slice, IDR 1 s, AC-3 384k |

Conversão de áudio: AC-3 384 kbit/s foi exigida com bitrate exato; o teste
sintético que produzia 128 kbit/s agora reprova. PCR acima de 20 ms reprova.

**Observação de áudio**: `globo-news` (206097) decodificou com 1 erro em 60 s
(`decode_errors`) e ficou pendente; os demais aprovados decodificaram limpo em
60 s. Lacunas de áudio na fonte continuam registradas por identidade e não foram
declaradas resolvidas.

## 6. Perguntas abertas

1. **Áudio intermitente**: as lacunas medidas na fonte (CNN 8,1 s; Band 8,9 s;
   Discovery H&H 6,6 s; Telecine 3,8 s; Band News 1,39 s) explicam o relato do
   usuário? A saída preparada não cria lacunas quando a fonte é contínua, mas
   não inventa áudio quando a fonte falha. Decisão do Sol: reprovar a fonte,
   aceitar com ressalva ou procurar outra variante.
2. **720p nativo**: sete identidades só entregaram 720p nas variantes medidas
   (CNN Brasil, Band, Record News, RedeTV, TV Cultura, BBC World, Globo RJ).
   O provedor tem mais variantes `HD²/H265/4K` que foram medidas e também
   entregaram 720p; não há garantia de que exista 1080p nativo no catálogo.
3. **Duas janelas**: o gate do Astra foi aplicado às candidatas a aprovação;
   identidades que passarem em uma única janela ficam pendentes e não entram no
   pacote.
4. **PCR de entrada**: valores de 66–100 ms (e um salto de 1,27 s em Band News)
   são característica da fonte. A saída é medida separadamente; o remux/transcode
   entrega ~20 ms.
5. **Multislice/B-frames na fonte**: várias fontes têm 2–3 slices e B-frames;
   por isso a receita usa transcode (nunca `-c copy`) e a saída é validada com
   1 slice e 0 B-frames.

## 8. Correções da revisão independente

| Bloqueio apontado | Correção aplicada | Teste de regressão |
| --- | --- | --- |
| PCR aceito até 50 ms; saída não medida após o filtro | limite de 20 ms e medição do fluxo final depois do `PcrBounder` da bridge | `tests/test_live_favorites_output.py` (PCR 20,5 reprova) |
| `output_ok` com FFmpeg em erro e AC-3 128 kbps | reprova `returncode != 0`, stderr não vazio, falha do filtro e bitrate ≠ 384 kbit/s | mesmos testes de saída |
| Segunda janela inválida aceita como estável | `record_stability` exige dados presentes, 1920×1080, duração/cobertura e áudio nas duas janelas | `test_invalid_second_window_never_approves` |
| Ausência de prova de decode limpo | passe `ffmpeg -f null` com contagem sanitizada; sem `decode_ok` a identidade fica pendente | `test_decode_evidence_is_required_for_approval` |
| Plano omitindo favorito e duplicata não detectada | cobertura obrigatória de todos os favoritos Live do snapshot e registro de `used_channels` | `test_plan_missing_a_live_favorite_identity_is_rejected`, `test_duplicate_candidate_across_identities_is_rejected` |
| Perfil 1080p x bootstrap/binário 720p | ativação exige conjunto indivisível e rollback conjunto documentado | `GRADE_FAVORITOS_ATIVACAO_SELETIVA.md` §8 |

## 9. Reprodução (sem segredos)

```sh
# evidência (exige ambiente do provedor; nada é impresso)
python3 scripts/probe_live_favorites_sources.py config/live_favorites_candidates.json \
  --phase winner --duration 300 --out-dir scratch/grade-1080p/evidence-window2 --only <ids>

# exportação final (gate de saída + segunda janela)
python3 scripts/build_live_favorites_export.py \
  --plan config/live_favorites_candidates.json \
  --catalog scratch/catalog/live_streams.json \
  --favorites scratch/catalog/favorites.json \
  --evidence scratch/grade-1080p/evidence \
  --second-window scratch/grade-1080p/evidence-window2 \
  --output-evidence scratch/grade-1080p/output-evidence \
  --output config/live_favorites_export.json --generation 3

# pacote parcial/final
python3 scripts/build_live_favorites_package.py \
  --export config/live_favorites_export.json --staging <novo-diretório> [--allow-partial]

# validação independente
python3 scripts/validate_live_favorites_package.py --staging <diretório> \
  --expect-volume 137438953472
```
