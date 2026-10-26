# Prompt de execução: lembrar onde o usuário parou nas séries

Implemente um histórico persistente de séries no USB-Stream-TV para o usuário
maratonar, encerrar a sessão e encontrar seu progresso no dia seguinte.
O objetivo principal é **lembrar o episódio correto**, inclusive quando a TV
avança para outro arquivo pelo controle físico. Tempo exato é uma capacidade
separada: não invente precisão nem prometa um seek que o hardware não oferece.

## 1. Resultado esperado pelo usuário

No controle web deve existir uma área **Continuar assistindo**, contendo:

- Nome e pôster da série.
- Último episódio confirmado e quando o progresso foi guardado.
- Botão **Continuar episódio N**.
- **Marcar como assistido**, **Ainda estou neste episódio** e correção manual
  do episódio para resolver avanços indevidos ou sessões que continuaram sozinhas.
- **Guardar onde parei**, com confirmação clara do que foi guardado.
- Minuto somente quando houver uma origem confiável; se estimado, escrever
  **posição aproximada**. Se desconhecido, mostrar apenas o episódio.

Exemplo: o usuário assiste aos episódios 170, 171 e parte do 172. Amanhã, mesmo
após fechar o navegador ou reiniciar a VPS, aparece **Continuar episódio 172**.
Se ele marcou o 172 como concluído, oferecer o próximo disponível, o 173.
Se o 173 ainda não estiver pronto, informar isso; não pular silenciosamente.

Se a TV continuar exibindo episódios enquanto a pessoa dorme, nenhum dado de
leitura comprova até onde ela prestou atenção. Ofereça histórico recente e
correção manual. **Guardar onde parei não deve afirmar que pausou ou desligou a
TV** se não existe um comando de pausa/desligamento validado. Não use “voltar
ao vivo” como se fosse parar a reprodução para dormir.

Execute implementação e testes; não termine apenas com outra proposta.
Informe separadamente o que foi validado em software e na TV física.

## 2. Ambiente e escopo

- Projeto local: `/home/sam/Code/usb-stream-tv-rust`.
- VPS acessível por `ssh oracle`.
- Produção: `/opt/containers/apps/usb-stream-tv`, container `usb-stream-tv`.
- Compose usado na última implantação: `compose.gdrive.runtime.yaml`.
- Servidor HTTP: `server.py`; painel principal: `dashboard.html`.
- Armazenamento: `vod_storage.py`, `vod_playback_cache.py`.
- Driver Rust: `crates/fuse_direct/src/`.
- Troca de modo: `scripts/switch_tv_mode.sh`.
- Heartbeat do tablet: `scripts/tv_watchdog.sh`.
- Deploy do tablet: `scripts/deploy_tablet.sh`.
- Leia os AGENTS.md e `docs/GDRIVE_VOD.md` antes de alterar código.

Há alterações preexistentes e outro trabalho de migração dos episódios para o
Drive. Preserve-os. Não substitua todo o servidor remoto por uma versão local
antiga. Coordene mudanças nos mesmos arquivos com o outro agente.
Não altere render, qualidade, sincronismo, Live TV ou o produtor istanbullu-sync
para implementar histórico. A migração local → Drive deve preservar o progresso.

## 3. Limitações já encontradas no código

Confirme no código atual; as linhas podem mudar depois de outras tarefas.

1. `ACTIVE_VOD_TASK`, `active_vod.txt` e `vod_mode.flag` representam seleção/modo,
   não um histórico confirmado. `touch_vod_stream()` também é chamado por
   acessos HTTP de mídia/template. Não transforme isso em “assistido”.
2. O watchdog envia o ID inicial de `vod_mode.flag`; hoje não informa qual
   episódio de uma playlist a TV realmente abriu nem o minuto reproduzido.
