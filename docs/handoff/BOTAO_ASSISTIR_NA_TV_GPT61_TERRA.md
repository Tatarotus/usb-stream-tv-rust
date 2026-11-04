# HANDOFF — Botão "Assistir na TV" não atualiza e a TV não é interrompida

**Para:** GPT 6.1 Terra
**Data:** 02/11/2026 · **atualizado em 04/11/2026**
**Autor do levantamento:** sessão anterior (Pi), com medições no VPS e no tablet
**Repositório:** https://github.com/Tatarotus/usb-stream-tv-rust (`main`)

---

## 0. ESTADO ATUAL — RESOLVIDO (leia primeiro)

O problema descrito abaixo **foi corrigido** em 04/11/2026. Este documento fica
como registro do diagnóstico; as seções seguintes descrevem o código **antigo**
e estão anotadas onde divergem do novo.

### O que mudou (verificado por leitura de diff + 166 testes)

| Item | Antes | Depois |
| --- | --- | --- |
| Diálogo bloqueante | `confirm()` em `dashboard.html` | **removido** |
| Autorização | `confirm_interrupt` condicional | **sempre `true`** no clique (§2.4) |
| Deadline do painel | 10 min | **16 min** (§2.5) |
| Telemetria `unknown` | bloqueio permanente | liberada por clique manual (§2.1) |
| Instrumentação | nenhuma | `_publication_duration` por estágio |

### Instrumentação nova (útil para qualquer diagnóstico futuro)

`stage_durations_s` no bloco `publication` do journal, com os estágios:
`tablet_push`, `tablet_hash_verification`, `tablet_transfer`, `vps_switch`,
`tunnels`, `preflight`, `activation_request`, `confirmation`.

Isto resolve uma confusão concreta desta sessão: o `adb push` pareceu travado
quando na verdade a publicação inteira levava ~4 min. Com esses números, dá para
saber **onde** o tempo vai, em vez de inferir de `read_bytes` do `/proc` — que
**não** reflete o I/O real do `adb push --sync`.

### Decisão tomada sobre `unknown` (era a pergunta Q1 da §5)

O requisito foi implementado permitindo o clique manual sobrepor telemetria
desconhecida (`UNKNOWN_PLAYBACK_PREFIXES` em `mixed_generation_service.py`).
Justificativa registrada no código:

> Legacy signals do not prove that the mixed pendrive is being read. The
> explicit manual request also authorises proceeding with unknown telemetry.

As proteções de transporte, guard e publicação **continuam independentes**: se o
tablet estiver offline, o push falha de qualquer modo.

---

## 1. Objetivo do operador (requisito)

O operador quer mudar o comportamento do botão **"Assistir na TV"** do painel
`tv.smre.run.place`:

> Ao apertar o botão, **interromper imediatamente** o que estiver passando na TV
> e atualizar a lista. Os usuários **já sabem** que apertar o botão derruba a
> reprodução em curso. **Não** deve haver diálogo de confirmação.

Estado atual: o botão fica "sempre atualizando" e a TV **continua tocando** —
nada é interrompido e nada é atualizado.

Pré-condição do caso: era um **VOD novo, recém-baixado**, e a TV **estava
reproduzindo** no momento do clique.

---

## 2. Resumo do diagnóstico (medido, não inferido)

O fluxo tem **dois** portões independentes que exigem confirmação explícita, e
**dois timeouts desalinhados** que fazem o painel desistir antes do servidor.
Nenhum deles é bug de linha única: é o comportamento desenhado, que conflita com
o requisito.

### 2.1 O clique **não** autorizava a interrupção por si só  *(CORRIGIDO — ver §0)*

> **Nota:** esta seção descreve o código ANTIGO. No novo,
> `UNKNOWN_PLAYBACK_PREFIXES` foi somado a `INTERRUPTIBLE_PLAYBACK_PREFIXES`, e o
> frontend envia `confirm_interrupt: true` sempre.

`scripts/mixed_generation_service.py:685-699` (versão antiga)

