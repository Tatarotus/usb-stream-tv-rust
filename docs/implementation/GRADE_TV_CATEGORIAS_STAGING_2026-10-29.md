# Grade de favoritos em `TV/` — estado, matriz e pendências (preparação, NÃO ativado)

Data: 29/10/2026 · branch `feat/tv-live-grade-completa` · HEAD `5b6c4cd`.
Revisado após auditoria independente: evidência contraditória foi despromovida,
métricas não registradas deixaram de ser afirmadas e cada número passou a citar o
artefato de origem. **Nada foi ativado, publicado, reiniciado ou reconectado.**

---

## 1. Estrutura implementada (staging)

`/TV/<categoria>/<canal> 1080 <região>.trp` convivendo com `Series/<série>/…` e
`Filmes/…`, em um único volume NTFS de 128 GiB.

Pacote reconstruído em `scratch/tv-grade/pkg-full` com o inventário real
(117 VODs `ready`) e o conjunto corrigido de canais:

| Métrica | Valor |
| --- | --- |
| Arquivos | **122** (5 Live + 117 VOD) |
| Rejeitados / omitidos | **0 / 0** (`capacity_shortfall_bytes: 0`) |
| Volume | 137.438.953.472 B = **128 GiB** |
| Alocado (cluster) | 105.495.699.456 B = **98,3 GiB** |
| Livre | 30.869.512.192 B = **28,7 GiB** |
| `manifest_id` | `b1ff921f7ee22e181f6f3483b3a962f17fd1d77c9eea184c1fbb6d81f64a769d` |
| Hashes | `favorites_layout.json ad1d3cb07ffbbd87` · `favorites_manifest.json 4f956bbe8426ea7e` · `favorites_package.json 56d2ffa05ad108ab` · `favorites_template.sparse.gz 0fd62a2f6f92decc` · `vod_allowlist.json 05b8eae407232511` |

Validado pelos verificadores de startup do cliente: **PACOTE VALIDADO**
(hashes, layout, manifesto, allowlist e todos os registros MFT da imagem
expandida). Não provado: enumeração/reprodução física pelo decodificador.

Layout Live do pacote:

```
/TV/Abertos/SBT 1080 RJ.trp                     (207198)
/TV/Abertos/Record 1080 RJ.trp                  (214640)
/TV/Documentarios/History 1080.trp              (387622)
/TV/Documentarios/Discovery Channel 1080.trp    (387640)
/TV/Variedades/Discovery H&H 1080.trp           (387648)
```

## 2. Matriz por identidade — só o que cada artefato prova

Fonte: `evidence_ledger` do export `config/live_favorites_export_tv.json`, com o
sha256 de cada arquivo citado. J1/J2 = janela 1 e 2 (~300 s cada).

| Identidade | ID | J1 | J2 | decode | saída | Situação |
| --- | --- | --- | --- | --- | --- | --- |
| sbt | 207198 | 299,98 s, 0 lacuna > 500 ms, **sem `capture_coverage`** | 299,99 s, cobertura 1,0 | `decode_ok` (sem métricas de mídia no arquivo) | `output_ok` 12/12, `failed=[]`, 1080p30 | aprovada (testada na TV) |
| record | 214640 | 299,97 s, 0 lacuna, **sem `capture_coverage`** | 299,99 s, cobertura 1,0 | `decode_ok` (idem) | `output_ok` 12/12 | aprovada (testada na TV) |
| history | 387622 | 300,0 s, cobertura 1,0 | 300,0 s, cobertura 1,0 | `decode_ok` (idem) | `output_ok` 12/12 | aprovada com limites |
| discovery-channel | 387640 | 300,0 s, **sem `capture_coverage`** | 300,02 s, cobertura 1,0001 | `decode_ok` (idem) | `output_ok` 12/12 | aprovada com limites |
| discovery-hh | 387648 | 299,99 s, cobertura 1,0 | 300,01 s, cobertura 1,0 | `decode_ok` (idem) | `output_ok` 12/12 | aprovada com limites |
| telecine-action | 365243 | 300,01 s, cobertura 1,0 | 300,01 s, cobertura 1,0 | **CONFLITO**: embutida `decode_errors` (57,057/60 = **95,1 %**) × disco `decode_ok` | `output_ok` 12/12 | **PENDENTE** |