3. HTTP Range não significa consumo na tela. Há cabeça antecipada de 8 MiB,
   prefetch de 4 MiB no cliente e cache/pré-carregamento na VPS. Downloads,
   aquecimento, probes de validação e varredura de diretório não são progresso.
4. A TV também pode ler índices no fim do arquivo antes de começar. Ler o fim,
   baixar tudo ou receber EOF não significa que o episódio terminou na tela.
5. Os parsers atuais reconhecem S01E01, 1x01, Temporada/Episódio, mas não
   necessariamente `A Noiva de Istambul 178 - Star 528p`. Essa série importada
   pode não ter `series_id`. Não deixe o caso concreto do usuário fora do recurso.
6. `get_series_playlist()` é recalculado ao gerar o template e ao servir
   `file_<índice>.mp4`. Publicar episódios durante uma sessão pode mudar índices.
   Um índice de arquivo não é identidade persistente de episódio.
7. **Confirme o driver realmente instalado antes de instrumentar.** O Rust
   possui `FatTemplate.find_vod_file`, mas o `DiskServer::serve_disk` inspecionado
   usa um offset linear e um único `VodEngine`, sem consultar esse mapeamento.
   Não presuma que ele já reproduz múltiplos episódios como o binário instalado.
8. O Volume Serial do FAT32 é randomizado. Não dependa de bookmark nativo da
   Samsung sobreviver a remontagens sem testar. Preserve os requisitos anti-cache
   de Live TV; não os desative para conseguir um teste de VOD.

## 4. Primeiro passo: identificar a fonte real de progresso

Faça inspeção de leitura e documente:

- Qual binário o tablet executa, caminho, hash, arquitetura e fonte C/Rust.
- Se uma playlist contém vários arquivos e como cada leitura USB identifica
  o arquivo/episódio e seu offset dentro dele.
- Se a reprodução seguida de dois episódios realmente funciona no hardware.
- Que sinais existem para distinguir reprodução, seleção, preview e probes.

Use o binário que está em produção como referência. Não troque C por Rust nem
refatore o motor inteiro sem necessidade. Se o driver Rust for o utilizado e
não mapear múltiplos arquivos corretamente, registre essa dependência e trate-a
isoladamente, com testes; não finja detectar a troca de episódios.

Não precisa alterar Live TV para observar VOD. Instrumentação deve ficar no
caminho correto de VOD e preservar as invariantes e os prazos do FUSE/USB.

## 5. Identidade estável de série e episódio

Crie um resolvedor compartilhado no backend, com testes, que entregue:

```text
series_key estável
series_title
season_number opcional
episode_number
task_id
identidade/revisão da mídia
```

Prefira o ID de série do catálogo quando existir. Para importações, use uma
chave explícita e persistente, por exemplo `import:istanbullu-star`.
Não mude task_ids nem renomeie objetos do Drive para criar o histórico.

Faça backfill validado para os títulos conhecidos da série:

```text
A Noiva de Istambul 131 - Star 528p → episódio 131
A Noiva de Istambul 178 - Star 526p → episódio 178
```

O número 528/526 é resolução, não episódio; ano de lançamento também não é
episódio. Evite regex genérica que transforme filmes numerados em séries.
Não invente temporada se a origem não a informa. Não junte cortes/edições
incompatíveis por semelhança de título. Backend e frontend devem usar a mesma
identidade, em vez de manter classificações divergentes.

Backfill de metadados acontece pelo processo dono do catálogo, sob lock e
persistência atômica. Não edite `vod_meta.json` externamente com o servidor vivo.

## 6. Histórico persistente e API

Crie um módulo próprio, por exemplo `vod_history.py`, e armazenamento local
pequeno dentro do volume persistente, por exemplo `vod_cache/watch_history.json`.
Não use apenas localStorage e não coloque esse estado no cache descartável do
vídeo atual. Um perfil doméstico padrão basta; não crie fluxo de contas/login.

Guardar por série:

- Último episódio selecionado, separado do último confirmado como assistido.
- Checkpoint do episódio em andamento.
- Posição em segundos opcional, origem e confiança (`manual`, `web`, `usb_estimated`).
- Episódios marcados como concluídos.
- Histórico recente para correção, sem logs ilimitados por bloco lido.
- Timestamps do servidor, revisão do registro e identidade da sessão de reprodução.

Requisitos:

- Escrita atômica, locks curtos e recuperação de arquivo inválido sem travar VOD.
- Não iniciar processos, rede ou threads durante import do módulo.
- Persistir periodicamente e em checkpoints explícitos; nada de fsync por leitura.
- Rejeitar eventos atrasados de uma sessão antiga e duplicatas.
- Um seek para trás pode reduzir a posição salva: não usar o maior offset visto
  como progresso permanente.
- Se a mídia mudar, invalidar apenas a posição incompatível, preservando o histórico.
- Excluir um VOD não pode apagar toda a história da série. Mostrar indisponível
  ou resolver o episódio equivalente comprovado; não escolher outro por engano.
- Migrar o mesmo task_id para o Drive não altera a identidade do progresso.

Sugestão de contrato, adapte aos padrões reais do projeto:

```text
GET  /api/vod/history
POST /api/vod/history/checkpoint   # marcador explícito do usuário
POST /api/vod/history/completed
POST /api/vod/history/reset
POST /api/vod/continue
```

Autentique alterações pelo PIN existente, lido privadamente. Telemetria do
tablet deve usar autenticação própria existente/adequada e sessão validada.
Não aceite um POST anônimo dizendo que todos os episódios foram assistidos.
Não exponha PIN/tokens em URLs, arquivos Git ou logs.

“Continuar” deve resolver o checkpoint e chamar o fluxo de reprodução existente,
incluindo aquecimento do Drive/cache. Não dispare reprodução só por abrir a página.
Sem progresso registrado, mantenha o comportamento normal, sem escolher ao acaso.

## 7. Detectar a troca real de episódio durante a maratona

Crie um snapshot imutável por sessão:

```text
session_id → geração, edição, lista de task_ids,
             file_idx/intervalo de clusters → task_id e offset local
```

Template, leitura dos arquivos e telemetria devem usar o MESMO snapshot.
Se novos episódios forem publicados enquanto a TV assiste, não remapeie os
arquivos que ela já tem montados. Identifique o episódio atual pelo arquivo
efetivamente lido, não pelo task_id inicial ou pelo nome visual truncado.

Instrumente o ponto de entrega de dados demandados pelo USB/FUSE, inclusive em
cache hit. Observar só pedidos de rede perde leituras atendidas pelo cache.
Registre somente após entrega bem-sucedida e use confirmação de cadeia/duração
para filtrar probes e leitura adiantada. Os limiares precisam ser medidos na TV.
Somente abrir diretório, ler cabeçalho ou tocar a cauda não confirma episódio.

Rede, JSON, hashing e persistência não podem bloquear o handler FUSE.
Use estado compartilhado leve e um worker/fila limitada que agrega progresso,
por exemplo no máximo um envio a cada 10–15 s. O watchdog pode transmitir esse
estado, mas ler apenas `vod_mode.flag` não resolve o episódio real.
Falha de telemetria nunca deve interromper vídeo, atrasar SCSI ou gerar NULLs.

Não marcar “concluído” apenas por leitura do fim ou por ter carregado tudo no
cache. Confirmação explícita do usuário é válida. Qualquer conclusão automática
na TV precisa de evidência e tolerâncias verificadas; não invente essa evidência.

Entregue desde o começo os marcadores manuais e o botão de continuar. Porém,
não declare que a maratona automática foi resolvida se só guardar o último
episódio clicado no navegador. Informe essa diferença no relatório.

## 8. Minuto e retomada dentro do episódio

