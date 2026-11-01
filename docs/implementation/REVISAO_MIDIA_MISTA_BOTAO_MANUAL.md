# Revisão independente — botão de atualização manual

Resultado: ainda não aprovada para ativação. A revisão não alterou produção. Os 51 testes focados do serviço e da API passaram, com 33 subtestes; não comprovam os caminhos abaixo nem teste físico.

1. `dashboard.html` em `playVodTask` trata falha/timeout de `/api/mixed/status` como modo indisponível e chama `/api/vod/play` legado. Se o misto estiver ativo mas o status falhar, isso pode acionar o switch antigo. Distinguir explicitamente indisponibilidade confirmada de estado desconhecido; falha deve informar erro e não iniciar switch.
2. `applyMixedGeneration` retorna “lista atualizada” somente com `selected_in_active` do estado, sem chamar o fluxo que verifica a geração ativa no tablet; após rollback/desconexão o diário pode estar obsoleto. Confirmar estado efetivo e revisão do conteúdo antes de anunciar sucesso ou pular a atualização.
3. A publicação manual usa `is_playback_active`, que só considera ocioso quando a TV está desligada e USB desconectado. Com TV ligada/USB CONFIGURED, parar o vídeo e tocar novamente pode continuar bloqueado; isso precisa ser testado no fluxo real. A intenção do usuário é que o clique aplique a lista e ele navegue na TV, com aviso quando a atualização interromper reprodução.
4. O handoff para N+1 rejeita lock vivo do supervisor anterior. Confirmar passagem real entre supervisores e serviços, não apenas mocks; testar N→N+1 com vídeo novo e Live, falhas e rollback.
5. O Dockerfile não instala `scripts/`, `adb`, `ssh` ou ferramentas NTFS. O botão executa `MIXED_APPLY_CMD` dentro do container, portanto o caminho exemplificado `/app/scripts/mixed_generation_service.py` não está disponível no build observado. Definir e testar a comunicação segura container→publicador VPS, estado compartilhado e permissões mínimas.
6. Na execução via SSH, `vps_switch_start` passa caminho de staging local para a VPS; a cópia temporária anterior para ADB já foi removida e não é caminho persistente para os serviços. O tratamento de `MIXED_VPS_APP_DIR`/`vps_switch_root` também precisa ser corrigido e testado em hosts com caminhos diferentes.
7. A guarda de servidor pode expirar durante `vps_switch_start`, que pode durar até 120 s, enquanto o TTL da guarda é menor e não há renovação posterior antes de ativar. Testar tempo decorrido e perda da guarda em todas as fases; nenhuma troca deve prosseguir após perda da exclusividade.
8. Polling da UI para após ~10 s mesmo se a operação continuar, e mensagens de falha afirmam que a lista anterior foi preservada sem prova do estado efetivo. Mostrar andamento até resultado confiável e preservar a incerteza quando a ativação ficar ambígua.

O relatório propõe `git checkout -- ...` como rollback de código. Isso pode descartar alterações legítimas preexistentes; não executar. A recuperação deve preservar a árvore suja e usar backups/artefatos de deploy identificados.

---

# Resposta 2026-10-28 — correções 1 a 8 e o que permanece

Nenhuma alteração em produção: nenhum deploy, container, tablet, LUN ou modo USB
foi tocado. Tudo abaixo é código do repositório e teste de host.

## 1. Estado desconhecido nunca aciona o switch legado (corrigido)

`playVodTask` consulta `/api/mixed/status` através de `probeMixedStatus`, que
devolve `mixed` | `legacy` | `unknown`. `legacy` só existe com resposta HTTP ok,
JSON válido e `available === false`; timeout, HTTP de erro ou JSON inválido são
`unknown` e mostram “nada foi alterado na TV” sem chamar `/api/vod/play`.

## 2. Sucesso só depois de provar no tablet (corrigido)

Novo endpoint somente-leitura `GET /api/mixed/confirm?task_id=…`, autenticado com
PIN e sem efeito: executa o publicador com `--apply --task-id X --verify --json`.
`MixedGenerationService.confirm_task` responde `True` (geração + `manifest_id` +
revisão + tamanho provados), `False` (tablet respondeu e não bate) ou `None`
(desconhecido: tablet inalcançável, allowlist ilegível, inventário sem revisão).
O dashboard só anuncia “Lista atualizada” após `confirmed === true`; `false`
reenvia a publicação e `None` preserva a incerteza.

## 3. TV ligada com pendrive montado é navegação, não reprodução (corrigido)

`is_playback_active` continua fail-closed para tudo que é mensurável
(`active_vod`, `listeners > 0`, bitrate > 0, telemetria ausente/velha/ inválida,
tablet offline). `USB=CONFIGURED` e TV ligada com USB desconectado passam a
`idle_tv_on_no_playback`; estados USB transitórios/desconhecidos continuam
bloqueando (`unknown_usb_state_fail_closed`), mesmo com a TV ligada.
`playback_state()` separa `idle` | `playing` | `unknown`; só `idle` publica.
A UI avisa sempre que a publicação vai reconectar o pendrive, porque a
reconexão interrompe a navegação/reprodução atual do ConnectShare.