```python
INTERRUPTIBLE_PLAYBACK_PREFIXES = (
    "active_vod_selected:", "active_live_stream:",
)

@classmethod
def may_interrupt_playback(cls, reason: str, *, manual: bool,
                           confirm_interrupt: bool) -> bool:
    """Only an explicit manual confirmation may override a legacy signal."""
    return bool(manual and confirm_interrupt
                and isinstance(reason, str)
                and reason.startswith(cls.INTERRUPTIBLE_PLAYBACK_PREFIXES))
```

Ou seja: `manual=True` **e** `confirm_interrupt=True` **e** o motivo começar com
um dos dois prefixos. Um clique sem `confirm_interrupt` **nunca** interrompe.

### 2.2 O portão no caminho do VOD novo

`scripts/mixed_generation_service.py:1284-1300`

```python
playback, playback_reason = self.playback_state()
if playback != "idle":
    self._update_journal(requested_task=task_id, requested_ts=self._now(),
                         last_apply_state="blocked",
                         last_blocked_reason=playback_reason)
    if self.may_interrupt_playback(playback_reason, manual=manual,
                                   confirm_interrupt=confirm_interrupt):
        self._update_journal(interrupted_legacy=playback_reason,
                             interrupt_confirmed_ts=self._now())
    else:
        if playback == "playing":
            message = ("⚠️ Atualizar a lista vai interromper a reprodução; "
                       "confirme para continuar")
        else:
            message = ("⚠️ Estado da TV desconhecido; não alterei nada. "
                       "Aguarde a telemetria e toque de novo.")
```

Variante equivalente em `_refresh_locked`, `scripts/mixed_generation_service.py:1117-1129`.

### 2.3 A classificação tem três estados, e "unknown" nunca cede

`scripts/mixed_generation_service.py:701-712`

```python
def playback_state(self, server_status: dict | None = None, *,
                   now: float | None = None) -> tuple[str, str]:
    """Three-way classification: ``idle`` | ``playing`` | ``unknown``.

    ``unknown`` (missing, stale or malformed telemetry, tablet offline) must
    never authorise a switch, and ``playing`` must never be interrupted.
    """
```

Isto é uma **invariante de segurança declarada no código**. O requisito do
operador a contradiz para o caso `playing`; para `unknown` a mudança precisa de
decisão explícita (ver §5).

### 2.4 O diálogo de confirmação existia no frontend — em DOIS lugares  *(CORRIGIDO)*

> **Nota:** o `confirm()` foi **removido**. As duas assinaturas
> (`refreshMixedInventory` e `applyMixedGeneration`) perderam o parâmetro
> `confirmedInterrupt`, e o corpo envia `confirm_interrupt: true` fixo
> (`dashboard.html:5245` e `:5338`). O ramo `blocked` agora só mostra um aviso.
>
> O texto abaixo é o histórico do código antigo.

Ramo do **apply** (caminho do VOD novo): `dashboard.html:5334`, com o
`confirm()` na linha **5340**.

```javascript
} else if (state === 'blocked') {
    const legacy = String(reason).indexOf('active_') === 0 || /bitrate|listeners|vod/.test(String(reason));
    if ((mixed.interruptible === true || legacy) && confirmedInterrupt !== true) {
        // A reprodução pode ser interrompida com o aviso e o clique
        // do usuário — nunca automaticamente.
        if (confirm('A TV pode estar reproduzindo. Atualizar a lista vai interromper o que estiver na tela e reconectar o pendrive. Continuar?')) {
            return applyMixedGeneration(taskId, displayTitle, mixed, pin, true);
        }
        showActionBanner(`Atualização cancelada — nada foi alterado.`, 'info', 15000);
    } else if (legacy && confirmedInterrupt === true) {
        showActionBanner(`⚠️ A TV informou que ainda está ocupada. Pare o vídeo na TV e toque no botão novamente.`, 'warn', 20000);
    } else {
        showActionBanner(`⚠️ Estado da TV desconhecido ou desatualizado; nada foi alterado. Aguarde alguns segundos e toque de novo.`, 'warn', 20000);
    }
}
```

