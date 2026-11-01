# Grade `TV/` — revisão do builder: pacote completo, configuração e auditoria

Data: 29/10/2026. Branch `feat/tv-live-grade-completa`, HEAD `5b6c4cd`.
Somente análise, código e testes de host. **Nada foi implantado, publicado,
reiniciado ou reconectado.** Produção intacta: geração **6 ativa**, geração
**7 preparada pelo timer** aguardando clique, bridge/timer ativos, container de
pé, `istanbullu-sync` preservada. Sem segredos neste documento.

---

## 1. Pacote COMPLETO validado (era o bloqueio nº 1)

`scratch/tv-grade/pkg-full` — inventário **real** de 29/10 (117 VODs `ready`,
79,6 GiB lógicos), export com `live_root: TV`, volume de **137438953472 B
(128 GiB)**.

| Métrica | Valor |
| --- | --- |
| Arquivos | **122** (5 Live + 117 VOD) |
| Rejeitados | **0** (`rejected_count: 0`) |
| Omitidos | **0** (`omitted_count: 0`, `capacity_shortfall_bytes: 0`) |
| Incluídos / pedidos | 117 / 117 |
| Volume | 137.438.953.472 B = **128 GiB** |
| Alocado (cluster) | 105.495.699.456 B = **98,3 GiB** |
| Livre | 30.869.512.192 B = **28,7 GiB** |
| `manifest_id` | `b1ff921f7ee22e181f6f3483b3a962f17fd1d77c9eea184c1fbb6d81f64a769d` |
| VOD novo (`61878388`) | **entra** |

Revisão de 29/10 pós-auditoria: o Telecine Action (365243) saiu do conjunto
aprovado por evidência contraditória (ver §3b) e o pacote foi reconstruído com
**5 canais**. Números anteriores deste documento (123 arquivos, 109,5 GB/26,9 GB
rotulados como GB quando eram GiB) foram corrigidos.

Conteúdo exato:

```
/TV/Abertos/Record 1080 RJ.trp                  (214640)
/TV/Abertos/SBT 1080 RJ.trp                     (207198)
/TV/Documentarios/Discovery Channel 1080.trp    (387640)
/TV/Documentarios/History 1080.trp              (387622)
/TV/Variedades/Discovery H&H 1080.trp           (387648)
/Series/A Noiva de Istambul/…                   (71 episódios)
/Series/Masterchef EUA/… (11)  /Series/Jaadugar A Witch in Mongolia/… (12)
/Series/Largados e Pelados/… (3)  /Series/100 Dias… (1)  /Series/Bluey/… (1)
/Series/Pesadelo na Cozinha com Gordon Ramsey/… (1)
/Filmes/…                                       (17)
```

Validado pelos **verificadores de startup do cliente** (`validate_mixed_package.sh`):
hashes, `Layout::parse`, `Package::verify`, `Manifest::validate`,
`parse_allowlist` e `verify_template_image` (todos os registros MFT da imagem
expandida) → **PACOTE VALIDADO**, 122 arquivos, `live=5 vod=117`.

Integração manager↔export sobre o pacote completo: **OK** (e a geração antiga
com o export antigo continua OK — compatibilidade preservada).

## 2. Configuração: quatro pontos precisam concordar (bloqueio nº 2)

Onde o export é lido hoje em produção:

| # | Componente | Referência atual |
| --- | --- | --- |
| 1 | bridge (unit) | `--export-config …/generation_g2_export.json` |
| 2 | bridge (unit) | `Environment=EXPORT_CONFIG_FILE=…/generation_g2_export.json` (herdado pelo switch) |
| 3 | prepare/timer (unit) | `--export-config …/generation_g2_export.json` |
| 4 | switch | `EXPORT_CONFIG_FILE` do chamador; default `$APP_DIR/config/live_favorites_export.json` (sempre sobreposto pelo item 2) |

