# RELATÓRIO PARA O ARQUITETO — mídia mista, primeira ativação física

Data: 28/10/2026. Árvore: `/home/sam/Code/usb-stream-tv-rust` (branch
`feat/tv-live-grade-completa`, árvore suja preservada).
Escopo desta rodada: os cinco bloqueios apontados na revisão + ponte
container→publicador.

**Nada foi implantado.** Nenhum deploy, `docker compose up`, restart de
container/watchdog/tablet, escrita em LUN/USB ou ativação na TV. Nenhum sucesso
físico é declarado. Todo o trabalho abaixo é código do repositório e teste de
host.

---

## 1. Container → publicador (implementado e testado)

O container deixou de precisar de `adb`, `ssh` ou ferramentas NTFS. Novo
`scripts/mixed_bridge.py` implementa um spool de arquivos compartilhado:

- `requests/` — gravável **apenas** pelo uid do container. Pedido JSON de até
  4 KiB com `{"version","id","action","task_id","issued_at"}`, escrito com
  `O_CREAT|O_EXCL|O_NOFOLLOW` (0600) e `rename` atômico.
- O daemon no host **reivindica** o pedido por `rename` atômico para
  `processing/` (exatamente um vencedor) e recusa, sem executar nada, se:
  não for arquivo regular (`O_NOFOLLOW`, `O_NONBLOCK`), modo ≠ 0600, uid ≠
  `--client-uid` (obrigatório), > 4 KiB, fora da janela de frescor (±120 s),
  esquema/action `apply|verify` inválidos, `task_id` fora de `[0-9a-f]{8}`, ou
  id já respondido (guarda de replay criada com `O_EXCL` **antes** do efeito
  externo).
- Execução **apenas por argv**: `publisher --apply --task-id <id> [--verify]
  --json`. Sem shell, sem caminho vindo do pedido, sem PIN/token no pedido ou na
  resposta. A resposta é o JSON token-free do publicador.
- `server.py`: `MIXED_BRIDGE_SPOOL` seleciona a ponte; `MIXED_APPLY_CMD`
  continua valendo como transporte direto. Os dois nunca se misturam.
- `Dockerfile`: copia **somente** `scripts/mixed_bridge.py` (nenhum adb/ssh).
- Resíduos são limpos apenas após 2× a janela de frescor (`done/`) ou 1×
  (`responses/`, `processing/`); nada novo é apagado.

## 2. Ordem dos serviços e preflight (corrigida)

Antes: transferência → túneis → preflight → (só então) serviços da geração.
Isso não podia funcionar em cold start, porque `tunnel-start` prova o túnel
contra uma porta que ainda não tinha ninguém ouvindo.

Agora, em `_publish_locked`:

1. transferir o pacote ao tablet (não interrompe ninguém);
2. checar ociosidade sob a guarda exclusiva;
3. **subir/supersede os serviços desta geração** e provar bytes do VOD
   (`/probe` com bearer 0600) — `switch_mixed_generation_vps.sh start`;
4. túneis dedicados do tablet;
5. preflight end-to-end pelo túnel (handshake Live + **range VOD autenticado**
   com bytes reais, com o pacote e a sessão que a TV vai usar);
6. rechecar ociosidade;
7. lease/handoff/takeover no tablet e ativação supervisionada;
8. confirmação no tablet → promoção.

`--supersede PREV` no switch resolve o N→N+1 nas portas compartilhadas: só os
pids **provados** por `/proc/<pid>/cmdline` da sessão anterior são parados, a
sessão anterior fica registrada e:
- `switch stop <nova>` a restaura (rollback do conjunto);
- uma falha do próprio `start` a restaura automaticamente e imprime
  `switch failed previous_restored=yes|no|none`.

O publicador lê essa linha: se `no`, grava `previous_services_down=true` e falha
com `vps_previous_services_down` — nunca afirma que a geração anterior ficou
intacta quando os serviços dela caíram.

Abortar depois da troca de serviços (passo 6) agora executa `vps_switch_stop` e
devolve o conjunto anterior; antes esse caminho deixava a VPS na geração nova e
o tablet na antiga.

Prova de Live antes do LUN: usa `PROBE` (que a manager responde
`ERR source unavailable`) e o range VOD real. **Não** se faz `OPEN` real antes
da FUSE da TV porque a manager exige `generation/epoch` estritamente crescentes:
um `OPEN` de sonda avançaria os contadores e faria o cliente real da TV parecer
obsoleto. O `--channel-id` do switch fica reservado a diagnóstico isolado.

## 3. Estado da TV e clique manual (explicitado e testado)

