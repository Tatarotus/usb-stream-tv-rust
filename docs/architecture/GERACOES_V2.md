# Gerações V2: publicação verificável e avisos persistentes

Estado: arquitetura proposta; não implementada nem implantada por este documento.
Projeto inspecionado em 2026-11-06, branch local `main`, árvore com alterações anteriores preservadas.

## 1. Problema observado e limites

- A recuperação da geração 18 em outra pasta voltou a servir Live/VOD, mas o publicador da geração 19 recusou o handoff com `previous_generation_owns_lock`.
- `previous_supervisor_capability()` e o protocolo de lease usam caminho derivado de session. Recuperar o conteúdo sem reconciliar a identidade quebra a próxima publicação.
- `transfer_package()` envia o diretório de staging com `adb push --sync`. Nesta rodada houve espera de minutos na transferência; a causa da lentidão não foi medida. Não atribuir automaticamente a binários, rede ou imagem expandida.
- `applyMixedGeneration()` acompanha status por timestamp e usa banners temporários. Não há garantia de que um usuário que fecha/reabre a página veja a falha.
- O retorno de `su -c` do tablet não é prova suficiente; exigir resposta positiva estruturada.
- Nenhuma arquitetura garante ausência de falhas de rede, energia ou firmware. Objetivo: falha visível, conservação da geração anterior quando possível e recuperação sem ações cegas.

## 2. Comportamento do produto

Download/exclusão muda o inventário desejado e prepara a próxima lista em segundo plano. Não reconecta USB. O botão existente NA TV solicita aplicar o inventário atual; se exige reconexão, apresenta aviso explícito antes de autorizar. Não começa reprodução nem marca assistido.

Se o inventário já estiver confirmado no tablet, retorna `already_current` sem reconectar. Se houver falha, o painel mostra permanentemente: etapa, mensagem compreensível, hora e ação possível. Fechar/reabrir não apaga o resultado. Nenhum botão genérico deve confirmar interrupção silenciosamente.

## 3. Responsabilidades e armazenamento

Manter bridge/publicador existente, sem criar outro orquestrador paralelo. Extrair gradualmente módulos pequenos: inventário, artefatos, operações, transporte e reconciliação. `mixed_generation_service.py` permanece adaptador CLI compatível durante a migração.

- Inventário: snapshot canônico com todas as tarefas prontas, revisões confiáveis e Live aprovado. Revisão ausente ou catálogo ilegível = desconhecido, nunca catálogo vazio.
- Artefatos: pacote imutável identificado por digest do manifesto e digest completo do pacote. Uma geração numérica é apenas rótulo humano.
- Runtime: binários/script com versão própria. Conteúdo novo não deve exigir recompilar ou reenviar runtime idêntico.
- Operações: SQLite no host da VPS, fora de bind-mount de arquivo individual; container acessa via bridge. Transações, busy timeout, unicidade de chave idempotente e no máximo uma publicação ativa por tablet. Não manter transação SQLite aberta durante RPC. Usar armazenamento local; não Drive/NFS. Locks do tablet continuam necessários.
- Estado observado: `actual` só muda por confirmação do dispositivo. `desired`, `prepared` e `actual` são distintos; journal local não prova ativação.

## 4. Identidade e recuperação do tablet

Supervisor deve oferecer `describe` com versão do protocolo, device_id provisionado, boot_id, instance_id aleatório, PID/starttime, geração, manifest_id, package_digest, runtime_digest, caminho, LUN e lock_owned. Validar campos e provar posse por leitura do lock e processo; caminho é localização, não identidade.

Antes de publicar: descobrir supervisor ativo pelo lock/registro protegido, consultar `describe`, cruzar LUN e processo e verificar artefatos. Dados conflitantes => `needs_reconciliation`, sem sinalizar processos nem trocar USB. PID/instance_id/starttime impedem confusão após reboot ou reutilização de PID.

