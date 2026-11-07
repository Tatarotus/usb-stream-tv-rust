# PROGRESSO — Gerações V2

Atualizado: 2026-11-06. **Este arquivo é o ponto de partida para retomar.**

Especificação: `docs/architecture/GERACOES_V2.md`
Prompt do builder: `docs/prompts/GERACOES_V2_BUILDER.md`

---

## Estado: FASE 1 COMPLETA E VALIDADA; FASE 2 em andamento (1/5 itens)

### Feito e commitado

**Commit `090d0f5`** — `feat(ops): store persistente de operacoes com id,
idempotencia e eventos`

| Arquivo | O que é |
| --- | --- |
| `scripts/mixed_operations.py` | Store SQLite: operações, eventos, outbox, fencing |
| `tests/test_mixed_operations.py` | **40 testes**, todos passando |
| `scripts/mixed_ops_api.py` | Adapter HTTP fino: pedido → operação, operação → payload público |
| `tests/test_mixed_ops_api.py` | **19 testes + 5 subtests**, todos passando |

Cobertura real (medida, não presumida):

- `operation_id` durável; chave idempotente escopada a `(principal, tablet_id)`
  com hash da intenção.
- Mesma chave + mesma intenção → operação original. Mesma chave + intenção
  diferente → `IdempotencyConflict` nomeando a original. Chave expirada →
  recusada, nunca reciclada.
- Uma operação não-terminal por tablet, por **índice único parcial** no SQLite.
- Terminal + evento na **mesma transação**.
- `needs_reconciliation`/`interrupted` seguram o slot; `resolve_reconciliation`
  exige prova registrada e é a única saída.
- `pendency_closed` separa *fechar a pendência* de *dispensar o aviso*.
- Fencing por epoch em `finish()` **e** `set_phase()`; terminal não reabre.
- `snapshot(cursor)` numa única transação, incluindo pendências fora do cursor.
- `bytes_done`/`bytes_total` = `NULL` quando não medidos.
- Outbox deduplicado, fora do caminho crítico.

Suite combinada: **220 testes Python + 33 testes shell**.

### Não feito (é onde retomar)

| Passo | Arquivo | Nota |
| --- | --- | --- |
| ~~Ligar o adapter em `server.py`~~ ✅ feito (`fa7350f`) | `server.py` | `/api/mixed/apply` aceita `idempotency_key`, devolve `operation_id` |
| ~~Polling global + aviso persistente no painel~~ ✅ feito (`f8c0226`) | `dashboard.html` | laço único, 2s/15s/backoff, não expira |
| ~~`/api/mixed/events` com cursor~~ ✅ feito (`2d74a0c`) | `server.py` | — |
| `/api/mixed/refresh` ainda não cria operação | `server.py` | só o `apply` passa pelo store hoje |
| Fases 2 a 5 | vários | ver especificação |

O adapter (`mixed_ops_api.py`) é chamável hoje: `request_apply` devolve 202 +
`operation_id` (ou 409 com `active_operation_id`), `request_status` responde por
id e `request_events` entrega cursor + pendências. Falta só o encanamento HTTP.

**Nada foi implantado.** O módulo não é importado por ninguém ainda.

---

## Validação da integração da fase 1 (executada)

As três perguntas do operador, respondidas com medição:

### 1. O worker real atualiza a operação persistida durante a execução?

**Não atualizava.** Medido: `grep` por `mixed_operations|OperationStore` dava
**0 ocorrências** em `mixed_generation_service.py` e em `mixed_bridge.py`; só
`server.py` usava o store.

Efeito medido: o endpoint criava a operação em `preparing`, que pertence a
`ACTIVE_STATES`. Sem `finish()`, a linha ficava em `preparing` **para sempre**,
segurando o slot e mostrando no painel uma publicação que nunca termina.

Não era regressão do botão — o `except` do endpoint engole o `TabletBusy` e a
publicação segue. Era **progresso falso** na tela.

**Corrigido** (`fd06c17`): `_OperationTracker` no publisher, que adota a operação
ativa do tablet e a finaliza conforme o desfecho real. 6 testes novos.

### 2. Falhas reais chegam ao evento e ao aviso persistente?

**Agora sim.** O mapa `_OPERATION_OUTCOME` traduz o resultado do publisher em
desfecho da operação: `failed`/`incomplete` → `failed_safe` (pendência visível);
`unconfirmed`/`unknown` → `needs_reconciliation` (nunca falha limpa, porque o
dispositivo pode ter sido tocado); `in_progress` **não** é finalizado aqui —
essa operação é de outro worker.