- `active_vod`, `listeners` e `bitrate` vêm dos modos **legados** e não provam
  que a TV parou de ler o pendrive misto. Eles são usados **apenas como
  bloqueio**; nunca como prova de ociosidade.
- O caminho automático nunca troca o LUN: `_publish_locked` exige
  `manual=True`; `step(publish=True)` e `publish_pending()` devolvem
  `manual_required` sem tocar no tablet (testado).
- O diário grava `mixed_reader_proof="not_available"` e `idle_reason` em toda
  publicação, para que ninguém confunda a decisão com uma medição.
- A recuperação nunca troca para uma geração **nova**; no máximo restaura a
  geração anterior cujo supervisor já havia sido parado pelo takeover, dentro do
  mesmo clique manual.
- A UI avisa sempre que a atualização vai reconectar o pendrive, e
  `blocked` distingue reprodução real (`active_*`) de telemetria desconhecida.

## 4. Lease no tablet (atômico)

- Aquisição serializada por `mkdir` (atômico em qualquer firmware): dois
  publicadores concorrentes não podem ambos se declarar donos.
- Dentro da seção crítica o lease é **re-lido**: lease vivo de outro token é
  recusado, nunca sobrescrito; reaquisição com o mesmo token é idempotente.
- Reclamação de lock órfão é serializada por um `mkdir` interno (`reclaim`) e
  re-verifica o pid registrado antes de remover: um contendor não pode apagar o
  lock recém-criado por outro.
- `handoff-renew` (novo): só o dono do token renova; lease expirado ou de outro
  dono é recusado. O publicador renova o lease em **lockstep** com a guarda do
  servidor (`GuardKeeper.extra`), então o lease não expira entre a aquisição e a
  ativação; renovação recusada aborta antes do LUN.
- Expiração continua sendo a recuperação de um publicador morto.

## 5. Arquivos de geração na VPS (sem `rm -rf` destrutivo)

`vps_publish_package` agora:

1. empacota e envia por `scp` para um tarball privado de nome único;
2. extrai em um diretório **novo** `$ROOT/.<session>.<pid>-<ms>.new` criado com
   `mkdir -m 700` (nunca reaproveitado e **nunca removido** — se colidir, falha);
3. roda `sha256sum -c SHA256SUMS` nesse diretório;
4. só então `mv -T` atômico para `$ROOT/<session>`;
5. se `$ROOT/<session>` já existe, **recusa** (`generation directory already
   exists on the VPS`) em vez de apagar — a geração ativa nunca é tocada.

Os únicos artefatos removidos são o tarball que esta execução subiu. Um diretório
`.new` órfão de um crash permanece (resíduo inofensivo, sem remoção de dados não
provados).

## 6. Evidência (host, sem hardware)

| Verificação | Resultado |
|---|---|
| `python3 -m pytest tests/ -q` | **560 passed, 230 subtests** |
| `tests/test_mixed_bridge.py` (novo) | 14 testes + 15 subtestes (round-trip real cliente↔daemon, recusa por modo/uid/frescor/replay/tamanho/symlink/nome fora do shape/action/task_id, sem credencial no spool) |
| `tests/test_mixed_generation_service.py` | 58 testes + 33 subtestes (ordem serviço→túnel→preflight→LUN, cold start sem `--supersede`, N→N+1 com `--supersede`, rollback do conjunto em cada falha, `previous_restored=no`, lease renovado, falha de renovação, concorrência de clique, caminho automático sem LUN) |
| `tests/test_server_mixed_apply.py` | 13 testes (ponte sem publicador no container, `--action verify`, sem `--background` no modo ponte, PIN obrigatório) |
| `tests/test_activate_favorites_tunnel.sh` | 23 seções PASS (lease concorrente 10×→1 vencedor; lease vivo nunca sobrescrito; `handoff-renew` só do dono; expiração; lock órfão reclamado; **8 contendores sobre lock órfão → 1 vencedor**) |
| `tests/test_switch_mixed_generation_vps.sh` | 16 seções PASS (`--supersede` para só pids provados, rollback automático em falha, `previous_restored=no` honesto, recusa de supersede de sessão que não é `current`) |
| `tests/test_watchdog_favorites_handoff.sh`, `tests/test_start_mixed_vod_proxy_vps.sh` | PASS |
| `cargo test -p fuse_ntfs --offline -- --test-threads=1` | 324 lib + integrações, 0 falhas |
| `validate_mixed_package.sh` em `scratch/mixed-staging/mixed-gen2-32abe7d7` | 122 arquivos (2 Live + 120 VOD), `manifest_id 23307d13…ee50b`, allowlist 120 aceita, imagem NTFS 137438953472 B virtuais, montagem recusada só por privilégio FUSE (esperado) |
| `node --check` no JS do dashboard | OK |

