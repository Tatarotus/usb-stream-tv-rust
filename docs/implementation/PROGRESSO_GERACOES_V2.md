# PROGRESSO — Gerações V2

Atualizado: 2026-11-06. **Este arquivo é o ponto de partida para retomar.**

Especificação: `docs/architecture/GERACOES_V2.md`
Prompt do builder: `docs/prompts/GERACOES_V2_BUILDER.md`

---

## Estado: fase 1 PARCIAL (store + adapter + endpoint apply prontos; painel pendente)

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

Suite combinada com o que já existia: **225 testes passando**.

### Não feito (é onde retomar)

| Passo | Arquivo | Nota |
| --- | --- | --- |
| ~~Ligar o adapter em `server.py`~~ ✅ feito (`fa7350f`) | `server.py` | `/api/mixed/apply` aceita `idempotency_key`, devolve `operation_id` |
| Polling global único + indicador persistente | `dashboard.html` | `applyMixedGeneration` (~5320), `refreshMixedInventory` |
| `/api/mixed/refresh` e `/api/mixed/status` por `operation_id` | `server.py` | o apply está feito; faltam os outros dois |
| Expor eventos com cursor (`/api/mixed/events`) | `server.py` | `mixed_ops_api.request_events` já pronto, falta a rota |
| Fases 2 a 5 | vários | ver especificação |

O adapter (`mixed_ops_api.py`) é chamável hoje: `request_apply` devolve 202 +
`operation_id` (ou 409 com `active_operation_id`), `request_status` responde por
id e `request_events` entrega cursor + pendências. Falta só o encanamento HTTP.

**Nada foi implantado.** O módulo não é importado por ninguém ainda.

---

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