### 3. Reiniciar o worker ou reabrir o painel preserva o resultado?

**Sim, por construção.** O estado vive em SQLite, e o painel busca
`/api/mixed/events` desde `cursor=0` ao abrir, incluindo pendências não
resolvidas independentes do cursor. Testes existentes provam o snapshot e a
sobrevivência a uma instância nova do store.

### Transação do chamador durante RPC (a pergunta mais fina)

**Garantido por construção, não por disciplina.** Medido com `ast`: os 18
métodos públicos do store são chamadas discretas; os únicos `contextmanager`
(`_tx`, `_ro`) são **internos**; **0** métodos devolvem conexão ou
contextmanager ao chamador. Logo é impossível segurar uma transação do store
através de um RPC — a API não oferece como.

E o publisher chama `tracker.phase(...)` antes e `tracker.finish(...)` depois,
cada um abrindo e fechando a própria transação. O `apply_task()` corre entre
eles, sem transação aberta.

### O que esta validação NÃO prova

Testes sintéticos não provam a TV. Nada foi implantado: o container roda um
`server.py` anterior e `MIXED_OPERATIONS_DB` não está definido em produção —
sem ele, o comportamento é exatamente o antigo.

## Fase 2 — estado item a item (medido)

| # | Item | Estado |
| --- | --- | --- |
| 1 | Ação `describe` no supervisor do tablet | ✅ implementado e testado (`0d02159`; Test 32/33 no shell) |
| 2 | Identidade independente do caminho | ✅ implementado e testado |
| 3 | Reconciliação da localização observada (CAS) | ✅ implementado e testado |
| 4 | Handoff vinculado à instância observada | ✅ implementado e testado |
| 5 | Revalidar identidade sob a guarda antes da mutação | ✅ implementado e testado |
| 6 | Correlação de `_OperationTracker` por intenção/revisão/execução | ✅ implementado e testado |
| 7 | Coordenação separada de notificação (falha fechada) | ✅ implementado e testado |
| 8 | `MIXED_OPERATIONS_DB` compartilhado + migração explícita | ✅ implementado e testado em sandbox; criação na VPS **não executada** |
| 9 | `/api/mixed/refresh` cria operação durável | ✅ implementado e testado |
| 10 | Revisão reservada correlacionada entre endpoint e publicador | ✅ implementado e testado |

**Nada foi implantado.** Ver a seção "Fase 2 — implementação" mais abaixo para
código, testes e pendências reais.

### Correção deste registro

Trechos antigos deste arquivo (mantidos abaixo por histórico) diziam "❌ não
implementado" para os itens 2 a 5 e descreviam a identidade como "derivada de
`_package_dir_on_tablet`". Isso **deixou de ser verdade** nesta rodada: código e
testes são a evidência primária, e a identidade agora é a encarnação do processo.
O texto histórico não deve ser usado como estado atual.

### O que o item 1 entrega

```
describe protocol=<> boot_id=<> generation=<> manifest_id=<>
         package_digest=<> runtime_digest=<>
         supervisor=<pid>-<starttime> candidate=<pid>-<starttime>
         lun=<> lock_owned=<0|1> path=<>
```

Puramente aditivo: novo ramo no `case`, nenhuma ação existente alterada.

**Reuso, não reimplementação:** `proc_incarnation` (pid + campo 22 de
`/proc/<pid>/stat`) já existia e já era usada em `handoff.takeover` e
`verify-active`. A primitiva de identidade que a §4 exige já estava lá; faltava
expor. Isso torna o item 4 mais barato do que parecia: o handoff já tem a
encarnação, falta negociar por ela em vez de por `package_dir`.

**Campos honestos:** sem `boot_id` (kernels Android costumam não ter
`/proc/sys/kernel/random/boot_id`) o campo reporta `uptime:<s>`, dizendo que é
substituto mais fraco, em vez de se passar por boot_id.

### Revisões da integração (feitas antes, a pedido)

* **Correlação por `task_id`**, não por tablet. Medido ao implementar: o *hash*
  da intenção não serve, porque o endpoint registra `confirm_interrupt` e o
  publisher não — trabalho igual hashearia diferente. Coluna `task_id`
  adicionada (migração aditiva).
