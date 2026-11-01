# HANDOFF — atualização manual da lista do pendrive pelo controle web

Data: 29/10/2026. Branch `feat/tv-live-grade-completa`, HEAD `5b6c4cd`.
Árvore suja preservada (sem reset/clean/stash/checkout). `istanbullu-sync` intacta.
Nenhum segredo (PIN, token, credencial, URL privada) aparece neste documento.

---

## 1. Estado real agora (medido, não lido do diário)

| Item | Valor observado |
|---|---|
| TV | ligada, `tv_usb_state=CONFIGURED`, **reproduzindo agora** |
| Geração ativa | **5** — `mixed-gen5-282898b2`, `verify-active → verified=1 lock_owned=1` |
| LUN | `/data/local/tmp/live-favorites/mixed-gen5-282898b2/mnt/tv_stream.img` |
| Processos do tablet | `favorites_fuse` (geração 5) + `favorites_tun` (túnel vivo) |
| `switch/current` | `mixed-gen5-282898b2` |
| Serviços da geração | manager Live 18190 + proxy VOD 18191 no ar |
| VOD real | `preflight ok` — range autêntico de bytes pelo túnel |
| Lista ativa | **117 VODs** + 2 Live (SBT `207198`, Record `214640`) |
| Diário | `/opt/containers/apps/usb-stream-tv/vod_cache/.mixed_state.json` → `phase=active, active=5` |
| Ponte | `usb-stream-tv-mixed-bridge.service` **active** (enabled=disabled: não sobe no boot) |
| Timer | `usb-stream-tv-mixed-prepare.timer` active+enabled, executa `--no-publish` |

**Código local e produção são idênticos** (hashes conferidos para os 6 arquivos):
`mixed_generation_service.py b3cb97aefa29`, `mixed_bridge.py 89fbb28c0875`,
`activate_live_favorites_tablet.sh c8e9319cdcf0`, `server.py 5fe95d5b080a`,
`dashboard.html 381feb410116`.

---

## 2. Os três fluxos, com evidência física

### B — download concluído → preparação automática → clique (CONFIRMADO)
A sequência mais valiosa da rodada, com horários do host:

- **21:13:18** — a geração 5 foi **preparada em staging pelo timer**, sem tocar no
  USB (a geração 4 continuou no ar, reproduzindo).
- **21:18:37** — o clique publicou a geração 5 (`phase=active`).
- Resultado: a lista passou de 116 para **117 VODs** (entrou um episódio novo).

Isso prova o requisito "preparar em segundo plano sem interromper" e "aplicar no
clique". O usuário confirmou que a TV voltou a funcionar depois de re-selecionar o
pendrive.

### A — exclusão (confirmado tecnicamente, pendente de teste com item novo)
A geração 4 (116 VODs, sem EP164/165/166) foi publicada e **confirmada tecnicamente
no tablet**; o usuário confirmou na TV que os episódios excluídos não aparecem.
O caminho usado foi o novo `refresh_inventory` (inventário inteiro, sem depender de
uma tarefa âncora).

### C — lista já atual (CONFIRMADO que não reconecta)
`POST /api/mixed/refresh` com o inventário igual ao ativo: o supervisor da TV
**não** foi trocado (`lock_pid` e `starttime` idênticos antes/depois) e o estado
final foi `already_current`, sem reconexão.

---

## 3. O que foi implementado nesta rodada

### 3.1 `interrupts_playback` honesto
Antes o refresh devolvia `false` mesmo tendo trocado o pendrive. Agora existe uma
única fonte de verdade (`reconnected_pendrive(state)` → `True` só para `active`),
exposta em `interrupts_playback` + `reconnected` no retorno do publicador, no
diário e em `/api/mixed/status`. A UI usa isso para escolher a mensagem:
"pendrive foi reconectado" vs "a lista já estava atualizada — nada foi alterado".

### 3.2 Achados do Astra (revisão independente) — todos corrigidos
1. **Inventário indisponível virava lista vazia** (o mais grave: publicaria uma
   geração sem nenhum vídeo). Agora catálogo ilegível/inválido → `unknown` +
   `inventory_unavailable`, sem publicar. Catálogo genuinamente vazio continua
   válido.
2. **Caminho automático podia publicar.** Agora `manual=False` retorna `prepared`
   sem chamar a publicação: só o clique reconecta a TV.
3. **Inventário mudando entre preparar e publicar.** O fingerprint é reconferido
   sob o lock e o pacote é reconstruído uma vez; se mudar de novo → `prepared`
   com "toque de novo", em vez de publicar lista obsoleta.
4. **Estado ambíguo destruía serviços.** Agora, se a ativação pode ter acontecido,
   nada é parado/restaurado: só a guarda é liberada e o estado fica `unconfirmed`
   para reconciliação. Separação explícita entre `ActivationNotStarted` (nada
   começou → desfaz com segurança) e `ActivationUncertain` (não desfaz).
5. **Gramática do handoff frouxa.** Só uma linha `takeover ...` com **um**
   `released=`; sucesso exige `acked=1` ou `already_free=1`; contradições →
   incerteza. Códigos de recusa (`lock_foreign`, `lease_missing`, …) passam a
   chegar ao diário em vez de mensagem genérica.