Recuperação em pasta alternativa é aceita apenas quando há prova completa do supervisor e artefatos. Reconciliar a localização observada no registro ativo antes de negociar handoff. Não aceitar arbitrariamente qualquer pasta que contenha o mesmo manifesto. Não relaxar a checagem de dono para resolver a geração 18.

Lease deve autorizar explicitamente a transição da instância observada para a operação nova e expirar. Renovação e takeover com token secreto, nunca exposto na API pública. Ack durável vinculado a operation_id, boot_id e instance_id. Perda de guarda impede avanço. O hardware pode iniciar leitura fora do servidor; não prometer exclusão de uso baseada apenas em bitrate/TV ligada.

## 5. Preparação e transferência

1. Snapshot do inventário + hash determinístico. Agrupar eventos próximos (debounce configurável, inicial 5 s) e descartar preparações antigas antes de publicar.
2. Construir pacote em diretório temporário exclusivo; validar com verificadores Rust e checar capacidade. Registrar omissões explicitamente. Publicar pacote local por rename após validação.
3. Transferir somente allowlist de artefatos: manifestos, layout, template comprimido e runtime ausente. Nunca enviar `mnt/`, imagem expandida, logs, PID, sockets ou journal. Não copiar segredos em objetos compartilhados; usar diretório privado separado e versionado.
4. Cache por SHA-256 no tablet. Upload `.part.<operation_id>`, verificar tamanho e digest, depois rename. Timeout por chamada e por falta de progresso; cancelar somente subprocesso de transferência próprio. Falha não toca USB.
5. Inicialmente manter `adb push` por artefato; reutilização por digest remove transferências repetidas. Não introduzir HTTP pull antes de medir se ADB é gargalo. Testar rename e utilitários no BusyBox real.
6. Somente depois dos artefatos válidos: expandir template esparso fora da montagem ativa e preparar candidato. Não alterar manifesto em uso. Não remover arquivos referenciados por montagem/rollback ativo.

## 6. Máquina de estados por operação

`queued → preparing → transferring → validating → ready_to_activate → activating → verifying → succeeded`

Saídas adicionais: `already_current`, `failed_safe`, `blocked`, `superseded`, `needs_reconciliation`. `interrupted` após reinício do worker exige reconciliação antes de nova ação.

- Cada operação tem ID próprio; UI acompanha ID, nunca infere correlação por timestamp.
- Persistir fase antes e resultado depois de cada efeito externo; lease e fencing impedem worker antigo de promover estado novo.
- Antes da ativação reconferir inventário sob lock; nova mudança invalida o alvo ou exige nova operação, sem alterar pacote imutável.
- `ready_to_activate` não significa aplicado. Exige autorização explícita de interrupção vinculada à revisão alvo, com expiração; mudanças posteriores não herdam autorização irrestrita.
- Preparar candidato e provar processo/montagem/bytes antes de desligar USB. Identidade pode demorar a aparecer no exec inicial; esperar com prazo, mas exigir prova exata antes da troca.
- `succeeded` exige verificar manifesto, runtime, posse do lock, LUN, USB e serviços com bytes reais. Isso confirma estado técnico, não imagem/som físicos.
- Timeout depois de pedir ativação => `needs_reconciliation`; consultar dispositivo antes de rollback/retry. Nunca dizer que nada mudou sem prova.
- Reiniciar worker não republica automaticamente operação interruptiva. Se já aplicada e comprovada, finaliza sucesso; se não aplicada, aguarda autorização válida; se ambígua, bloqueia.

## 7. Contrato de API e notificações

Reutilizar `/api/mixed/apply`, `/refresh`, `/status` com compatibilidade durante migração. POST autenticado aceita chave idempotente; retorna HTTP 202 e operation_id persistido antes de executar. Duplicata recebe a mesma operação; outra requisição concorrente recebe referência à operação em curso. O payload público nunca contém PIN, token, URL privada ou caminho local.

Status por operação: `operation_id`, `state`, `stage`, `stage_started_at`, `updated_at`, `heartbeat_at`, `bytes_done`, `bytes_total`, `desired_generation`, `observed_generation`, `error_code`, `user_message`, `retryable`, `reconnected`, `next_action`. Totais desconhecidos são null. Não inventar porcentagem nem duração.