Consulta `ask-astra` (recortes sanitizados, sem PIN/token/URL privada) apontou
quatro correções concretas, todas aplicadas e testadas: leitura com
`O_NONBLOCK` (FIFO não bloqueia o daemon), modo exato 0600, `--client-uid`
obrigatório, guarda de replay reservada **antes** do efeito externo, retenção 2×
para `done/`, e reclamação de lock órfão serializada.

Verificação independente por subagente `verifier` (somente leitura): todas as
suítes acima PASS; `git status` idêntico antes/depois. Ele também fez prova
**RED** em cópias descartáveis: inverter a ordem serviço→túnel faz o teste de
ordem falhar; remover a recusa de lease alheio faz o teste concorrente falhar —
as invariantes têm dentes. Apontou um refinamento (nome de arquivo de pedido
fora do formato 32-hex podia gerar uma resposta), já corrigido: nomes que não
são id mintado são ignorados e `requests/` órfão é limpo após 2× a janela.

---

## 7. Estado exato do deploy

**Não há deploy.** O que existe hoje:

- Código no repositório (árvore suja preservada; sem `reset/clean/stash`).
- Pacote de staging validado em `scratch/mixed-staging/mixed-gen2-32abe7d7`,
  ainda **`incomplete`** porque `transport.token` e `chisel.auth` não estão
  nesta máquina (devem vir de `--private-dir`).
- Container de produção **não** reconstruído: continua sem a ponte.

### 7.1 Pré-requisitos para o primeiro teste físico

1. **Credenciais privadas** no host que fala com o tablet:
   `--private-dir <dir>` com `transport.token` e `chisel.auth` (0600). Sem isso
   o pacote permanece `incomplete` e nada é publicado.
2. **Scripts do publicador no host** (app dir), incluindo `scripts/`,
   `config/live_favorites_export.json`, `catalog_cache/live_streams.json` e o
   `.env` com `TV_PIN`.
3. **Ponte**:
   - `mkdir -p /var/lib/usb-stream-tv/bridge` (0700), montado no container no
     mesmo caminho;
   - daemon no host:
     ```sh
     python3 /opt/usb-stream-tv/scripts/mixed_bridge.py serve \
       --spool /var/lib/usb-stream-tv/bridge \
       --client-uid "$(docker exec usb-stream-tv id -u)" \
       --publisher python3 /opt/usb-stream-tv/scripts/mixed_generation_service.py \
       --state-file /var/lib/usb-stream-tv/bridge/state.json \
       --staging-base /var/lib/usb-stream-tv/staging \
       --private-dir /opt/usb-stream-tv/private \
       --adb-serial 127.0.0.1:25555 \
       --vps-switch-script /opt/usb-stream-tv/scripts/switch_mixed_generation_vps.sh \
       --vps-app-dir /opt/usb-stream-tv
     ```
   - container (`.env`): `MIXED_BRIDGE_SPOOL=/var/lib/usb-stream-tv/bridge`,
     `MIXED_STATE_FILE=/var/lib/usb-stream-tv/bridge/state.json`,
     `MIXED_APP_DIR=/app`;
   - `compose.yaml`: montar o spool (diff exato, **não aplicado**):
     ```yaml
     volumes:
       - /var/lib/usb-stream-tv/bridge:/var/lib/usb-stream-tv/bridge
     ```
4. **Rebuild do container** (`docker compose build && docker compose up -d`) —
   só na janela, e só se o usuário autorizar.
5. Publicador prepara a geração (`--check-only` / daemon) **sem** publicar.

### 7.2 Rollback (preparado, não executado)

| Camada | Comando |
|---|---|
| Serviços da geração | `sh scripts/switch_mixed_generation_vps.sh stop <SESSION>` (mata só pids provados e restaura a sessão anterior) |
| Tablet (LUN/modo) | `busybox sh <pkg>/activate_live_favorites_tablet.sh rollback <pkg>` (restaura o VOD anterior ou o Single Live instalado) |
| Botão volta ao legado | `MIXED_BRIDGE_SPOOL=` vazio (+ restart do container), ou `MIXED_APPLY_CMD=` vazio |
| Código | `git stash` **não** é usado; a árvore suja é preservada. Reverter = reaplicar os arquivos a partir de backup identificado |

### 7.3 Checklist do teste físico (com o usuário, TV livre)