O que **não** é afirmado em nenhum lugar deste documento:

- que a J1 de sbt, record e discovery-channel tem cobertura 1,0 — o artefato não
  registra `capture_coverage`, então o valor fica `null`;
- que a evidência de `decode` comprova cobertura de vídeo — os arquivos
  `decode-*.json` não registram métricas de mídia;
- que a saída tem procedência forte — `output-*.json` não registra sha256 do
  bitstream nem carimbo de medição;
- que as medições **embutidas** no export para sbt e record são verificáveis: a
  captura delas tem tamanho diferente do arquivo em disco (15.383.476 ×
  15.446.268 e 20.621.156 × 20.483.916), ou seja, são de rodadas cujos artefatos
  não foram preservados. O citable é o arquivo em `evidence_ledger`.

## 3. Evidência contraditória: Telecine Action (despromovida)

A auditoria estava certa. Para o **mesmo ID 365243** existem duas rodadas com
vereditos opostos:

| Rodada | veredito | métrica | captura |
| --- | --- | --- | --- |
| embutida no export | `decode_errors` | `video_span_s` 57,057 em 60 s → **0,951 < 0,97** | 24.316.672 B |
| arquivo em disco (`decode/decode-telecine-action-365243.json`) | `decode_ok` | sem métricas registradas | 24.297.684 B |

Rodadas diferentes, bytes diferentes, vereditos diferentes: não há reconciliação
possível sem uma captura nova, e captura nova é sondagem ao vivo — proibida
nesta janela. Decisão: **pendente**, com `missing_evidence = "captura de decode
no ID 365243 com cobertura de vídeo registrada e ≥ 0,97"` registrado no export.
O ID alternativo favoritado (365244) já havia falhado a J1 (282,43 s, 1 lacuna >
500 ms, sem J2), então não existe identidade limpa para Telecine hoje.

## 4. Pendências de fonte (não omitidas)

11 identidades pendentes, todas registradas no export com `reason`,
`missing_evidence` e `next_test`:

| Identidade | Motivo | O que falta |
| --- | --- | --- |
| telecine-action | `pending_decode_conflict` | captura de decode ≥ 0,97 no ID 365243 |
| band-news | `pending_source_stability` | 2ª janela completa (a atual cobre 75 %) |
| sbt-news | `pending_source_stability` | 2ª janela estável em 1080p (J2 veio 720p) |
| globo-news | `pending_decode_validation` | decode limpo (`decode_error: 1` no resumo) |
| globo (RJ), band, cnn-brasil, record-news, redetv, tv-cultura, bbc-world | `pending_no_native_1080` | candidato 1080p nativo (as melhores medidas são 1280x720) |

Nenhuma alternativa 720p foi rotulada como 1080p: os vereditos medidos são
`not_1080p`. As prioridades do usuário estão destacadas em §4b.

## 4b. Prioridade do usuário (29/10): Globo, Telecine, Discovery e Band

Prioridade é **intenção de produto**; aprovação é **evidência**. O export agora
marca `priority` + `priority_rank` e mantém o bloco `priority` com o que ainda
**não** pode entrar na TV — **sem alterar um único status, medição ou aprovação**.