## 4. Handoff N→N+1 explícito (implementado, testado com device falso)

`handoff-lease` aceita `PREVIOUS_SESSION`; só nesse caso um lock vivo da geração
anterior é aceito, e apenas se `/proc/<pid>/cmdline` provar que o dono pertence
àquela sessão. `handoff-takeover` envia `TERM` (nunca `KILL`) a esse dono
provado, aguarda a liberação do `mode_switch.lock`, e recusa qualquer dono
estranho. Sem takeover bem-sucedido a ativação nem é tentada. Se a guarda for
perdida depois do takeover e antes da nova ativação, a geração anterior é
reconstruída pelo mesmo caminho supervisionado e o resultado real é gravado.

## 5. Container → publicador (decidido; depende da janela)

O caminho suportado e testado é o hop remoto: `MIXED_APPLY_CMD` é uma lista sem
shell (ex.: `ssh <host> python3 …/mixed_generation_service.py`) e o pacote
validado é enviado ao host que serve a mídia. `MIXED_VPS_PACKAGE_ROOT` e
`MIXED_VPS_SWITCH_SCRIPT_REMOTE` documentam os caminhos **do host remoto**.
Bloqueio real, não resolvível sem a janela: o container precisa do cliente
`ssh`, e o `MIXED_STATE_FILE` precisa ser o mesmo arquivo para servidor e
publicador (mount compartilhado) — nenhum dos dois foi implantado nem medido.

## 6. Caminho do host correto (corrigido)

`vps_publish_package` empacota o staging, envia por `scp`, extrai em
`$MIXED_VPS_PACKAGE_ROOT/<session>` (0700), roda `sha256sum -c SHA256SUMS` no
host remoto e só então passa esse diretório remoto ao switch. Sem hop, devolve o
staging local. `vps_app_dir_effective` nunca gera `--app-dir ''` nem `cd ''`.

## 7. Guarda sobrevive à troca longa (corrigido)

`GuardKeeper` renova a lease em thread própria durante toda a seção guardada e
`ensure()` revalida antes/depois de cada passo mutante (transferência,
preflight, decisão de ativação, troca VPS, ativação). Perder a guarda aborta
antes do LUN e libera a lease.

## 8. UI acompanha até o fim e preserva incerteza (corrigido)

O polling segue estados não-terminais por até 10 minutos (renovando o aviso),
tolera sonda indisponível sem inventar erro e, ao terminar, confirma no tablet.
`unconfirmed` diz explicitamente que **não** afirma que a lista anterior está
intacta; `blocked` distingue reprodução real de telemetria desconhecida.

## Evidência local desta rodada

- Python: `535 passed, 216 subtests passed` (`python3 -m pytest tests/ -q`).
  `test_mixed_generation_service.py`: 49 testes, incluindo confirmação
  somente-leitura, recusa de takeover, perda de guarda após takeover com
  restauração da geração anterior, transporte VPS remoto (tar/scp/sha256sum).
  `test_server_mixed_apply.py`: 11 testes, incluindo PIN obrigatório e formato
  do probe `--verify` sem `--background`.
- Shell: `test_activate_favorites_tunnel.sh` (17), `test_watchdog_favorites_handoff.sh`,
  `test_switch_mixed_generation_vps.sh` (13) e `test_start_mixed_vod_proxy_vps.sh` (5) PASS.
- Rust: `cargo test -p fuse_ntfs --offline -- --test-threads=1` 0 falhas
  (324 lib + integrações).
- Pacote de staging `scratch/mixed-staging/mixed-gen2-32abe7d7` revalidado com
  `scripts/validate_mixed_package.sh` (mesmos verificadores de startup Rust, não
  só hashes): 122 arquivos (2 Live + 120 VOD), 120 tasks na allowlist,
  `manifest_id 23307d13…ee50b`, imagem NTFS de 137438953472 bytes virtuais e
  allowlist aceita; a montagem é recusada apenas por falta de privilégio FUSE no
  host (esperado).
- Verificação independente por subagente `verifier`: 10 arquivos pytest da mídia
  mista `114 passed, 81 subtests`; 4 suítes shell PASS; `cargo test` com a única
  falha conhecida e pré-existente de corrida global em `phase_b_tests` (arquivo
  não modificado, passa com `--test-threads=1`); `git status` inalterado.
- `dashboard.html`: sintaxe JS validada com `node --check`.

## Continua pendente (exige janela livre e/ou hardware)

1. Implantar dependências e medir Drive/VPS/tablet reais; pastas, seek, Live e
   atualização de um download novo na TV.
2. `transport.token`/`chisel.auth` via `--private-dir` (sem eles o pacote é
   `incomplete`).
3. Decidir e testar a topologia container→publicador do item 5 (`ssh` no
   container + estado compartilhado) antes de ligar o botão.