* **Coordenação separada de notificação.** `claim()` falha fechado antes de
  qualquer efeito; `phase()`/`finish()` seguem best-effort.

## Restrições que continuam valendo

- **Não reiniciar** serviços, container, bridge, USB ou TV sem janela livre
  explicitamente autorizada. Bitrate ou silêncio **nunca** são autorização.
- **Não tocar** `istanbullu-sync`. **Não** fazer sondagens IPTV (uma conexão só).
- **Não** imprimir PIN, tokens ou URLs privadas.
- **Não** enfraquecer locks para contornar `previous_generation_owns_lock`.
- **Não** fazer reset/clean/stash/checkout destrutivo: a árvore tem trabalho de
  outros agentes (15 arquivos pendentes no momento deste registro).
- Telegram: implementar adaptador/outbox com testes falsos. **Não enviar
  mensagens reais** sem autorização de envio.

---

## Estado operacional observado (somente leitura)

```
active_generation : 18
active_session    : mixed-gen18-0ca1b784
pub.phase         : failed      <- publicação 19 foi recusada
listeners         : 2 (18190, 18191)
```

A TV estava servindo a gen18. O tablet havia voltado ao modo legado após
reboot e foi recuperado em pasta alternativa por outro agente — exatamente o
cenário que a §4 da arquitetura trata. **Não corrigir removendo a checagem de
dono**: a raiz é dependência de caminho/identidade.

---

## Achados da revisão independente (`ask-astra --deep`)

Sobre recorte sanitizado de `scripts/mixed_operations.py`. Três acatados com
correção **e teste próprio**; um recusado com motivo:

| Achado | Veredito | Como ficou |
| --- | --- | --- |
| Checar slot ativo dentro da transação, não após `IntegrityError` | acatado | re-ler corria com a outra operação terminando e devolvia id vazio |
| Validar fencing também em `set_phase` | acatado | worker superseded não escreve fase |
| `snapshot()` lendo pendências 2× fora de transação única | acatado | agora uma transação |
| Trocar `AUTOINCREMENT` por rowid puro | **recusado** | cursor durável não pode reusar seq após remoção |

**Limitação do revisor:** a saída veio truncada no topo; foram avaliados os
trechos visíveis. Não é auditoria de implementação.

---

## Defeito encontrado e corrigido durante a própria verificação

`ds-unstick` aplicado ao meu trabalho: eu havia assumido que `failed_safe`
pertence sempre a `UNRESOLVED_STATES`. Isso vale para falha *durante* a
publicação, mas não para uma criada *pela* reconciliação. Medido: após
reconciliar com prova, a operação continuava pendente — ou seja, reconciliar
não mudava nada na tela, contradizendo o contrato 5 da §10. Corrigido com a
coluna `pendency_closed` e teste que prova `unresolved` 1 → 0 e slot liberado.

---

## Como retomar

1. Ler `docs/architecture/GERACOES_V2.md` e o prompt do builder **integralmente**.
2. Confirmar branch e árvore suja (`git status`) antes de agir.
3. Continuar da integração: `server.py` primeiro, mantendo compatibilidade de
   `/api/mixed/apply`, `/refresh`, `/status` durante a migração.
4. Rodar `python3 -m pytest tests/test_mixed_operations.py -q` (deve dar 40).
5. Só depois painel, depois fases 2–5.

**Ordem sugerida das fases** (§8 da arquitetura): observabilidade → identidade
reconciliação → runtime/transporte → máquina de estados → janela física.

## Revisão independente sobre a ligação no server.py

`aider-argon` revisou o recorte do endpoint. **Ele editou `server.py` diretamente,
o que não foi pedido** — avaliado e parcialmente aceito, com teste próprio:

| Sugestão | Veredito | Por quê |
| --- | --- | --- |
| `if created:` antes de `set_phase` | aceito | sem a guarda, clique duplicado rebobinava a fase de uma operação viva |
| remover `operation_id = None` do except | aceito | a variável já nasce `None`; manter o id permite registrar `failed_safe` |

Teste `test_duplicate_click_does_not_reset_the_phase_of_a_running_operation`
prova a guarda.

**Lição operacional:** ao usar `aider-argon` com `--read`, ele ainda pode escrever
em arquivos do repositório. Conferir `git status` depois de cada invocação.

## Pendência imediata antes de qualquer deploy