| # | Identidade | Situação | Motivo medido | O que falta |
| --- | --- | --- | --- | --- |
| 1 | globo (RJ) | **BLOQUEADA** | `pending_no_native_1080` | candidato 1080p **nativo** na região RJ |
| 2 | telecine-action | **BLOQUEADA** | `pending_decode_conflict` | captura de decode ≥ 0,97 no ID 365243 |
| 3 | discovery-channel | aprovada | — | limite: artefato de decode sem métricas de mídia; J1 sem `capture_coverage` |
| 4 | discovery-hh | aprovada | — | limite idem + sinal de instabilidade (a 1ª tentativa medida falhou) |
| 5 | band (SP) | **BLOQUEADA** | `pending_no_native_1080` | candidato 1080p nativo |
| 6 | globo-news | **BLOQUEADA** | `pending_decode_validation` | decode limpo (`decode_error: 1` hoje) |
| 7 | band-news | **BLOQUEADA** | `pending_source_stability` | 2ª janela completa (a atual cobre 75 %) |

Nada aqui promove 720p a 1080p: as melhores medidas de **Globo** e **Band**
permanecem `1280x720` (`not_1080p`), e **Telecine** continua pendente por
contradição de decode entre duas rodadas no mesmo ID. Nenhuma sondagem nova foi
feita: os candidatos saem apenas dos dados já existentes.

### 4c. Pesquisa local dos prioritários (planejamento, não aprovação)

Fonte: auditoria sobre **dados já existentes**. Nenhuma conexão IPTV nova, nada
sondado com a TV em uso. Vocabulário fixado no export (`priority.research`):
`measured` (resolução medida em artefato), `insufficient` (medição curta demais),
`unknown` (tentativa falhou), `conflict` (duas rodadas discordam), `to_measure`.

| Família | Candidato | Estado medido | O que isso significa |
| --- | --- | --- | --- |
| Globo | 10006, 10007 (capital RJ) | `measured` 1280x720 | **720p**; `not_1080p`, não aprova |
| Globo | 10012, 10017, 10022 (afiliadas RJ) | `unknown` | timeout na triagem: resolução **não comprovada** |
| Globo | **10001 (SP)** | `insufficient` | 20 s em 1080p **HEVC**; faltam janelas/decode/saída; programação regional de SP difere do RJ pedido |
| Telecine | **365242** (rótulo 4K) | `insufficient` | 20 s mediram 1080p; **rótulo comercial não é evidência** |
| Telecine | 365243 | `conflict` | decode contraditório entre rodadas → **pendente** |
| Discovery | 387640 Channel | `measured` 1080p | **aprovado**, faltam confirmação física e rastreabilidade das provas |
| Discovery | 387648 H&H | `measured` 1080p | **aprovado**, com instabilidade já observada (1ª tentativa falhou) |
| Band | 207170…207178, 3917940 | `measured` | 720p / sem 1080p comprovado; **não inferir resolução do nome** |

**Ordem de sondagem (regra: UM alvo por CANAL distinto; nunca outra variante de canal
já aprovado, para a grade ganhar diversidade):**

| # | Canal | ID | Medição de hoje | O que falta |
| --- | --- | --- | --- | --- |
| 1 | **Telecine Action FHD2** | 365245 | 1080p, **6.657 kbps** (melhor medição de Telecine) | duas janelas de 300 s + decode + saída |
| 2 | **Globo SP** | 10001 | 20 s em 1080p HEVC | completar janelas/decode/saída (programação regional difere do RJ) |
| 3 | **Globo News FHD1** | 206097 | 1080p, 6.711 kbps, decode com 1 erro | captura de decode limpa |
| 4 | **Globo RJ (afiliadas)** | 10012, 10017, 10022 | timeout, resolução não comprovada | remedir resolução, um por vez |
| 5 | **Band News FHD2** | 206081 | 1080p, J2 com 75 % de cobertura | segunda janela de 300 s ≥ 0,97 |
| 6 | **Band SP FHD1** | 207170 | 720p medido | remedir resolução nativa |
| 7 | Telecine Action H265 | 365243 | decode contraditório | captura de decode ≥ 0,97 (fecha a contradição) |