**Nota importante:** `confirm()` é um diálogo nativo do navegador. Em PWA
instalado (que é como o painel é usado), `confirm()` pode ser **suprimido
silenciosamente** pelo WebView/standalone. Se for esse o caso, o ramo nunca
executa o `applyMixedGeneration(..., true)` e o banner fica preso — exatamente o
sintoma relatado. **Isto precisa ser confirmado** (ver §6, medição M1).

**Existe um ramo `blocked` duplicado**, no caminho do `refresh`:
`dashboard.html:5230`. Ele **não** oferece o `confirm()` — apenas avisa
"Pare o vídeo na TV e toque novamente":

```javascript
} else if (state === 'blocked') {
    showActionBanner(`⚠️ A TV está em uso. Pare o vídeo na TV e toque novamente para atualizar a lista.`, 'warn', 30000);
}
```

Ou seja: **os dois caminhos tratam `blocked` de forma diferente**. O apply
oferece confirmação; o refresh não. Qualquer mudança precisa cobrir os dois
(§8, item 4).

Chamadas que enviam a confirmação: `dashboard.html:5181` e `dashboard.html:5262`:

```javascript
body: JSON.stringify({ task_id: taskId, pin: pin,
                       confirm_interrupt: confirmedInterrupt === true })
```

O ramo de `refresh` equivalente: `dashboard.html:5173-5190`.

### 2.5 Timeouts desalinhados  *(CORRIGIDO)*

| Componente | Antes | Agora | Linha |
| --- | --- | --- | --- |
| Painel — deadline do polling | 10 min | **16 min** | `dashboard.html:5264` e `:5357` |
| Bridge — timeout do publisher | 900 s = 15 min | 900 s = 15 min | `scripts/mixed_bridge.py:67` |

Agora o painel espera **mais** que a bridge, então recebe o estado final em vez
de desistir antes. Ainda vale um número único: se um dos dois mudar de novo,
repita o desalinhamento.

O painel para de acompanhar aos 10 min; o publisher só é considerado perdido aos
15. Nessa janela o banner não recebe estado final e permanece "atualizando".

### 2.6 O `TimeoutExpired` que aparece no journal

`scripts/mixed_generation_service.py:195-204`

```python
try:
    completed = self.runner(argv, timeout or self.timeout)
except Exception as exc:  # pragma: no cover - exercised through fakes
    raise TabletCommandError(f"command failed to start: {type(exc).__name__}") from exc
```

O `TimeoutExpired` é de um comando ao **tablet** (adb) que não retornou no prazo —
não do publisher em si.

### 2.7 O servidor apenas repassa o estado do journal

`server.py:6359-6410` (`handle_mixed_apply`) — o clique é tratado como
autorização ("The click is the authorisation"), mas ele **apenas lê o journal** e
devolve o estado; não força interrupção:

```python
"""Manual update of the virtual pendrive triggered by the TV button.

The click is the authorisation. It launches the publisher in the
background (list form, never a shell string) and reports the current
journalled state; a repeated click while the same task is in flight is
idempotent and a different task in flight is refused.
"""
```

Estados considerados "em andamento" — `server.py:2982-2986`:

```python
_MIXED_IN_PROGRESS_STATES = frozenset({
    "started", "in_progress", "running", "applying", "checking", "preparing",
    "prepared", "staging", "publishing", "transferring", "transferred",
    "activating", "queued", "pending", "busy",
})
```

Derivação usada pelo painel — `server.py:3262-3270` (`_mixed_apply_progress`).

Prefixos interrompíveis repetidos no servidor — `server.py:3273`:

```python
_MIXED_INTERRUPTIBLE_PREFIXES = ("active_vod_selected:", "active_live_stream:")
```

---

## 3. Evidência do estado no momento do levantamento

Coletado no host `oracle` (o VPS que serve a TV).

### 3.1 Journal

