# Atualização da lista pelo botão Assistir na TV

O operador decidiu que o clique manual autoriza interromper a reprodução, sem
diálogo de confirmação, inclusive quando a telemetria da reprodução estiver
desconhecida. O PIN continua obrigatório. Publicações automáticas não recebem
essa autorização; as verificações de transporte, guarda e publicação continuam.

Em 04/11/2026, no clique de teste às 1h25 (horário de São Paulo), o journal
registrou a geração 18 como ativa e reconectada após cerca de 4min19s; o operador
confirmou que o VOD novo apareceu. Não houve diálogo e a TV continuou reproduzindo
durante a transferência. Foi observado `adb push --sync` em andamento durante
mais de três minutos. A resposta HTTP do clique não estava disponível; como a
operação terminou antes de dez minutos, o prazo do painel não foi medido.

O fluxo atual transfere o pacote antes de trocar os serviços e o pendrive. Logo,
a autorização do clique não garante interrupção imediata. A recuperação segura
após interromper antes da transferência ainda precisa de projeto e validação.

Em 04/11/2026 o painel recebeu uma ação de atualização do inventário completo,
sem depender de um vídeo selecionado, e marcação manual do episódio no histórico.
O publicador passou a registrar durações por etapa; não se deduz da duração total
que uma etapa específica tenha sido a causa da demora. Os arquivos do painel e
do publicador foram instalados no VPS sem reiniciar os serviços. Ainda falta
verificar uma nova atualização e as marcações na TV/PWA reais.