1. Usuário confirma que **ninguém está assistindo**.
2. `/api/status`: `tv_usb_state`/`tv_on` coerentes; se houver dúvida, não ativar.
3. Um clique no botão (a UI avisa que o pendrive será reconectado).
4. Acompanhar `/api/mixed/status` até `active` e `/api/mixed/confirm` com
   `confirmed=true`.
5. Na TV: pastas na raiz; `SBT → Record → SBT`; um episódio com seek; um filme;
   retorno ao Live.
6. Telemetria: `verify-active` = `verified=1`; manager registra
   `source_started`; proxy serve o episódio sem 404.
7. Qualquer desvio: rollback da tabela 7.2 e relato — **sem** declarar sucesso.

---

## 8. Pendências

### Não impedem o primeiro teste
- Atualização da lista com episódios novos (N→N+1): implementada e testada com
  device falso; o exercício físico fica para a segunda etapa, como combinado.
- Resíduo `.new` na VPS após um crash: inofensivo, não apaga dados.
- Rebuild do container e montagem do spool no `compose.yaml`: diff pronto, não
  aplicado.

### Impedem o primeiro teste (dependem de você)
1. `transport.token` + `chisel.auth` via `--private-dir`.
2. Daemon da ponte rodando com o `--client-uid` correto e o spool montado.
3. Scripts do publicador no app dir do host + `.env` com `TV_PIN`.
4. Rebuild do container para incluir `scripts/mixed_bridge.py`.
5. Confirmação de que a TV está livre.

### Limites assumidos e documentados
- **Reprodução mista não é mensurável** pelo servidor: o controle remoto da TV
  pode ler o pendrive sem passar por software. A autorização é o clique manual
  com aviso explícito; nenhuma guarda de software elimina esse risco.
- Live antes do LUN é provado por handshake `PROBE` + range VOD real; um `OPEN`
  real de sonda é proibido de propósito (contadores generation/epoch).
- Nada foi medido contra Drive real, VPS real, tablet real ou TV.
---

# Atualização 29/10/2026 — exclusões e novos downloads (TV em uso, nada publicado)

Estado: TV **em uso por pessoas**. Nenhuma publicação, troca de USB, restart de
serviço ou interrupção foi executada nesta rodada. Todo o trabalho é código,
teste de host e inspeção somente-leitura.

## Estado real encontrado (não o do diário)

| Item | Realidade |
|---|---|
| TV / USB | `tv_on=True`, `usb=CONFIGURED` no início; **desligada** ao final da rodada |
| Geração ativa | `verify-active` → `verified=1 generation=3 lock_owned=1`; LUN aponta `mixed-gen3-819e5f54/mnt/tv_stream.img` |
| Serviços da geração | **estavam mortos** (nada em 18190/18191); reiniciados nesta rodada pelo switch (não toca no USB) |
| `switch/current` | `mixed-gen3-819e5f54` |
| Sessões | gen3 e gen4 apontavam `previous_session` **uma para a outra** (ciclo criado pela falha + restauração manual) |
| Ponte | parada e desabilitada (correto) |
| Timer de preparação | ativo, executa com `--no-publish` (só staging) |

## 1. Causa raiz do "tablet gave no takeover answer" (PROVADA)

`adb shell su -c` neste tablet retorna **rc=0 mesmo quando o comando falha**
(verificado: `su -c false` → rc=0; script com `exit 1` → rc=0). O publicador
decidia pela returncode, então o `die` do script (`takeover timeout; previous
supervisor did not stop`) era **descartado** e virava a mensagem genérica — a
saída real nunca foi preservada, exatamente como o handoff relatou.

## 2. Corrida do handoff (a hipótese do handoff, agora tratada)

Antes, o `rollback()` do supervisor antigo liberava o `mode_switch.lock` e
iniciava a restauração do VOD legado enquanto o novo handoff esperava o lock
sumir — o supervisor antigo morria, o tablet voltava ao VOD legado e o handoff
se perdia.

Protocolo novo, todo sob marcador impresso (nunca por exit status):

1. `handoff-takeover` grava `takeover.request` = **token + pid + encarnação**
   (`pid-starttime` de `/proc/<pid>/stat`) **antes** do TERM.
2. `rollback()` só suprime a restauração legada se **as três** provas baterem:
   o marcador cita a *sua própria encarnação* (pid reutilizado não serve), o
   `mode_switch.lock` é dele, e o lease vivo tem o mesmo token. Qualquer outra
   situação restaura o VOD legado como sempre (TERM comum, lease inválida,
   crash).
3. Ao aceitar, o rollback grava **`handoff.ack`** (token + encarnação) antes de
   liberar o lock.