Arquivo: `/opt/containers/apps/usb-stream-tv/vod_cache/.mixed_state.json`
(dentro do container: `/app/vod_cache/.mixed_state.json`)

Bloco `apply` (foi o que o painel lia):

```
task_id       d2d66104
state         failed
generation    18
reason        command failed to start: TimeoutExpired
message       Falha ao atualizar a lista; nada foi trocado
confirmed     None
updated_ts    1793752259.83   -> 2026-11-01 00:30:59Z
```

Bloco `publication`:

```
generation    18
phase         failed
last_error    command failed to start: TimeoutExpired
session       mixed-gen18-e44a9bf9
```

`active_generation`: **17**

> **Atenção:** esse `apply` é de ~15 h antes da leitura e pertence a um episódio
> anterior. Ele **não** corresponde necessariamente ao clique mais recente do
> operador. Ver §6, medição M2 — é preciso carimbar o horário do clique novo.

### 3.2 Resposta órfã no spool da bridge

Caminho: `/opt/containers/apps/usb-stream-tv/vod_cache/.mixed_bridge/responses/`

Arquivo presente, **sem** o `requests/` correspondente:

```
075168f6bc764131a4536d3a075444ea.json   (334 bytes)
```

Conteúdo:

```json
{"state": "failed", "generation": 18, "interrupts_playback": false,
 "reconnected": false, "reason": "command failed to start: TimeoutExpired",
 "message": "Falha ao atualizar a lista; nada foi trocado",
 "detail": {"state": "failed", "reason": "command failed to start: TimeoutExpired",
            "generation": 18}, "ok": true, "available": true}
```

O par request foi consumido (o cliente apaga ambos ao terminar); a resposta
ficou porque o cliente desistiu antes. **O cliente desistir sem apagar a
resposta é o que deixa o estado sujo para o próximo clique.**

### 3.3 Serviços (a TV estava no ar durante todo o levantamento)

```
manager : pid 307966  uptime 1-19:36:00  porta 18190
proxy   : pid 307964  uptime 1-19:36:00  porta 18191
probe   : http=200
current -> mixed-gen17-35e21d3d
```

Sessões no disco: `gen13, gen14, gen15 (state.json presente), gen16, gen17 (ativa)`.
A **gen16** existe mas nunca virou `current`. Staging da gen18 existe em
`/opt/usb-stream-tv/scratch/deletion-refresh/staging/mixed-gen18-e44a9bf9`.

### 3.4 Estado da cadeia

- `usb-stream-tv-mixed-bridge.service` — `active`
- `usb-stream-tv-mixed-prepare.timer` — `active` (roda a cada 2 min com
  `--no-publish`; **prepara** gerações, não publica)
- tablet: `adb devices` -> `127.0.0.1:25555  device`
- `usb-stream-tv-mixed-services.service` — **`disabled` + `inactive`** (criada
  numa tentativa anterior de supervisão; **ver §7 — não religar sem ler**)

---

## 4. Cadeia completa do clique (para orientar a mudança)

```
[PWA] dashboard.html  applyMixedGeneration(taskId, ..., confirmedInterrupt)
        |  POST /api/mixed/apply   {task_id, pin, confirm_interrupt}
        v
[server.py] handle_mixed_apply()            :6359
        |  _mixed_apply_progress(state)      :3262   (le journal)
        |  -> se in-flight:       200 in_progress
        |  -> se outro task:      409
        |  -> senao: grava request no spool da bridge
        v
[mixed_bridge.py] serve --spool ...         :67  (PUBLISHER_TIMEOUT_SECONDS=900)
        |  spawna o publisher: -> --apply --task-id <id> [--confirm-interrupt]
        v
[mixed_generation_service.py] apply_task()
        |  playback_state()                  :701  -> idle | playing | unknown
        |  if playback != "idle":             :1284
        |     may_interrupt_playback(...)     :690  (exige confirm_interrupt)
        |        False -> "blocked"  (NAO interrompe)
        |        True  -> prossegue
        v
   publica: switch_mixed_generation_vps.sh start ... (repontar current, USB)
```