O container roda um `server.py` **anterior** (`20168a4b5cd6`). Para o endpoint
novo valer, é preciso recriar o container — o que **interrompe a TV**. Pende de
janela livre autorizada. Além disso, `MIXED_OPERATIONS_DB` precisa ser definido
para o store ligar; sem ele o comportamento é exatamente o antigo.

## Fase 2 — plano (identidade e reconciliação do tablet)

Objetivo: reconhecer a geração real pelo supervisor, sem depender do nome da
pasta — que é o que bloqueou a publicação 19 (`previous_generation_owns_lock`).

| Passo | Aceite |
| --- | --- |
| 1. `describe` no supervisor do tablet | devolve protocolo, device_id, boot_id, instance_id, PID/starttime, geração, manifest_id, digests, caminho, LUN, lock_owned |
| 2. Identidade validada por posse | caminho é localização, não identidade; PID/instance/starttime impedem confusão após reboot |
| 3. Reconciliar localização observada | CAS no registro ativo; nunca adotar pasta só por conter o mesmo manifesto |
| 4. Lease com fencing e ack durável | ack ligado a operation_id + boot_id + instance_id; perder a guarda impede avanço |
| 5. Supervisor antigo não é morto | só recuperado por roteiro supervisionado em janela livre |

**Não enfraquecer** `previous_generation_owns_lock`: a raiz é dependência de
caminho/identidade, não a checagem de dono.

### Achado do passo 1 (já executado)

Lido o protocolo. A prova de identidade **já é bem desenhada**:

```python
HANDOFF_PROTOCOL_ANNOUNCEMENT = "handoff.ack 1"
# "The RUNNING supervisor's own announcement is the proof. Reading the script
#  file is not: a later revision can replace it while the old process still
#  supervises."
```

Ou seja: a prova é o **processo em execução**, não o arquivo. Isso já está certo.

**A lacuna real é outra:** toda a identidade é derivada de
`self._package_dir_on_tablet(session)` — isto é, **caminho**. Foi exatamente isso
que recusou a publicação 19 (`previous_generation_owns_lock`): a geração 18 foi
recuperada em pasta alternativa, então o caminho registrado não corresponde ao
processo que está de fato supervisionando.

Confirma a §4 da arquitetura: *"caminho é localização, não identidade"*.

### Próximo passo concreto

Adicionar a ação `describe` em
`scripts/activate_live_favorites_tablet.sh` de forma **puramente aditiva** (nova
ação no `case`, nenhuma existente alterada), devolvendo a tupla:
protocolo, device_id, boot_id, instance_id, PID+starttime, geração, manifest_id,
package_digest, runtime_digest, caminho, LUN e lock_owned. Depois fazer
`handoff_lease` negociar por essa tupla em vez de por caminho.

Provar com `tests/test_activate_favorites_tunnel.sh` (suíte existente) mais um
teste novo para a ação. **Não** enfraquecer `previous_generation_owns_lock`.

---

## Fase 2 — implementação (medida nesta rodada)

### Identidade: encarnação, nunca caminho

`scripts/activate_live_favorites_tablet.sh`

- `describe` devolve protocolo, boot_id, geração, manifest_id, digests,
  `supervisor=<pid>-<starttime>`, `candidate`, `lun`, `lock_owned` e o caminho.
- `lease-write` **não tem mais fallback por caminho**. Um dono vivo só é
  autorizado quando a incarnação que o chamador observou é igual à do dono
  recalculada no tablet, sob o lock. Dono divergente ⇒
  `previous_generation_owns_lock`.
- `handoff-takeover` exige a incarnação observada (`lock_incarnation_unproven`
  sem ela) e compara **antes** de olhar qualquer pasta. O caminho deixou de
  autorizar sinal; sobrou só para localizar o diretório da marca. Ordem final:
  lock livre → sucesso `already_free`; dono vivo ilegível → recusa; sem
  incarnação observada → recusa; incarnação diferente → recusa; só então a
  marca e o TERM.

`scripts/mixed_generation_service.py`

- `SupervisorIdentity` (já existia) + `observe_active_supervisor()`: descobre o
  supervisor pelo **registro protegido** (`mode_switch.lock/pid`), lê a
  `cmdline` do dono só para achar o script da revisão em execução, e usa o
  `describe` **desse processo** como prova. Confere que a incarnação respondida
  começa pelo pid do dono; discordância ⇒ `needs_reconciliation`.
