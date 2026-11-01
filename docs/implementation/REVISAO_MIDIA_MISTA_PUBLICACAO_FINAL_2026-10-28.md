# Revisão da publicação e novo pacote

Resultado: o novo pacote avançou e a correção do empacotamento foi confirmada; ativação automática ainda requer correções de orquestração. Nenhuma mudança na produção durante esta revisão.

## Verificado

- `scratch/mixed-staging/mixed-gen2-32abe7d7`: 8 hashes conferem; executáveis ARM32 estáticos.
- Leitura independente do SPARSE01: novo pacote tem assinatura FILE nos 122 registros MFT dos arquivos. Nos pacotes antigos examinados houve 83 registros inválidos a partir do inode 112. O defeito relatado existe, embora a contagem anterior de 78 não coincida com esta inspeção. Não ativar pacotes antigos.
- Testes focados Python de geração, pacote, template, proxy e fluxo integrado: 53 passaram e 72 subtestes. Scripts de túneis, início do proxy e handoff passaram.
- Não houve teste real de Drive/VPS/tablet/TV nesta revisão. A árvore atual e os testes locais não comprovam publicação real.

## Bloqueios concretos remanescentes

1. **Reprodução pode começar durante o preparo.** `scripts/mixed_generation_service.py:_publish_locked` verifica ociosidade antes de transferir e testar, mas chama `activate_package` sem reavaliar o estado ou manter uma guarda coordenada com novos comandos. Reproduzido em teste local: o preflight muda o estado para VOD em reprodução, e a publicação ainda chama start e retorna ACTIVE. Não basta uma consulta inicial nem apenas outro check isolado: coordenar a decisão com o tablet/controle de modo e abortar com segurança se o estado mudar.
2. **Recuperação pode desfazer sessão ativa.** No estado `unconfirmed`, o mesmo método chama `rollback_package` antes de verificar reprodução. Definir o que fazer quando recuperação não consegue confirmar estado mas alguém pode estar assistindo; estado incerto não pode autorizar interrupção automática de uma sessão alheia.
3. **Troca de geração precisa ser completa.** O orquestrador atual opera o tablet e não reconfigura por geração o manifesto/sessão do manager Live nem a allowlist do proxy VOD da VPS. Ambos são carregados ao iniciar os serviços. Um VOD novo pode receber 404 do proxy antigo, e o manager rejeitar OPEN com manifesto/sessão novos. O supervisor da geração anterior também mantém o mode_switch.lock enquanto ativo; não há handoff explícito N→N+1 antes de iniciar o novo supervisor. Testar duas gerações consecutivas realmente servindo bytes e Live, rollback de todo o conjunto e falhas em cada fase.
4. **Transporte SSH usa caminho do host errado.** `TabletTransport.push` passa o caminho local para `ssh host adb push ...`, sem transferir antes o pacote à máquina que possui ADB. Implementar transporte real dos arquivos ou restringir/documentar execução exclusivamente na VPS e validar essa pré-condição, sem fingir suporte remoto funcional.

## Próximas validações

Corrigir os bloqueios sem tocar na reprodução ativa; criar testes que mudam o estado durante transferência/preflight, recuperação com espectador e duas gerações com processos de servidor reais. Validar o pacote expandido com os mesmos verificadores do startup Rust, não apenas hashes/JSON. O teste `mixed_staging_manifest_and_layout_bind_without_errors` ainda referencia pacote antigo e não substitui essa prova. Depois, com janela livre, implantar dependências e medir Drive real, testar pastas/seek/Live e atualização de novo download.

---

# Atualização 2026-10-28 — implementação dos quatro bloqueios

Publicação **manual** pelo botão existente (`✅ NA TV`): o clique autoriza
preparar/validar/publicar e reconectar o pendrive; baixar um VOD não publica
nada (o daemon apenas prepara em segundo plano). Nada foi implantado no
tablet/VPS/TV e nenhum tráfego real foi exercitado.

## 1. Reprodução x publicação (resolvido no software)

- `server.py`: `ModeGuard` + `POST /api/mode_guard` (lease exclusiva com dono,
  token de 128 bits, TTL 15–120 s, renovação e liberação autenticada).
  `dispatch_device_cmd` e `handle_vod_play` recusam comandos estrangeiros
  enquanto a lease vive, com **zero efeitos colaterais** (teste dedicado).