**Variantes excluídas da fila** (o canal já está aprovado — sondá-las não traz canal
novo): 207192/207193 (SBT SP), 214642 (Record Rio HD2), 387624 (History FHD1),
387641 (Discovery), 387649 (Discovery H&H).

**Confirmação física (não é sondagem)**: 387640, 387648, 387622 — aprovados por
evidência de host; falta abrir na TV.

**Fase posterior — a lista dos ~400**: o catálogo tem 3416 canais **sem campo de
resolução**; 434 têm rótulo FHD/1080/4K no nome e só 14 desses foram medidos (e 3
medidos em 1080p **sem** rótulo, ou seja o rótulo não é confiável em nenhum sentido).
Depois da fila acima, promover **canais novos** dessa lista (Globo 66, Record 21,
Discovery 21, Telecine 15, Band 5 sem medição), sempre medindo a resolução primeiro.

Regra mantida: nenhuma dessas identidades entra em `TV/` sem duas janelas de
300 s, decode limpo e saída 12/12 em **1080p nativo** no ID exato.

## 5. Migração de export: roteiro com prova e volta atrás

`scripts/migrate_export_to_tv.sh` (`check` / `apply` / `rollback`, 0755 como os
outros scripts). Revisado após auditoria externa; os defeitos demonstrados foram
corrigidos e cada um tem teste negativo próprio:

| # | Defeito demonstrado | Correção |
| --- | --- | --- |
| D1 | falha no `apply` restaurava unidades e bridge mas **nunca o timer** (parado em definitivo, em silêncio) | `undo_migration()` único desfaz tudo, inclusive o timer; se o timer não voltar, ele **diz** isso |
| D2 | prepare em voo > 60 s → `die` com o timer parado | espera de 900 s (mesma ordem do publisher) e o caminho de timeout passa por `undo_migration` |
| D3 | `restore_units` sem checagem: cópia parcial era reportada como sucesso (`\|\|` desativa `errexit` na função) | cada `cp` e o `daemon-reload` checados com `\|\| return 1`; `rollback` confere os leitores no fim |
| D4 | `rollback` bem-sucedido saía com **código 1** (usava `die` como mensagem) | `exit 0` explícito; `die` só para falha |
| D5 | revert/reinício comparavam com `$PREVIOUS` em vez do export da unidade restaurada | o esperado é lido de `unit_export` da unidade restaurada |
| D6 | journal ilegível passava em silêncio (fail-open) | caminho derivado do `--state-file` da unidade; sem journal legível o `apply` **recusa** (`MIGRATE_ALLOW_UNKNOWN_JOURNAL=1` só para exceção explícita) |
| D7 | nada provava que a sessão ativa poderia ser revertida antes de mutar | preflight roda a mesma prova do switch na sessão de `SWITCH_ROOT/current`; sem prova, **recusa** |
| D8 | o relatório prometia um guarda "≥3 referências" que **não existia** | reescrita literal em Python (sem `sed`), recusa de unidade que nomeia dois exports, e conferência individual dos **5 leitores** |
| D9 | `--publish` transferia o pacote obsoleto sem revalidar o export | `stale_publish_reason` bloqueia antes de tocar no tablet: `stale_for_export`, `stale_for_inventory` ou `inventory_unavailable` (fail-closed) |
| D11 | backup não atômico e escolha por mtime | backup montado em `.tmp` e renomeado no fim, com marcador `COMPLETE`; o rollback só usa backup íntegro |
| D12 | ruído de erro no caminho feliz; `sed` sem escape; só a 1ª ocorrência lida; `start` do timer não conferido | sonda silenciosa; reescrita literal; unidade com dois exports é recusada; o timer é conferido com `is-active` |

### Segunda rodada de auditoria (N1–N9)