- `reconcile_location(observation, generation, manifest_id)`: exige prova
  completa (respondeu, `lock_owned=1`, incarnação ≠ `none`, geração e
  manifest_id gravados) e troca `active_session` por **CAS** — sessão, geração
  e manifest revalidados dentro do lock. Pasta com o mesmo manifesto e outro
  dono é recusada (`observed_manifest_differs` /
  `observed_supervisor_does_not_own_lock` / `observed_incarnation_unproven`).
- `publish_pending` passo 7: observa → reconcilia → `handoff_lease` com a
  incarnação → `handoff_takeover(observed=...)`. Identidade conflitante
  **bloqueia** com `needs_reconciliation` e desfaz os serviços do lado servidor;
  nada é sinalizado e o LUN não é tocado.
- `previous_supervisor_capability`: a prova passou a ser o **anúncio da revisão
  em execução** (`describe` → `handoff.ack 1`), não o arquivo
  `activation/handoff.protocol`, que uma revisão posterior pode substituir
  enquanto o processo antigo ainda supervisiona. Supervisor antigo sem
  `describe` ⇒ recusa antes de qualquer sinal.
- `revalidate_supervisor()`: segunda leitura imediatamente antes da mutação.
  `handoff_takeover` observa por conta própria quando não recebe identidade, e
  recusa em vez de sinalizar por nome de pasta.

### Coordenação separada de notificação

`scripts/mixed_operations.py`

- Colunas novas (migração aditiva): `intent_revision`, `executor`,
  `executor_claimed_at`. `SCHEMA_VERSION` vai a `2` e o valor gravado é
  atualizado no arquivo antigo, não só criado em bancos novos.
- `claim_execution(operation_id, task_id, revision, executor)`: CAS que
  **grava qual execução é dona** da operação. Mesma execução pode reclamar
  (retry interno); execução diferente, tarefa diferente ou revisão diferente
  (quando ambas declaradas) ⇒ `OperationsError`.
- `identity()`: caminho, `device:inode`, `instance_id` criado uma vez e
  `schema_version` — para provar "mesmo arquivo" em vez de "mesmo caminho".

`scripts/mixed_generation_service.py`

- `_OperationTracker` correlaciona `(tablet_id, task_id, revision)` e prova a
  associação com `claim_execution`; o executor é
  `host:<pid>-<starttime>` (`_local_execution_identity`). Adotar "qualquer
  operação ativa do tablet" deixou de ser possível.
- `claim()` continua **falhando fechado** antes de qualquer efeito: store
  inutilizável, operação de outra tarefa/revisão/execução ⇒ `blocked`, e a
  publicação aborta sem tocar no dispositivo.
- `mark_effect_started()` + `_OperationTracker.finish()` devolvem
  `recorded` / `unrecorded` / `unrecorded_after_effect`. Se o resultado não pôde
  ser gravado **depois** de um efeito interruptivo possível, o publicador
  remapeia para `needs_reconciliation` com mensagem de "confira o estado da TV";
  nunca `failed_safe` sem prova.

### Banco compartilhado

- `.env.example`: bloco do `MIXED_OPERATIONS_DB` com o valor do container
  (`/app/vod_cache/.operations/operations.db`), o valor do host, a razão de
  serem o MESMO arquivo com prefixos diferentes, e o comando de criação.
- `scripts/setup_operations_db.sh`: dry-run por padrão; cria diretório `0700` e
  arquivo `0600`, roda a migração do próprio módulo e agora **prova** o
  compartilhamento por `device:inode` + `db_instance_id` + marcador, falhando
  com "BANCOS INDEPENDENTES" quando os dois lados divergem.

## Testes medidos

| Comando | Resultado |
| --- | --- |
| `pytest tests/test_mixed_operations.py -q` | **47 passed** (era 40; +7 de correlação e migração) |
| `pytest tests/test_mixed_ops_api.py -q` | 19 passed + 5 subtests |
| `pytest tests/test_mixed_generation_service.py -q` | **133 passed, 59 subtests** (era 123 e 59) |
| `pytest` (operations+api+service+server apply+bridge+dashboard ops+contract) | **265 passed, 78 subtests** |
| `bash tests/test_activate_favorites_tunnel.sh` | **rc=0**, 39 testes, banner final (a suíte usa `set -euo pipefail`: qualquer assert falho aborta) |
| `bash tests/test_setup_operations_db.sh` | **rc=0**, 6 testes em sandbox (sem ssh/docker de produção) |

