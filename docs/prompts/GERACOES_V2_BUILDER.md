# Prompt completo para o builder

Trabalhe em /home/sam/Code/usb-stream-tv-rust. Leia AGENTS.md e docs/architecture/GERACOES_V2.md integralmente. A branch observada foi main, com árvore suja; confirme estado atual e preserve todas as mudanças anteriores. Não faça reset/clean/stash/checkout destrutivo.

Objetivo: implementar a arquitetura Gerações V2, mantendo grade Live + séries + filmes, preparação automática e aplicação manual pelo botão existente, com falhas persistentes visíveis. O documento de arquitetura é a especificação; seus critérios de aceite são obrigatórios. Não prometa ausência de falhas.

Contexto: geração 18 foi restaurada em pasta alternativa no tablet; publicação 19 recusou previous_generation_owns_lock. Não conclua estado atual por este texto: inspecione somente-leitura. A raiz é dependência de caminho/identidade; não corrija removendo checagem de dono. No tablet su -c pode retornar 0 após falha; exija prova positiva. A implementação atual acompanha timestamp e banners temporários; precisa operação com ID e aviso persistente.

Siga as fases do documento nesta ordem:
1. Operação persistente e notificações globais no dashboard; compatibilidade da API. Estado e evento terminal na mesma transação; POST rápido, idempotência, polling único por operation_id, reload e falha de rede cobertos. Aviso não expira silenciosamente.
2. describe do supervisor, identidade por boot/instância/processo/manifesto/runtime, reconciliação sem depender do nome da pasta, protocolo de lease com fencing e ack. Supervisor antigo não é morto para forçar migração.
3. Transferência por allowlist, cache por digest, runtime reutilizado, arquivos parciais e limites de tempo/progresso. Nunca transferir montagem, imagem expandida, PID ou logs. Não misturar segredos ao cache compartilhado.
4. Máquina de estados recuperável, um publicador por tablet, autorização de interrupção vinculada ao inventário e reconciliação após crash. Não criar segundo writer concorrente nem retries cegos após ativação ambígua.
5. Validar host e revisão independente antes de qualquer janela de deploy/teste físico. Não reiniciar container, bridge, serviços, USB ou TV nesta execução sem autorização explícita e atual de janela livre. Não use bitrate/silêncio como autorização. Preparar roteiro reversível concreto e informar o que falta.

Arquivos principais: scripts/mixed_generation_service.py, scripts/mixed_bridge.py, scripts/activate_live_favorites_tablet.sh, scripts/build_mixed_media_package.py, scripts/switch_mixed_generation_vps.sh, server.py e dashboard.html. Integre com alterações existentes sem apagá-las; separar módulos por responsabilidade, evitar reescrever o projeto inteiro.

Não tocar istanbullu-sync nem iniciar sondagens IPTV. Não imprimir PIN, tokens ou URLs privadas. Telegram via eni-send é opcional: implementar adaptador/outbox e testes falsos; não enviar mensagens reais sem autorização de envio. Painel deve funcionar sem Telegram.

Verifique toda a matriz de aceite da arquitetura. Inclua testes significativos de crash entre efeitos, lock/PID/boot, checksum/ADB parado, exclusão/download, clique duplicado, retorno ao painel, erro persistente, revisão do inventário e already_current sem USB. Testes sintéticos não comprovam a TV. Execute suites focadas e shell pertinentes; amplie somente para riscos de regressão concretos. Não invente comandos/flags de BusyBox: valide compatibilidade real de modo não interruptivo.

Consulte ask-astra --deep para revisar recortes sanitizados que cubram cada fase crítica; fallback aider-argon somente leitura se indisponível. Não enviar só as primeiras linhas de um arquivo longo supondo revisão integral. Confira as sugestões com medição/testes; registre limitações. Duas correções falhas exigem escalonamento conforme AGENTS.md.

Entregue relatório com: arquivos/diff; critérios atendidos/pendentes; comandos e resultados reais; achados da revisão; estado operacional observado; o que foi implantado (se nada, diga nada); roteiro e rollback; bloqueios. Não declare conclusão só por testes passando nem pare em relatório se houver implementação autorizada ainda executável. Se houver dúvida impossível de resolver com inspeção, registre precisamente e avance nas frentes independentes.