| # | Falha reproduzida | Correção |
| --- | --- | --- |
| N1 | o undo dizia *"undone and verified"* sobre um pré-estado **já inconsistente** (exatamente o resíduo do D8 antigo) | `undo_migration` confere os **5 leitores** depois do reinício; se não concordarem, diz que a inconsistência é anterior e encerra com `RESTORE FAILED` |
| N2 | journal legível mas pacote pendente **incomparável** passava (fail-open) | `journal_report` devolve 3 para "UNREADABLE/NOT COMPARABLE"; o `apply` **recusa antes de mutar** e mostra o motivo (não silencia o diagnóstico) |
| N3 | `write_text` truncava a unidade de produção no caminho vivo | escrita atômica: arquivo temporário no mesmo diretório, **modo preservado**, `fsync` do arquivo **e** do diretório, `os.replace` |
| N4 | nenhum `trap`: sinal entre o backup e a reescrita deixava bridge/timer parados | `trap` INT/TERM/HUP durante o apply, com guarda de reentrância; a interrupção passa pelo mesmo undo |
| N5 | `apply` não exigia janela nem drenava o publisher | `MIGRATE_CONFIRM_WINDOW=1` obrigatório (nunca inferido de telemetria) + recusa se o **spool** do bridge tem `requests/` ou `processing/` ocupado |
| N6 | raiz de backup dentro do `APP_DIR` e sem checagem de posse | raiz fora do `APP_DIR`, modo 700, sem symlink; backup só é aceito se os arquivos forem regulares e do dono da unidade (ou root) |
| N7 | `sed` no restore do switch: `&` no caminho vira "match inteiro" | substituição literal em Python, como já era no migrador |
| N9 | a prova podia escolher candidato diferente do switch | mesma ordem de candidatos do switch, caminho provado **registrado no backup** (`active_session_export`) |
| D9 (vida longa) | a guarda lia o journal da memória | `fresh_journal` relê o arquivo em disco (escrito atomicamente: tmp + fsync + `os.replace`), sob o mesmo lock de estado |

### Terceira rodada (F1–F9) — bloqueios corrigidos

| # | Falha reproduzida | Correção |
| --- | --- | --- |
| **F1** | journal **corrompido** (JSON inválido) não é o código 3, então passava: o `apply` migrava com exit 0 | `journal_report` bloqueia em **qualquer** rc ≠ 0, e o Python converte exceção em 3 com mensagem própria (sem traceback solto) |
| **F2** | spool não localizado ⇒ apenas *warning* ⇒ `apply` prosseguia; e havia **TOCTOU** entre a checagem e o `stop` | spool desconhecido **recusa** (override explícito `MIGRATE_ALLOW_UNKNOWN_SPOOL=1`); `stop_bridge_verified` recheca **antes** e **depois** do stop, exige spool vazio e **nenhum publisher vivo** (varredura somente-leitura de `/proc`, discriminada por `argv[1]` + flags do publisher) |
| **F3** | 2º sinal durante o undo matava o script no meio (unidades restauradas, bridge parada, timer parado) | handler permanece armado durante a recuperação e o 2º sinal apenas o registra; o undo chega ao fim (verificado por teste que envia dois TERM) |
| **F4** | journal válido **sem pending** era tratado como corrupção e bloqueava | "no pending package journalled" é estado legítimo: exit 0, sem comparação |
| **F5** | o export provado era só registrado; o rollback não o usava | o rollback **lê** `active_session_export`, exige que ele exista e ainda case com o manifesto da sessão ativa, e imprime o comando do switch com `EXPORT_CONFIG_FILE=<provado>` |
| **F6** | correções do switch sem teste | Test 19 cobre o restore com lease contendo `manager_cmd`: caminho com `&` chega **literal** ao argv do manager |
| **F7** | `rollback` não reiniciava o timer | o rollback devolve o timer e reporta; teste 29 |
| **F8** | defaults novos não documentados | tabela de defaults/knobs abaixo |
| **F9** | backup não recusava **origem** symlinkada | `backup_units` recusa unidade symlinkada (teste 30) |