Evolução das contagens nesta rodada: store 40 → 47; service 123 → 133; server apply 26 → 31; shell 33 → 38.

Cenários exigidos e onde estão provados:

| Cenário | Teste |
| --- | --- |
| Recuperação em pasta alternativa | shell Test 34; `SupervisorReconciliationTests.test_recovery_in_an_alternative_folder_reconciles_the_location` |
| Manifesto igual, dono diferente | shell Test 35; `..._a_different_manifest_is_not_adopted_just_by_location` |
| PID reutilizado | `SupervisorIdentityTests.test_a_reused_pid_is_not_the_same_supervisor`; shell Test 36 |
| Reboot | `..._test_a_reboot_changes_the_incarnation` |
| Identidade alterada entre consulta e ação | shell Test 36/37; `..._identity_changed_between_query_and_action_blocks_the_takeover` |
| Lease perdido | shell Test 20/21; `test_guard_lost_right_after_takeover_restores_the_previous_generation` |
| Comando atrasado | shell Test 36 (`lock_incarnation_changed`) e Test 37 (`lock_incarnation_unproven`) |
| Correlação por intenção/revisão/execução | `ExecutionCorrelationTests` (5) + `OperationTrackerCorrelationTests` (4) |
| Migração de banco existente | `ExplicitMigrationTests` (2) |

## Revisão independente (somente leitura)

Um revisor separado leu apenas os trechos alterados e apontou 6 itens. Todos
foram conferidos contra código/medição antes de aceitar:

| Achado | Veredito | Como ficou |
| --- | --- | --- |
| `handoff-takeover` ainda autorizava por caminho quando a incarnação não chegava | **procedente (crítico)** | incarnação obrigatória; comparação antes de qualquer pasta; Test 37 |
| `handoff_lease` descartava a incarnação quando não havia sessão anterior | procedente | passa `''` + incarnação, sem mentir sobre o vínculo |
| CAS da localização só revalidava `active_session` | procedente | revalida sessão, geração e manifest dentro do lock |
| `reconcile_location` confia na observação do chamador | aceito como projeto | a revalidação independente acontece em `revalidate_supervisor`, imediatamente antes da mutação |
| `intent_revision` NULL aceitaria qualquer revisão | procedente (comentário) | comportamento mantido de propósito; comentário corrigido |
| `SCHEMA_VERSION` continuava 1 após novas colunas | procedente | versão 2 + atualização do valor gravado |

**Verificação da revisão:** o item crítico foi reproduzido com teste próprio
(Test 37 falha sem a correção). O parecer sobre ordem de checagens foi conferido
por execução, não por leitura: Test 24 passou a falhar exatamente em
`no_previous_state` antes da correção, confirmando a ordem prevista.

**Limite:** a revisão externa não executou nada; ela leu recortes. O parecer
sobre "processo não é sinalizado" vale para o código lido, não para o tablet
físico.

## Pendências reais

Fechadas nesta rodada (com teste): `/api/mixed/refresh` passou a criar operação
durável; a revisão reservada passou a viajar do endpoint para o publicador; o
caminho positivo do `handoff-takeover` está provado; o setup do banco foi
testado em sandbox.

Continuam abertas:

1. **Nada foi implantado.** O container roda um `server.py` anterior e
   `MIXED_OPERATIONS_DB` não está definido em produção: sem ele o store não liga
   e o comportamento é o antigo.
2. `scripts/setup_operations_db.sh` foi exercitado **só em sandbox**. A criação
   real e a prova host==container com o container de verdade seguem pendentes de
   janela autorizada.
3. Os endpoints respondem **HTTP 200** com `operation_id`, não 202. O contrato
   202/409 com `active_operation_id` existe no adapter (`mixed_ops_api`) e no
   store; o `refresh` já devolve 409 nos dois casos de conflito, o `apply` não.
4. `desired_generation` enviado pelo cliente é registrado, mas não exigido: a
   revisão correlacionada é a do journal (`pending_generation`) no instante do
   clique. Mudança de alvo entre o clique e o claim ⇒ publicação bloqueada, que
   é o comportamento desejado, mas ainda sem teste de corrida dedicado.
5. Fases 3 a 5 (runtime/transporte, máquina de estados por flag, janela física)
   continuam não implementadas.

## Roteiro reversível para a janela física

Nada aqui foi executado. Ordem proposta, cada passo com verificação e volta:

1. **Antes de tocar em qualquer coisa** (somente leitura): `git status` limpo do
   que é meu; anotar `active_generation`, `active_session` e o dono do
   `mode_switch.lock`; `describe` no tablet para gravar a incarnação de partida.
2. **Banco**: `sh scripts/setup_operations_db.sh` (dry-run) e depois
   `CONFIRM_SETUP=1`. Volta: parar de definir `MIXED_OPERATIONS_DB` nos dois
   lados — o store não liga e o comportamento antigo retorna; o arquivo pode
   ficar no disco sem efeito.
3. **Container** (única etapa que interrompe a TV): recriar com a nova variável.
   Verificação: `describe`/`status` continuam respondendo; painel mostra a
   operação. Volta: recriar com o `server.py` anterior e a variável ausente.
4. **Publicação de teste**: uma operação `apply` com idempotency_key, painel
   aberto, e conferir `operation_id` estável ao repetir o clique. Volta: o
   journal antigo (`active_session`/`active_generation`) está preservado e
   nenhuma geração é apagada pela reconciliação.
5. **Recuperação em pasta alternativa** (só se reaparecer): observar, reconciliar
   e conferir que o painel mostra `location_reconciled` com `from`/`to`.
6. **Se algo divergir**: não sinalizar processo e não reconectar USB; registrar
   `needs_reconciliation` e reconciliar com prova (LUN, lock, artefatos).

Reversão de código **não** reverte USB: a geração ativa continua a que estava
lá; reconciliar antes de qualquer rollback de serviço.

---

## Rodada final: pendências fechadas (medido)

| Pendência | Fechada com | Prova |
| --- | --- | --- |
| `/api/mixed/refresh` sem operação | `handle_mixed_refresh` cria/adota pelo mesmo store, devolve `operation_id` e responde 409 em `IdempotencyConflict` (nomeando a original) e em `TabletBusy` (apontando a operação em curso), sem lançar um segundo publicador | 4 testes novos em `test_server_mixed_apply.py`; a suíte do arquivo foi de 26 para 31 |
| Revisão reservada não correlacionada | endpoints gravam `intent_revision`/`desired_generation` lidos do journal (`pending_generation`); o publicador usa o mesmo campo; `claim_execution` compara | `test_the_reserved_revision_travels_with_the_operation` + `ExecutionCorrelationTests` |
| Sem teste positivo de takeover | supervisor falso que escreve `handoff.ack` e libera o lock | shell Test 38: `takeover released=1 acked=1` |
| Setup do banco nunca exercitado | modo sandbox: `ssh` substituído por execução local, `docker` ausente | `tests/test_setup_operations_db.sh`, 6 testes, rc=0 |

O sandbox do setup prova também duas propriedades de segurança: a **prova não é
inventada** quando o container não existe (o script diz "prova adiada"), e uma
**cópia** do banco carrega o mesmo `db_instance_id` mas outro inode — por isso a
comparação usa `device:inode` **e** `db_instance_id`, nunca o caminho.

---

## Rodada de auditoria de código: as quatro lacunas, fechadas

O auditor de código reprovou a entrega anterior com quatro lacunas concretas. Cada
uma foi fechada com código **e** teste; nenhum requisito foi enfraquecido para o
teste passar.

### 1. `apply` engolia erro do store e lançava mesmo assim

`server.py:handle_mixed_apply` agora, com store configurado, **falha fechado**:

| Situação | Resposta | Lança? |
| --- | --- | --- |
| outra operação segura o slot do tablet (`TabletBusy`) | 409 + `active_operation_id` | **não** |
| mesma chave com outra intenção (`IdempotencyConflict`) | 409 + `operation_id` da original | **não** |
| persistência falhou | 503 "nada foi iniciado" | **não** |
| duplicata (mesma chave + mesma intenção) | 200 + `operation_id` + `duplicate: true` | **não** (relança não) |

Sem `MIXED_OPERATIONS_DB` o comportamento legado é idêntico. Testes:
`test_apply_is_refused_with_409_when_the_tablet_slot_is_taken`,
`test_apply_is_refused_with_409_when_the_key_means_something_else`,
`test_apply_fails_closed_when_the_operation_cannot_be_recorded`,
`test_a_duplicate_apply_does_not_relaunch_the_publisher` (todos com
`popen.assert_not_called()` / contagem de lançamentos).