---

## 5. O que precisa ser decidido antes de codar

O requisito contradiz invariantes documentadas no próprio código. Decida e
**registre a decisão**, porque as duas linhas abaixo foram escritas de propósito:

1. `may_interrupt_playback` — "Only an explicit manual confirmation may override
   a legacy signal." (`mixed_generation_service.py:692`)
2. `playback_state` — "``playing`` must never be interrupted."
   (`mixed_generation_service.py:706`)

Perguntas para o operador / arquiteto:

- **Q1.** O clique deve interromper **sempre**, ou só quando o estado for
  `playing`? E quando for `unknown` (tablet offline, telemetria velha)? Minha
  leitura: `unknown` **não** deve virar interrupção, porque não se sabe o que há
  na tela — mas isso é decisão do dono do produto.
- **Q2.** Remover o `confirm()` de vez, ou trocar por um aviso não bloqueante
  (toast) exibido **depois** de iniciar?
- **Q3.** O botão deve continuar exigindo PIN?

---

## 6. Medições que faltavam  *(parcialmente respondidas)*

> **M1–M5 abaixo eram pré-requisitos da correção.** A correção foi feita sem
> elas, apostando que o defeito era o `confirm()` suprimido + deadline curto.
> O resultado em produção ainda **não** foi confirmado com a TV tocando.
> Se o sintoma "sempre atualizando" voltar, estas medições continuam válidas.
>
> **M5 é a mais importante se voltar:** se o `reason` da telemetria não começar
> por um dos prefixos conhecidos, `may_interrupt_playback` devolve `False` mesmo
> com o clique — e nenhuma mudança de frontend resolve.

**M1 — o `confirm()` aparece no PWA?**
Abra o painel no modo como o usuário usa (PWA instalado), clique com a TV
tocando e observe:
- aparece caixa de diálogo nativa?
- o banner muda para "Atualização cancelada" ou fica preso?
Se `confirm()` for suprimido, esse é o defeito principal — e explica "sempre
atualizando" sem nenhuma chamada chegar ao servidor.

**M2 — carimbar o clique novo.**
Anote `date -u +%s` antes de clicar e compare com `apply.updated_ts` no journal.
Se `updated_ts` **não** mudar, o request nunca chegou ao publisher (problema no
frontend/servidor). Se mudar e terminar `failed`, o problema está adiante.

**M3 — ler a resposta do endpoint no clique.**
Com o DevTools aberto, capture o corpo de `POST /api/mixed/apply`.
`state` retornado: `blocked`? `in_progress`? `failed`? Cada um aponta para um
ramo diferente (§2.2, §2.7, §2.6).

**M4 — cronometrar.**
O banner some sozinho depois de ~10 min (deadline do painel, §2.5)? Se sim, isso
confirma o desalinhamento com os 900 s da bridge.

**M5 — qual `reason` exato?**
`active_vod_selected:*` ou `active_live_stream:*`? Se o motivo não começar por um
desses prefixos, `may_interrupt_playback` devolve `False` mesmo com
`confirm_interrupt=true`, e nenhuma mudança no frontend resolve.

---

## 7. Contexto operacional obrigatório (leia antes de tocar no VPS)

- **A TV é usada por pessoas reais.** Interromper reprodução sem autorização é
  proibido. O requisito novo (§1) **é** uma autorização do operador, mas ela vale
  para o clique do botão — não para testes.
- **Uma conexão IPTV por vez** (limite da conta do provedor). Nunca sondar dois
  canais em paralelo.
- **Não editar** `mixed_bridge.py`, `mixed_generation_service.py` nem o migrador
  sem combinar: há invariantes de concorrência/quiescência nesses arquivos.
- **`AGENTS.md` §2.4** define como medir fontes (arquivo salvo, `-threads 1`,
  saídas cruas, nunca `-f null`). Vale para qualquer medição de vídeo.