Bônus encontrado por F6 (e confirmado com `ask-astra`): o `&` no caminho não era só
um problema do `sed`; o lançamento do manager via `sh -c "exec $MANAGER_CMD"` fazia
o **shell reinterpretar** a string. Agora o export é citado na origem, a reescrita
é literal e o lançamento usa **um único parser** (`shlex.split` + `os.execvp`), que
preserva o PID (a prova de posse em `/proc` continua válida).

### Defaults e knobs reais do migrador (F8)

| Variável | Default | Nota |
| --- | --- | --- |
| `TARGET` | `/opt/usb-stream-tv/config/live_favorites_export_tv.json` | export com `TV/<categoria>/` |
| `PREVIOUS` | `/opt/usb-stream-tv/config/generation_g2_export.json` | precisa existir e ser usável |
| `APP_DIR` | `/opt/usb-stream-tv` | candidatos e scripts |
| `SWITCH_ROOT` | `/opt/usb-stream-tv/switch` | usado na prova da sessão ativa |
| `BACKUP_ROOT` | **`/var/lib/usb-stream-tv-migrations`** (mudou; antes `APP_DIR`) | raiz 0700, sem symlink; um backup do default antigo fica **invisível** ao rollback novo |
| `MIXED_BRIDGE_SPOOL` | vazio | o spool é descoberto por `--spool` na unidade da bridge |
| `PUBLISHER_SCRIPT` | `mixed_generation_service.py` | discriminador da varredura de publisher |
| `MIGRATE_CONFIRM_WINDOW` | `0` | **`apply` exige `1`**: janela autorizada pelo operador, nunca inferida |
| `MIGRATE_PREPARE_WAIT` | `900` | espera de preparação em voo, em segundos |
| `MIGRATE_ALLOW_UNKNOWN_JOURNAL` | `0` | override consciente quando não há journal comparável |
| `MIGRATE_ALLOW_UNKNOWN_SPOOL` | `0` | override consciente quando o spool não é localizável |

Reprodução do que a auditoria pediu para conferir (D8): o teste 3 do
`tests/test_migrate_export.sh` mexe **só** na linha de ambiente da unidade e
exige que o `check` aponte exatamente `bridge-unit(env)`. Um guarda que contasse
"quantos concordam" passaria nesse caso; o código confere os cinco leitores um a
um e falha se **qualquer** um divergir.

Sequência do `apply` (nada disso publica, reconecta ou desliga a TV):

1. `check`-like: alvo existe e é usável; anterior existe e é usável.
2. **Preflight D7**: a sessão ativa é provável contra um export em disco.
3. **Preflight D6**: o journal é localizável e legível.
4. Backup atômico das duas unidades.
5. Para o timer (registrando se estava ativo) e espera a preparação em voo.
6. Para a bridge, reescreve as unidades, `daemon-reload` (checado).
7. Reinicia a bridge e lê `/proc/<pid>/cmdline` **e** `environ` do PID real.
8. Confere os 5 leitores; inicia o timer e confirma `is-active`.
9. Relata o estado do journal e avisa para publicar **pelo painel**, nunca `--publish`.

## 6. O que mudou no código (com testes)

| Arquivo | Mudança |
| --- | --- |
| `scripts/build_mixed_media_package.py` | `live_root` opcional → `TV/<categoria>/`; validação de componente NTFS (separadores, `:*?"<>|`, NUL, 255 unidades UTF-16, `.`/`..`); rejeita caminhos Live que só diferem por caixa |
| `scripts/run_live_favorites_manager.py` | conjunto esperado derivado do **mesmo** gerador (fonte única); aceita profundidade 1 ou 2; rejeita Live duplicado |
| `scripts/validate_live_favorites_package.py` | aceita 1 ou 2 componentes; rejeita vazio/profundo demais |
| `scripts/switch_mixed_generation_vps.sh` | lease grava `export_config`; rollback prova o export da sessão; recusa quando não há export utilizável; `log_restore` deixa a decisão no `restore.log` da sessão |
| `scripts/migrate_export_to_tv.sh` (novo) | roteiro de migração com verificação do processo vivo e rollback automático |
| `config/live_favorites_export_tv.json` | 5 aprovadas, 11 pendentes, `evidence_ledger` com sha256 por artefato, `contradictions` e ressalvas |