### 2. Adoção por `operation_id`, e refresh sem tarefa

- O endpoint passa `--operation-id <id>` ao publicador **nos dois transportes**:
  direto (argparse novo) e bridge (`build_request`/`validate_request`/
  `publisher_argv`/CLI do cliente).
- `_OperationTracker.claim()` adota **por id**: lê a linha, compara tablet,
  intenção e revisão, e só então faz o CAS de execução. A varredura por tablet
  ficou como caminho secundário.
- A intenção passou a ser comparada de forma **normalizada**: refresh não tem
  `task_id`, e `None == None` é igualdade, não motivo de recusa. Antes, o
  `if not self.task_id` recusava a própria operação de um refresh.
- Operação terminal adotada por id → `already_terminal`: o publicador **reporta**
  o resultado e não republica.

Fluxo inteiro (não stubs separados):

| Teste | Prova |
| --- | --- |
| `test_refresh_http_to_worker_chain_adopts_the_same_operation` | HTTP refresh → `operation_id` na resposta **e** no argv lançado → worker adota o mesmo id → `finish` fecha a MESMA linha |
| `test_a_worker_with_another_revision_is_refused_on_the_same_id` | corrida "alvo mudou entre o clique e o claim" |
| `test_a_terminal_operation_is_reported_not_republished` | duplicata terminal não republica |
| `test_the_operation_id_travels_from_the_request_to_the_publisher_argv` | bridge: request → validação → argv do publicador |

### 3. Identidade estável e comparação completa sob a guarda

- `boot_identity`: `boot_id` do kernel; se ausente, **`btime`** de `/proc/stat`.
  **Nunca uptime** — ele cresce a cada chamada e faria duas leituras da MESMA
  inicialização parecerem boots diferentes. Medido: duas chamadas seguidas
  devolvem o mesmo valor.
- `instance_of`: `activation/instance.id`, **escrito no start do supervisor**
  (linha 1109 do script) e apenas lido depois — `describe` continua somente
  leitura.
- `identity_of <pkgdir>` = `boot|instance|incarnation|manifest_id|package_digest`.
- `identity_is_complete`: cinco componentes reais; `none` ou fragmento reprova.
- `owner_pkg_dir <pid>`: **descobre** o pacote pelo cmdline do dono do lock. O
  caminho serve para achar o pacote; a autorização é a igualdade da tupla
  recalculada a partir dele.
- `lease-write` compara sob a guarda (`lease_cs_exec`/flock) e
  `handoff-takeover` compara **antes** de qualquer sinal ou escrita de marca.
  Token incompleto ou dono cuja pasta não se pode provar →
  `lock_incarnation_unproven` (fail-closed); tupla diferente →
  `lock_incarnation_changed`.
- Dois validadores de forma que zeravam o token novo (resquício do padrão
  `<pid>-<starttime>`) foram **removidos**: eles transformavam uma identidade
  completa em "nada observado".

Testes: shell 34 (pasta alternativa), 35 (dono divergente), 36 (comando
atrasado), 37 (nada observado), 38 (positivo, com ack) e **39 (token de boot
anterior)**.

### 4. Corridas exigidas

| Corrida | Teste |
| --- | --- |
| alvo mudou entre clique e claim | `test_a_worker_with_another_revision_is_refused_on_the_same_id` |
| requisição velha de boot anterior | shell Test 39 |
| comando atrasado de publisher antigo | shell Test 36 |
| takeover sem instância observada | shell Test 37 |

### Revisão externa desta rodada

`aider-argon` (somente leitura, diretório isolado, removido depois) revisou o
recorte de identidade. Três perguntas voltaram "sem achado"; a estabilidade do
`boot_identity` ele não conseguiu avaliar (não recebeu o corpo da função) **e eu
medi**: estável. O achado 5 — falso negativo para supervisor sem todos os
componentes — foi conferido: nosso supervisor escreve `instance.id` no start,
`SHA256SUMS` é obrigatório para ativar (Test 5) e um supervisor legado é recusado
por projeto ("recuse supervisores antigos sem suporte comprovado"), recuperado
pelo roteiro supervisionado em janela livre.

### O que NÃO mudou

As recusas de dono estrangeiro continuam recusas: o motivo é que ficou **mais
forte** (`unproven` quando o dono não pode ser ligado a pacote algum, em vez de
`changed`). O Test 38 agora também assere que um processo espectador **não** foi
sinalizado.