Evento terminal é salvo na mesma transação do resultado. GET autenticado de notificações entrega eventos posteriores a cursor; confirmação de leitura por cliente/usuário não apaga a operação. Não confundir aviso visto com problema resolvido.

Dashboard monta um indicador discreto persistente perto do controle da TV, acessível por leitor de tela e sem estourar largura mobile. Atualiza em qualquer aba: 2 s enquanto há operação, 15 s parado, backoff até 30 s em falha de rede; consulta imediata em focus/visibilitychange. Poll global único, sem laço independente por botão. Uma aba reaberta carrega operação e eventos pendentes. Aviso terminal não desaparece por temporizador; usuário pode dispensar, e resultado permanece consultável.

Exemplos:
- `Transferindo lista: 420 KiB de 900 KiB. A lista atual continua disponível.` (somente se essa conservação estiver comprovada).
- `Não foi possível atualizar: a TV usa uma sessão recuperada ainda não reconciliada. A lista nova não foi aplicada.`
- `Conexão perdida após a troca. Verificando o estado da TV; não tente novamente agora.`
- `Lista atualizada. Se necessário, selecione novamente o pendrive no ConnectShare.`

Watchdog do worker detecta heartbeat vencido: isso sinaliza operação possivelmente parada, não comprova falha segura. Marca reconciliação necessária e notifica; não remove lock ativo por relógio apenas.

Telegram opcional por `eni-send`, somente quando configurado/autorizado. Outbox persistente, timeout curto, retry com backoff, deduplicação por evento e limite de frequência. Falha Telegram não bloqueia publicação. Entrega pelo menos uma vez pode duplicar após crash; não prometer exactly-once. Painel funciona sem Telegram. Nenhum envio nesta fase de arquitetura.

## 8. Migração incremental e rollback

1. Primeiro corrigir observabilidade no fluxo atual: operation_id, fase/heartbeat, erro persistente e painel global. Não mudar USB/runtime junto. Testes de UI: falha, navegação entre abas e reload.
2. Adicionar describe/reconciliação e testes de identidade. Compatibilidade: supervisor antigo só recuperado por roteiro supervisionado em janela livre; nunca sinalizar versão antiga supondo suporte. Importação do journal é transacional e preserva original.
3. Separar runtime e pacotes; transporte com allowlist/hash e cache. Migração sem apagar geração ativa ou tokens.
4. Ativar máquina de estados com flag desabilitada por padrão; um único publicador, sem writers V1/V2 simultâneos. Validar shadow read-only antes de publicar.
5. Janela física: confirmar TV livre com usuário, snapshot e rollback testado, publicar geração contendo Masterchef EUA T14E01, verificar pastas, série/seek, Live e idempotência. Alterações de inicialização/boot em fase separada para não misturar resultados.

Rollback de código não reverte USB. Reconciliar primeiro; restaurar explicitamente pacote/serviços anteriores pelo supervisor se necessário. Preservar runtime correspondente, banco e outbox. Não executar comandos Git destrutivos, sobrescrever trabalho alheio nem editar inode de bind-mount por rename em produção.

## 9. Matriz obrigatória de aceite

Todos pendentes de implementação:

| Cenário | Resultado exigido |
|---|---|
| Download ou exclusão durante reprodução | Prepara; zero reconexões automáticas |
| Clique duplicado/reload/two workers | Mesmo operation_id ou conflito explícito; no máximo uma troca |
| Recuperação em outra pasta | Identidade provada e reconciliada; ou bloqueio visível sem sinal |
| PID reutilizado/reboot/lease perdido | Recusa takeover e promoção sem prova nova |
| ADB lento/parado/checksum ruim | Prazo limitado e erro com etapa; zero troca antes da validação |
| Template inválido/imagem expandida no staging | Validador rejeita; transferência nunca inclui imagem expandida |
| Crash antes/depois de cada efeito | Reinício reconcilia resultado; nenhuma repetição cega |
| Falha após pedido de ativação | needs_reconciliation, sem promessa de geração anterior intacta |
| Alteração simultânea de inventário | Sucesso não atribuído à revisão errada |
| Falha UI/rede, sair e voltar | Operação/aviso reaparecem; nenhum sucesso antigo mascara pendência |
| Conteúdo já atual | Zero USB reconnect, mesmo supervisor e starttime |
| Telegram indisponível | Painel informa resultado; publicação não bloqueada |