Para vídeo HTML5 reproduzido no navegador, eventos de reprodução real e
`currentTime` permitem checkpoints e restauração após carregar os metadados.
Separe essa origem de previews e da reprodução na TV; não use um preview web
para sobrescrever silenciosamente o progresso confirmado da TV.

Na TV USB, bytes lidos não são o relógio do decodificador. Não use
`offset / file_size * duration` como minuto exato: vídeo VBR, áudio intercalado,
metadados, cache e read-ahead tornam isso incorreto.

Se for oferecer posição aproximada, use índice real de amostras/PTS do MP4,
filtros de leitura e marcador conservador, com validação física e rótulo explícito.
Não baixe/análise toda a biblioteca de 5 TB para criar a primeira versão.
Um horário informado manualmente pelo usuário também é um checkpoint válido,
mas a UI deve explicar se a TV ainda exige avançar usando o controle físico.

**Não implemente seek somando um offset aos bytes de movie.mp4.** Isso quebra o
MP4 e seus índices. Não afirme que enviar segundos para `/api/vod/play` faz a TV
buscar automaticamente: esse recurso não existe hoje.

Retomada automática no minuto da TV só pode ser entregue após provar uma via:
bookmark nativo estável ou arquivo/segmento MP4 derivado válido, com índices,
timestamps, tamanho e template corretos. Um derivado deve ser temporário,
limitado ao vídeo atual, preservar o original do Drive e registrar sua origem
temporal. Não acrescente transcodificação ilimitada nem degrade áudio/vídeo.

Se essa via não for validada, entregue **Continuar episódio N** e histórico com
correção manual. Marque retomada temporal automática como não disponível, sem
botão ou mensagem que prometa o contrário.

## 9. Testes obrigatórios e sequência de implantação

1. Testes do domínio: classificação da Noiva, numeração longa, filme com números,
   edições distintas, primeiro/último episódio e próximo ainda indisponível.
2. Persistência: fechar navegador e reiniciar servidor preserva checkpoints;
   escrita concorrente/arquivo inválido não perde registros de outras séries.
3. Sessões: rejeitar eventos antigos, duplicatas, trocar série e seek para trás.
4. Playback versus infraestrutura: aquecimento do Drive, prefetch, Range de QA,
   listagem e probe de cauda não contam como assistido nem como conclusão.
5. Playlist: publicar um episódio novo durante a sessão não muda file_idx→task_id.
6. Migração: mover local→Drive mantém histórico e “Continuar” funcionando.
7. UI: sem progresso, com episódio em andamento, concluído, apagado/indisponível,
   posição desconhecida e posição explicitamente aproximada.
8. Se houver mudança no driver, compilar corretamente para o tablet, verificar
   arquitetura/hash e usar o deploy mestre. Não substituir binários por fora.
9. Teste físico: começar um episódio, avançar para o seguinte pelo controle da
   Samsung, parar no meio, guardar progresso, fechar painel e reiniciar os
   componentes apropriados. No retorno, deve aparecer o episódio correto.
10. Repetir com pausa longa, seek para trás, probes de abertura e cache já cheio;
    verificar que a telemetria não introduz pausas/erros USB.

Coordene reinícios com o outro agente: não reinicie o container durante ingestão
em `/tmp/serve` nem interrompa a produção dos episódios. Preserve montagens/rede
do compose runtime e o VOD do Drive. Não declare testes físicos concluídos se
apenas rodou mocks ou analisou logs sem observar a TV.

## 10. Relatório final

Informe arquivos alterados, testes, forma de persistência, como usar os botões e
qual driver foi instrumentado. Separe explicitamente:

- Marcador manual por episódio funcionando.
- Troca automática de episódio na TV detectada e fisicamente validada, ou pendente.
- Posição temporal desconhecida, aproximada ou comprovada.
- Retomada no início do episódio versus retomada automática no minuto.

O usuário deve saber onde parou sem receber uma falsa impressão de precisão.