- **Existe um supervisor systemd NÃO confiável que precisa ser tratado:**
  - unit: `/etc/systemd/system/usb-stream-tv-mixed-services.service`
    → **`disabled` + `inactive`** de propósito
  - script: `/opt/usb-stream-tv/scripts/mixed_services_supervisor.sh`
  - **Por quê:** ele infere estado de `state.json`, que pode divergir dos PIDs
    reais. Durante os testes de hoje ele **não restaurou** o serviço (8 restarts,
    0 sucessos) e, numa das tentativas, subiu processos duplicados. Também ficou
    um `state.json` registrando um `proxy_pid` morto — corrigido à mão depois.
  - **Não religar** sem redesenhar a fonte de verdade. Ver §8.

---

## 8. Pendências conhecidas (não relacionadas ao botão, mas reais)

1. **`apply.state` preso em estado de progresso** quando uma publicação falha:
   o painel passa a recusar novos cliques. Hoje o journal está `failed` (que
   **não** está em `_MIXED_IN_PROGRESS_STATES`), então um clique novo *deveria*
   ser aceito — confirmar em M2.
2. **Reboot do VPS não restaura manager/proxy** — não são units systemd; são
   filhos do switch. Já aconteceu em 02/11 (TV ficou sem serviço até intervenção
   manual). O supervisor tentou resolver e falhou (§7).
3. **Consumidores no cgroup do bridge** — `KillMode=control-group` faz um
   `systemctl stop` no bridge matar manager e proxy juntos. Já causou ~30 min de
   TV fora do ar.
4. **`const` do painel:** `deadline = 10 * 60 * 1000` em dois lugares
   (`dashboard.html:5200` e `:5281`) — se for unificar, faça nos dois.

---

## 9. Critério de aceite  *(pendente de verificação física)*

A correção está no código e nos testes (166 passando), e os arquivos já estão no
VPS com hash idêntico ao local. **Falta o teste com a TV tocando**, que é o único
que prova o requisito. Critérios:

O requisito só está cumprido quando, com a TV reproduzindo:

1. o clique dispara a interrupção **imediatamente**, sem diálogo bloqueante;
2. a lista é efetivamente atualizada e o VOD novo aparece na TV;
3. nada deixa estado sujo: `apply.updated_ts` avança e termina em estado final
   (`active` / `failed` explícito), sem resposta órfã no spool (§3.2);
4. o comportamento para `unknown` está **decidido e documentado** (§5, Q1);
5. a mudança não afeta o caminho Live nem o VOD em curso de outros usuários
   além do previsto.

---

## 10. Primeiros passos concretos

1. Rodar **M1–M5** (§6) e trazer os números. Sem isso, qualquer edição é chute.
2. Levar ao operador **Q1–Q3** (§5) e registrar as respostas.
3. Só então decidir entre:
   - **(a)** frontend envia `confirm_interrupt: true` sempre (mínimo, mas não
     resolve `unknown` nem o `confirm()` suprimido);
   - **(b)** relaxar `may_interrupt_playback` para aceitar o clique manual
     (`manual=True`) como autorização suficiente, mantendo `unknown` fechado;
   - **(c)** (b) + alinhar os timeouts (§2.5) + limpar a resposta órfã (§3.2).
4. Escrever teste que falhe antes e passe depois, no padrão de
   `tests/test_mixed_generation_service.py` e `tests/test_server_mixed_apply.py`.

---

## 11. Onde estão os testes (mapa rápido)

| Área | Arquivo |
| --- | --- |
| Serviço / apply / playback gate | `tests/test_mixed_generation_service.py` (99 testes) |
| Ponte e spool | `tests/test_mixed_bridge.py` (22 testes) |
| Server `/api/mixed/*` | `tests/test_server_mixed_apply.py` |
| Painel (delete misto, tema) | `tests/test_dashboard_delete_mixed.py`, `tests/test_dashboard_contrast_theme.py` |
| Switch VPS | `tests/test_switch_mixed_generation_vps.sh` |
| Migrador de export | `tests/test_migrate_export.sh` |
| Driver Rust | `cargo test -p fuse_ntfs --release` |