Metas a medir, não promessas: falha terminal visível em até 5 s com painel em primeiro plano/rede saudável; submissão responde sem aguardar ADB; transferência mostra bytes reais; atualizações repetidas reutilizam runtime sem reenvio. Medir tempos por fase, p50/p95 quando houver amostra suficiente, antes de impor SLA de publicação.

## 10. Contratos adicionais após revisão independente

Revisão de arquitetura via `ask-astra --deep` nesta rodada; não é auditoria de implementação. Cinco lacunas identificadas e incorporadas como requisitos verificáveis:

1. Identidade: fixar tupla device_id/boot_id/instance_id/PID/starttime/digests; revalidar sob a guarda do tablet imediatamente antes da mutação. Atualização da localização observada por compare-and-swap. Divergência exige nova descoberta, nunca adoção por caminho ou manifesto isolado.
2. Fencing: árbitro do tablet persiste época crescente e serializa validação e início de cada efeito. Rejeitar comando atrasado, boot antigo e worker sem posse; não conceder nova mutação enquanto efeito anterior estiver ambíguo. Host reserva atomicamente snapshot/revisão/digests/autorização e usa compare-and-swap nas transições. Mudanças posteriores no catálogo criam pendência nova, não modificam o alvo reservado nem ficam mascaradas pelo sucesso anterior. Integrar versão do inventário com seus escritores, sem bloquear downloads durante RPC.
3. Journal no dispositivo: persistir operation_id, alvo, época e checkpoint antes do efeito interruptivo, mais resultado observado depois. Consulta por operation_id disponível após reboot. Intenção sem resultado não comprova execução nem ausência dela: reconciliar LUN/lock/artefatos/serviços; persistindo ambiguidade, bloquear e avisar, sem repetição ou rollback automático.
4. Idempotência: chave no escopo do principal/tablet, hash da intenção e revisão resolvida, tudo persistido atomicamente. Mesma chave/intenção retorna operação original; chave com intenção diferente retorna 409. Outra intenção concorrente recebe 409 + active_operation_id, sem aceite ou autorização herdada. Definir retenção e rejeição de chaves expiradas no protocolo; não reciclar silenciosamente em nova ativação.
5. Avisos: registrar entrada em needs_reconciliation e heartbeat vencido, deduplicados por transição. Snapshot inicial inclui problemas não resolvidos independentemente do cursor. Dispensar detalhe não remove indicador de problema ainda pendente. Paginação com sequência durável e cursor expirado exige ressincronização. Só prova da resolução daquele efeito encerra sua pendência; sucesso não relacionado não a apaga.

Adicionar testes de cada contrato acima, incluindo comando atrasado de worker anterior, mudança de identidade entre describe e ação, crash após efeito antes da resposta e alerta já lido reaparecendo como pendência.

## 11. Arquivos de entrada e verificações existentes

`scripts/mixed_generation_service.py`, `scripts/mixed_bridge.py`, `scripts/activate_live_favorites_tablet.sh`, `scripts/build_mixed_media_package.py`, `scripts/switch_mixed_generation_vps.sh`, `server.py`, `dashboard.html`; inspecionar também alterações não commitadas de `scripts/mixed_services_supervisor.sh` antes de integrar.

Executar suítes focadas dos arquivos alterados, testes de transporte/estado e shell do tablet. Não fazer varredura IPTV durante isso: plano admite só uma conexão. Não reiniciar serviços/USB nem publicar durante elaboração/revisão. Depois de validação host, distinguir integração real e teste físico; relatar limites.