- `scripts/activate_live_favorites_tablet.sh`: lease de handoff
  (`$BASE/mixed_handoff.lease`, 600) com dono e validade; `start` exige lease
  válida e só então adquire `mode_switch.lock`; `trap` libera apenas o que é
  seu; `verify-active` informa `lock_owned=` e `lease=none|ours|foreign`.
- `scripts/tv_watchdog.sh`: recusa comandos (`crc=75`, "Handoff in progress")
  sob lease estrangeira viva e não desfaz favoritos nesse estado.
- `scripts/mixed_generation_service.py`: adquire a guarda, renova entre etapas,
  reavalia ociosidade **sob** a guarda, libera sempre (inclusive em bloqueio
  após o preflight) e não publica sem autorização manual.

Limite físico assumido: o controle remoto da TV pode iniciar leitura SCSI sem
passar por software; nenhuma guarda de software elimina isso. Mitigação:
verificação fail-closed, lease, recusa do watchdog e aviso explícito ao
usuário de que a atualização interrompe a reprodução.

## 2. Recuperação não derruba sessão alheia (resolvido)

`resume_publication` só promove quando o tablet confirma a mesma
geração/manifesto; em `unconfirmed` retorna `manual_reconciliation_required`
sem rollback e sem nova troca. O caminho de exceção (`_recover_failed_publication`)
verifica o tablet antes de decidir: confirma → promove; não confirma →
restaura a geração anterior **dos serviços** e grava estado ambíguo
(`manual_reconciliation_required`) em vez de afirmar falha limpa. Liberação de
guarda não confirmada nunca é registrada como liberada (`guard_held`).

## 3. Troca de geração completa (implementado, não exercitado em produção)

`scripts/switch_mixed_generation_vps.sh start|stop|status` valida o contrato do
pacote, cria diretório privado por sessão (700/600), registra `state.json` +
`current` atômicos, sobe manager Live + proxy VOD com manifesto/sessão/allowlist
da geração, prova leitura real de bytes em `/probe`, confirma `"ok": true`,
mata somente PIDs registrados (verificados por `/proc/<pid>/cmdline`) e restaura
a sessão anterior. O serviço encadeia: guarda → transferência → túneis →
preflight → troca VPS → lease → start → confirmação → promoção, com rollback do
conjunto em falha. Teste cobre duas gerações consecutivas, falha da VPS, recusa
de lease, perda da guarda e ativação ambígua.

## 4. Transporte SSH (documentado e restrito)

`mixed_ssh_transfer.py` envia o pacote à VPS (spool privado `0700`) e só então
faz `adb push`; falhas de scp/ssh/adb, limpeza e recuperação têm testes. Resíduo
conhecido: se o SSH cair durante a limpeza, o spool `/tmp/mixed-vod-XXXXXXXX`
pode permanecer; a exceção de limpeza pode mascarar o erro original (registrado,
não corrigido).

## Provas desta rodada (locais, sem hardware)

- Python: 526 testes + 216 subtestes; 4 suítes shell (túneis, watchdog,
  proxy VPS, troca de geração) PASS.
- Rust: `cargo test -p fuse_ntfs --offline` 324 lib + integrações, 0 falhas
  (execução single-thread; duas provas pré-existentes de `phase_b_tests` são
  instáveis em paralelo por estado global de `test_seam.rs`).
- Pacote `scratch/mixed-staging/mixed-gen2-32abe7d7` revalidado:
  122 caminhos (2 Live, 120 VOD), `manifest_id 23307d13…ee50b`, hashes, layout,
  allowlist e imagem NTFS/MFT (137438953472 bytes virtuais) — exit 0.
- Dashboard: sintaxe JS validada com `node --check`.

## Pendências reais

1. Janela livre autorizada: implantar dependências, medir Drive real, testar
   pastas/seek/Live e a atualização manual de um novo download na TV.
2. Fornecer `transport.token`/`chisel.auth` via `--private-dir` (o pacote fica
   `incomplete` sem eles).
3. Container de produção ainda sem `server_time`, `content_revision`,
   `vod_revision.py` e sem porta 8080 publicada no host; o proxy deve apontar
   para `https://tv.smre.run.place`. `MIXED_APPLY_CMD` precisa ser configurado
   para o botão sair do modo legado.