### 3.3 Container e ponte
- O container rodava **sem** as variáveis `MIXED_*` (o `docker restart` não relê
  `env_file`). Recriado com uma composição derivada da **config real do container**
  (`compose.mixed.runtime.yaml`), provando antes: **0 volumes perdidos**, apenas 3
  adicionados (`vod_revision.py`, `scripts/mixed_bridge.py`,
  `scripts/mixed_cache_signal.py`); imagem, rede `gateway_net` (externa), comando,
  `env_file` e `environment` idênticos. `compose.yaml` em disco era uma versão
  simplificada que teria removido mounts do Drive/slate/watchdog — **não** foi usada.
- Verificado depois: `MIXED_*` presentes, `gateway_net` preservada, 116→117 VODs
  do Drive intactos, `/api/mixed/refresh` e o campo `reconnected` respondendo.

---

## 4. Testes

| Suíte | Resultado |
|---|---|
| `pytest tests/ -q` | **607 passed, 248 subtests** |
| `test_mixed_generation_service.py` | 89 testes — inclui a classe nova `EndToEndInventoryFlowTests` com os dois fluxos completos (exclusão e episódio novo), lista já atual, último VOD excluído, catálogo indisponível vs vazio, mudança de inventário durante a preparação, clique repetido, transporte ambíguo, gramática estrita do handoff |
| `test_dashboard_delete_mixed.py` | 6 cenários (último VOD, confirmação nula, ponte indisponível, modo legado, cancelamento, falha) |
| `test_server_mixed_apply.py` | 17 (endpoint refresh, PIN, confirm_interrupt, in-flight) |
| `test_mixed_bridge.py` | 14 (protocolo, replay, uid, frescor, sem credencial no spool) |
| 4 suítes shell (túnel/lease, watchdog, switch VPS, proxy VPS) | PASS (31 seções na do tablet) |

Revisão independente: `ask-astra --deep` em 3 recortes sanitizados
(`scratch/astra-final/`), 5 achados, todos verificados contra código e testes e
corrigidos. Nenhuma aprovação inventada.

---

## 5. Incidentes desta rodada (transparência)

Durante o deploy eu causei dois danos temporários, ambos revertidos e verificados:

1. `scripts/mixed_bridge.py` e `dashboard.html` foram zerados por um `cat` em
   lugar com fonte em `/tmp` inexistente/`scp` malformado. Restaurados a partir do
   repositório; confirmado por tamanho e hash nos dois lados
   (`dashboard.html` 370209 bytes, servido pelo app).
2. Descobri que **bind-mount por inode** quebra quando o arquivo é substituído
   (`//deleted` no `mountinfo`). Solução usada: escrever **em lugar** (mesmo
   inode) e, quando a montagem já tinha órfão, recriar o container. Verificação
   final: `grep //deleted /proc/self/mountinfo` = 0. **Registrar isso como
   procedimento**: para editar arquivo montado, nunca `mv`/`tar` por cima.

Nenhum desses incidentes tocou o pendrive ou a reprodução (o pendrive vive no
tablet; o container só serve o proxy).

---

## 6. Backups e rollback

- Backup da rodada física: `/opt/usb-stream-tv/.backup-fisico-20261029T184742Z/`
  (scripts, diário, `server.py`, `dashboard.html`, `state.json` da geração 3).
- Composições preservadas: `compose.gdrive.runtime.yaml` (original) e
  `compose.mixed.runtime.yaml` (usada na recriação).

Rollback por camada (nenhum executado além do descrito):

| Camada | Comando |
|---|---|
| Serviços da geração | `sh /opt/usb-stream-tv/scripts/switch_mixed_generation_vps.sh stop mixed-gen5-282898b2` |
| Tablet (LUN) | `busybox sh <pkg>/activate_live_favorites_tablet.sh rollback <pkg>` |
| Container | `docker compose -f compose.gdrive.runtime.yaml up -d --force-recreate` |
| Ponte | `systemctl stop usb-stream-tv-mixed-bridge.service` |
| Preparação automática | `systemctl stop usb-stream-tv-mixed-prepare.timer` |

---

## 7. Pendências reais

1. **Re-selecionar o pendrive após cada troca** — limitação do ConnectShare, não
   do código. O usuário teve de ejetar e selecionar de novo para a TV voltar a
   funcionar. Vale considerar tornar isso explícito na mensagem do painel.
2. **Teste de exclusão com item novo** não foi refeito ponta-a-ponta nesta rodada
   (a evidência é a geração 4 medida + confirmação visual anterior do usuário).
   Precisa de um item descartável combinado com o usuário.
3. **Ponte não sobe no boot** (`enabled=disabled`) — decisão anterior; a TV
   continua servindo sem ela, mas o botão para de funcionar após reboot.
4. **`vps_publish_package` acumula diretórios de geração** em
   `/var/lib/usb-stream-tv/generations` (não removo nada por segurança) — revisar
   uma política de limpeza que nunca toque na geração ativa.
5. **Timer pode preparar gerações que ninguém publica** (é o desenho), gerando
   staging acumulado; convém uma retenção.

## 8. Como o usuário atualiza hoje

- **Depois de excluir**: excluir no painel → nada é reconectado → tocar em
  **NA TV** → o painel avisa da reconexão → lista nova publicada → na TV, ejetar e
  re-selecionar o USB.
- **Depois de um download**: nada a fazer; o timer prepara em ~2 min → tocar em
  **NA TV** no episódio → mesmo aviso e mesma sequência.
- **Se a lista já estiver atual**: tocar em NA TV informa que nada foi alterado.