## 7. Testes executados

| Suíte | Resultado |
| --- | --- |
| `pytest tests/ -q` | **634 passed, 255 subtests** |
| `tests/test_switch_mixed_generation_vps.sh` (21 testes) | PASS — inclui lease legado provado (18), export não provável com aviso (18b) e recusa sem export utilizável; o restore do `manager_cmd` agora é literal (N7) |
| `tests/test_migrate_export.sh` (30 testes, novo) | PASS — reload não troca o processo; 5 leitores conferidos individualmente; `apply` reinicia e prova argv/env; falha parcial reverte unidades + `daemon-reload` + **bridge** + **timer**; prepare em voo não deixa o timer parado; timer inativo não é iniciado; backup incompleto ignorado; `rollback` sai 0 e usa o export da unidade; recusa de alvo inválido, de sessão ativa não provada e de journal ilegível; unidade com dois exports recusada; **N5** janela não autorizada e publicação em voo bloqueiam; **N2** pacote incomparável bloqueia antes de mutar; **N1** o undo não alega "verificado" sobre pré-estado inconsistente; **N3** reescrita atômica preservando modo; **N9** export da sessão ativa registrado no backup; **N4** sinal no meio do apply não deixa timer nem bridge parados; **F1** journal corrompido bloqueia; **F4** ausência legítima de pending não bloqueia; **F2** spool desconhecido bloqueia e request reivindicada durante o stop é detectada; **F2** publisher vivo bloqueia o stop; **F3** dois sinais não interrompem o undo; **F5** rollback exige o export provado; **F7** rollback devolve o timer; **F9** unidade symlinkada não é backupeada |
| `tests/test_mixed_generation_service.py` (6 testes novos de D9) | PASS — pacote de outro export, inventário mudado e catálogo ilegível são bloqueados **antes** de tocar no tablet, e o caminho normal segue publicando; journal sem `inventory_hash` também bloqueia |
| `tests/test_activate_favorites_tunnel.sh`, `test_watchdog_favorites_handoff.sh`, `test_start_mixed_vod_proxy_vps.sh`, `test_pack_rc_live_release.sh` | PASS |
| `scripts/validate_mixed_package.sh` | **PACOTE VALIDADO** (122 arquivos) |

Os testes de migração usam systemd/processos simulados, matam **apenas** PIDs que
eles mesmos criaram (conferindo UID e cmdline) e não deixam resíduo.

## 8. Riscos e próximo passo

1. **Nada ativado**; a grade `TV/` existe apenas em staging.
2. **Histórico físico**: só SBT e Record abriram na TV. As outras três aprovadas
   têm evidência de host e podem falhar no decodificador — ativar em etapas.
3. **Capacidade**: 98,3 GiB de 128 GiB com 5 canais; cada canal Live custa 4 GB
   lógicos, então as 11 pendentes não cabem todas juntas.
4. **ConnectShare** exige re-selecionar o pendrive após cada troca de pacote.
5. **Limites de evidência** declarados na §2 continuam valendo: cobertura de J1
   ausente em 3 identidades e artefatos de `decode`/`output` sem métricas de
   cobertura e sem carimbo de medição.
6. **Sequência quando a TV estiver livre**: `migrate_export_to_tv.sh check` →
   `apply` (janela livre) → gerar a geração com o export `TV/` → publicar pelo
   botão → testar em etapas (SBT → Record → SBT com Return; depois **um** canal
   novo, sugerindo History ou Discovery Channel, já que Telecine ficou pendente)
   → série com seek → um filme.
