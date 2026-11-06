# PROGRESSO — Gerações V2

Atualizado: 2026-11-06. **Este arquivo é o ponto de partida para retomar.**

Especificação: `docs/architecture/GERACOES_V2.md`
Prompt do builder: `docs/prompts/GERACOES_V2_BUILDER.md`

---

## Estado: FASE 1 COMPLETA E VALIDADA; fase 2 iniciada

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

Suite combinada com o que já existia: **244 testes passando**.

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