4. `handoff-takeover` só responde `released=1` **vendo o ack**; se o dono morreu
   sem ack → `released=unknown reason=no_ack`; se continua vivo →
   `released=0`; recusas antes de qualquer sinal → `released=0 reason=<código>`
   (estado conhecido, nada mudou).
5. O publicador aceita **uma única linha exata** em stdout (`takeover
   released=<1|0|unknown>`); valor parecido (`released=10`), menção em stderr ou
   resposta contraditória **não** é resposta. `unknown`, resposta ausente e
   falha de transporte viram `TakeoverUncertain` → `unconfirmed` + reconciliação
   manual, **sem** desfazer serviços/USB às cegas.
6. Lease expirada no meio: `renew` é renovado em lockstep com a guarda; uma
   resposta sem ack é `unknown`, nunca sucesso.

## 3. Portão de capacidade (o risco que sobrava)

O supervisor **ativo** na TV roda a revisão antiga (sem `handoff.ack`): sinalizá-lo
o mataria e o deixaria em VOD legado sem registro — a falha de produção.
Antes de qualquer sinal, o publicador lê o script da sessão anterior no tablet e,
se ele não conhece o protocolo, **recusa** (`PreviousSupervisorNotUpgradable`)
sem TERM, sem tocar em serviços/USB, com mensagem honesta. Isso impede repetir a
falha enquanto a sessão ativa não for atualizada de forma supervisionada.

## 4. Exclusão sem âncora (a limitação apontada no handoff, corrigida)

- Novo caminho `refresh_inventory` + ação `refresh` na ponte + endpoint
  `POST /api/mixed/refresh`: publica o **inventário atual** sem depender de
  existir um VOD pronto. Cobre excluir o último VOD.
- `confirm_inventory`: prova a **lista inteira** (não um arquivo) comparando a
  allowlist do tablet com o índice registrado, nas duas direções — um item que a
  TV ainda oferece e a geração não contém é divergência, nunca sucesso.
- `dashboard.html`: a exclusão deixou de escolher "uma tarefa remanescente" e
  usa o refresh; a mensagem de sucesso só aparece **após** confirmação real.
  Com o modo misto ativo e a ponte indisponível, **não** cai no switch legado
  (isso reconectaria o pendrive com a lista errada) e diz que a lista pode
  continuar desatualizada.

## 5. Lock do lease: de `mkdir`+`rm -rf` para `flock` do kernel

O `flock` do busybox do tablet **existe** mas só funciona na forma
`FILE -c CMD` (a forma por descritor dá "Bad file descriptor" — testado no
aparelho). O lock do lease passou a ser um `flock` do kernel: liberado
automaticamente quando o dono morre, sem caminho de recuperação de lock órfão e
sem `rm -rf` de um lock recém-criado (achado de alta severidade do Astra).

## Verificação

| Verificação | Resultado |
|---|---|
| `python3 -m pytest tests/ -q` | **594 passed, 236 subtests** |
| `tests/test_mixed_generation_service.py` | 76 testes (portão de capacidade, parser exato do marcador, incerteza de transporte, refresh sem VOD, prova da lista inteira) |
| `tests/test_dashboard_delete_mixed.py` | 6 cenários (último VOD, confirmação nula, ponte indisponível, modo legado, cancelamento, falha) |
| `tests/test_server_mixed_apply.py` | 17 (endpoint refresh sem VOD, PIN, confirm_interrupt, in-flight) |
| `bash tests/test_activate_favorites_tunnel.sh` | **30 seções** PASS (marcador por encarnação, ack, recusas inertes, flock, corridas) |
| 3 demais suítes shell | PASS |
| Parsex do marcador | 7/7 formatos reais aceitos/recusados corretamente |

Revisões `ask-astra --deep` (recortes sanitizados em `scratch/astra-handoff/`):
três achados de alta severidade aplicados e testados — leitura de PID/encarnação,
ausência de ack tratada como incerteza, exigência de resposta única e exata, e
falha de transporte como desconhecida.

## Pendências que NÃO impedem nada

- Nada foi implantado: os scripts locais (`d0a2c38e`, `89fbb28c`, `a740e756`)
  diferem dos da VPS (`eaa10c70`, `4728e438`, `79ff14d6`).
- O supervisor ativo ainda roda a revisão antiga; a atualização precisa de uma
  janela com a TV livre e de um restart supervisionado (o portão de capacidade
  garante que nenhuma troca será tentada antes disso).

## Pendências que impedem o teste físico

1. **TV livre** (está desligada agora; ligar é decisão do usuário).
2. Janela autorizada para o restart supervisionado da sessão ativa (troca o
   script do supervisor em execução; interrompe a tela).
3. Um item descartável, combinado com o usuário, para o teste de exclusão.