**Defeito encontrado e corrigido:** o *lease* do switch **não registrava o
export**. Um `stop` que restaurasse a geração anterior herdaria o export do
chamador — ou seja, após migrar para `TV/`, um rollback traria a geração antiga
com o export novo e o manager recusaria o próprio manifesto (`manifest_matches_export`),
deixando a TV sem Live. Agora:

- `_write_lease` grava `export_config=` da sessão;
- `_restore_previous` restaura usando **o export daquela sessão** (e mantém o
  comportamento anterior quando o lease é antigo e não traz o campo).

Prova: `tests/test_switch_mixed_generation_vps.sh` → *Test 17* — N com export A,
N+1 com export B e `--supersede`, `stop N+1` → o manager restaurado é lançado com
**A**, nunca com B (e um segundo `stop` é inerte).

Migração preparada e **não executada**: `scripts/migrate_export_to_tv.sh`
(`check` só lê e mostra o que difere; `apply` troca os 3 pontos de unidade com
`daemon-reload` e **reescreve as unidades literalmente em Python** (sem `sed`: o
caminho contém pontos e `&` seria retroreferência). Antes de mutar, cada unidade é
lida: se nomear dois exports diferentes, ou nenhum, a migração **recusa** em vez de
reescrever só parte. Depois, os três textos são conferidos um a um contra o alvo e
os **cinco leitores** (unidade argv, unidade env, prepare, processo argv, processo
env) precisam concordar; `rollback` restaura do backup íntegro. Ela **não publica
nada**: a grade nova só vale na publicação seguinte, pelo painel.

## 3. Auditoria das evidências primárias (bloqueio nº 3)

Fontes: `evidence/winner-*.json` (janela 1), `evidence/window2/winner-*.json`
(janela 2), `evidence/decode/` (60 s decodificados), `evidence/output-*.json`
(pipeline real + `PcrBounder`), `evidence/attempts/`.

| Identidade | ID exato | J1 | J2 | Decode | Saída |
| --- | --- | --- | --- | --- | --- |
| sbt | 207198 | 299,98 s · 0 lacuna · **cobertura não registrada** | 299,99 s · cov 1,0 · 0 | `decode_ok` (sem métricas no arquivo) | 12/12, `failed=[]` |
| record | 214640 | 299,97 s · 0 · **não registrada** | 299,99 s · cov 1,0 · 0 | `decode_ok` (idem) | 12/12 |
| telecine-action | 365243 | 300,01 s · cov 1,0 · 0 | 300,01 s · cov 1,0 · 0 | **CONFLITO**: embutida `decode_errors` (95,1 %) × disco `decode_ok` | 12/12 |
| history | 387622 | 300,0 s · cov 1,0 · 0 | 300,0 s · cov 1,0 · 0 | `decode_ok` (idem) | 12/12 |
| discovery-channel | 387640 | 300,0 s · 0 · **não registrada** | 300,02 s · cov 1,0001 · 0 | `decode_ok` (idem) | 12/12 |
| discovery-hh | 387648 | 299,99 s · cov 1,0 · 0 | 300,01 s · cov 1,0 · 0 | `decode_ok` (idem) | 12/12 |

O que a auditoria apontou e ficou **corrigido** neste documento:

- **Telecine Action 365243 não é aprovável**: há duas rodadas de decode no mesmo
  ID com vereditos opostos (a rodada embutida no export falhou `video_coverage`
  com 57,057/60 = 95,1 % < 97 %; o arquivo em disco diz `decode_ok`, com captura
  de outro tamanho: 24.316.672 × 24.297.684 B). Sem nova captura não se
  reconcilia → identidade **pendente** e pacote reconstruído.
- **History, Discovery Channel e Discovery H&H** tinham `decode_verdict`/`output_verdict`
  nulos no export enquanto o documento afirmava provas. Agora cada identidade cita
  os artefatos por nome, com sha256 do arquivo, em `evidence_ledger`, e as
  ausências ficam declaradas: os `decode-*.json` não registram métricas de mídia e
  os `output-*.json` não registram sha256 do bitstream nem carimbo de medição.
- **Cobertura da J1 de SBT, Record e Discovery Channel não é afirmada como 1,0**:
  o artefato não tem `capture_coverage`.
- **Medições embutidas de SBT/Record não são verificáveis**: a captura delas tem
  tamanho diferente dos arquivos em disco, logo vêm de rodadas cujos artefatos não
  foram preservados. O citável é o arquivo listado no livro-razão.

As inconsistências que continuam sendo de variante/tentativa (e não do ID
publicado) estão em `evidence_caveats`:

| O que a revisão anterior apontou | Arquivo/medição que explica |
| --- | --- |
| Telecine com cobertura insuficiente | havia **duas** coisas: a variante favoritada **365244** (`winner-telecine-action-365244.json`: 282,43 s, 1 lacuna > 500 ms, **sem J2**) e o **conflito de decode no próprio 365243**, que é o motivo da pendência. |
| History com erro de decode | não há erro no ID publicado: `decode/decode-history-387622.json` = `decode_ok`. O que falha é a variante favoritada **387624**: `window2/winner-history-387624.json` = `no_media`. |
| Discovery H&H com cobertura insuficiente | `attempts/winner-discovery-hh-387648-attempt1.json` = `input_error`, 252,31 s, 5 lacunas > 500 ms — tentativa **repetida com sucesso**; a rodada vencedora e a J2 estão completas. Fica como **sinal de instabilidade**. |
| "globo-news" com erro de decode | confirmado: `decode-summary.csv` traz `{decode_error: 1}` para 206097 → permanece **pendente**. |

O que **não** resolve e por quê: um CSV de decode de 60 s ou um rótulo
`output_ok` isolado não cobrem o tempo — por isso a tabela cruza **duas janelas
independentes** com decode e saída por ID exato. Nenhuma sondagem nova foi feita.

## 4. Pendentes e alternativas medidas (bloqueio nº 4)

São **11** identidades pendentes: as 10 abaixo mais `telecine-action`
(`pending_decode_conflict`).

| Identidade | Variantes medidas | Melhor medida | Nativa | O que falta |
| --- | --- | --- | --- | --- |
| globo (RJ) | 6 | 10006 Globo Rio FHD1 | 1280x720 | candidato 1080p nativo RJ |
| band | 8 | 207170 | 1280x720 | candidato 1080p nativo (RJ tinha lacuna de áudio) |
| cnn-brasil | 5 | 206087 | 1280x720 | candidato 1080p nativo |
| record-news | 5 | 206110 | 1280x720 | candidato 1080p nativo |
| redetv | 4 | 208421 | 1280x720 | candidato 1080p nativo |
| tv-cultura | 4 | 208429 | 1280x720 | candidato 1080p nativo |
| bbc-world | 2 | 1765055 | 1280x720 | candidato 1080p nativo |
| band-news | 5 | 206081 | 1920x1080 | 2ª janela completa (a atual cobre 75 %) |
| sbt-news | 4 | 3030725 | 1080p→720p | 2ª janela estável em 1080p |
| globo-news | 3 | 206097 | 1920x1080 | decode limpo |

Nenhuma alternativa 720p foi rotulada como 1080p: os vereditos são `not_1080p`
na própria medição.

## 5. Testes

- `pytest tests/ -q` → **634 passed, 255 subtests**.
- Suítes shell: túnel/lease, watchdog, **switch VPS (20 testes: inclui lease
  legado provado e recusa sem export utilizável)**, proxy VPS,
  `pack_rc_live_release` e **`test_migrate_export.sh` (14 testes, ver abaixo)**
  → **PASS**.
- `validate_mixed_package.sh` no pacote completo → **PACOTE VALIDADO**.
- Revisão externa do código de geração (recortes sanitizados, arquivos não versionados): 3
  achados corrigidos — (a) o manager não replicava `strip()`/categoria
  padrão/`.trp` do gerador → agora deriva o esperado do **mesmo** gerador
  (fonte única); (b) colisão de caminho em volume **case-insensitive**; (c) NUL e
  limite de 255 unidades UTF-16. Confirmou também que `file_id`/`bootstrap_id`
  não dependem do caminho.

## 6. Artefatos e hashes

| Arquivo | sha256 (16) |
| --- | --- |
| `scripts/build_mixed_media_package.py` | `a6f285f44ab2d861` |
| `scripts/run_live_favorites_manager.py` | `310423d201b8222d` |
| `scripts/validate_live_favorites_package.py` | `8c8f125a86356599` |
| `scripts/switch_mixed_generation_vps.sh` | `5c5ee6a9b33d66bb` |
| `config/live_favorites_export_tv.json` | `fb30a41ddcce990b` |
| `scripts/migrate_export_to_tv.sh` | `22e10aebd115380f` |
| `tests/test_migrate_export.sh` | `d7aa2284705b5607` |
| `scripts/mixed_generation_service.py` | `9b892b43e79be30f` |
| `tests/test_mixed_generation_service.py` | `03cfb5186ebf9ebc` |
| `tests/test_mixed_media_package.py` | `40c7935172d45bee` |
| `tests/test_live_favorites_bridge.py` | `6f4316a5af43cbe3` |
| `tests/test_switch_mixed_generation_vps.sh` | `9908491c37729529` |

Pacote: `favorites_manifest.json 4f956bbe8426ea7e` · `favorites_layout.json ad1d3cb07ffbbd87`
· `favorites_package.json 56d2ffa05ad108ab` · `favorites_template.sparse.gz 0fd62a2f6f92decc`
· `vod_allowlist.json 05b8eae407232511` · `manifest_id b1ff921f7ee22e181f6f3483b3a962f17fd1d77c9eea184c1fbb6d81f64a769d`
(a imagem NTFS é esparsa: ~69 MB físicos para 128 GiB virtuais).

## 7. Configuração de ativação (para quando a TV estiver livre)

1. **Migrar o export** nos 3 pontos + reiniciar o processo:
   `scripts/migrate_export_to_tv.sh apply` (backup automático, reinicia a bridge,
   lê `/proc/<pid>/cmdline` e `environ` do PID real e reverte sozinho se algo
   falhar; `check` mostra antes o estado de cada leitor).
2. **Gerar a geração** no VPS com o export TV (mesmo pipeline, mesmo volume,
   `Series/` e `Filmes/` intactos).
3. **Publicar pelo clique** (com aviso de reconexão) e confirmar no tablet.
4. **Testar em etapas na TV**: SBT → Record → SBT com Return; depois **um** canal
   novo (sugestão: Telecine Action); depois um documentário; depois o resto;
   série com seek; um filme.

## 8. Rollback

| Camada | Ação |
| --- | --- |
| Export/unidades | `migrate_export_to_tv.sh rollback` |
| Serviços da geração | `switch_mixed_generation_vps.sh stop <session>` (restaura a anterior **com o export dela**) |
| Tablet/LUN | `activate_live_favorites_tablet.sh rollback <pkg>` |
| Dados da grade | remover a identidade do export e gerar nova geração (não mexe em código) |

## 9. Pendências e riscos

1. **Nada ativado**; a grade `TV/` só existe em staging.
2. **Historico físico**: só SBT e Record abriram na TV. Os outros quatro têm
   evidência de host (fonte + decode + saída) e **podem falhar na TV** — ativar
   em etapas.
3. **ConnectShare** exige re-selecionar o pendrive após cada troca (observado 2×).
4. **Instabilidade sinalizada**: Discovery H&H exigiu repetir a medição;
   band-news e sbt-news falharam a 2ª janela.
5. **Capacidade**: 98,3 GiB de 128 GiB alocados com os 5 canais; cada canal Live
   novo custa 4 GB lógicos. Com as 16 identidades não caberia — por isso as
   pendentes ficam de fora.
6. **Divergência de variante**: para Telecine e History o arquivo publicado não é
   o ID favoritado (o favoritado falhou a evidência). Se o usuário preferir o
   favoritado, é decisão dele — e aí o canal volta a pendente.
7. **Não verificado**: enumeração/reprodução física, estabilidade por horas e a
   atualização de VOD depois da migração.
