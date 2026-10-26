# Implementar canais favoritos como arquivos TRP no pendrive virtual da TV

## Objetivo e limites da tarefa

Este é o roteiro para o agente que IMPLEMENTARÁ a funcionalidade. A preparação
deste documento foi uma análise de código e consulta ao GPT-6 Astra; não foi
um deploy do modo multicanal. A seção de revisão abaixo registra a consulta.

O usuário quer escolher canais pelo controle físico da Samsung PL51F4000:

```text
Favoritos/
  Abertos/
    SBT.trp
    Record.trp
  Noticias/
    CNN Brasil.trp
```

As fontes são os favoritos Live já existentes no painel. Os nomes acima são
exemplos de apresentação; descobrir os IDs reais no catálogo. Não confundir
nome do arquivo com endereço de streaming nem fixar IDs do exemplo no código.

O arquivo é uma visão virtual de transmissão MPEG-TS, com extensão `.trp`.
Não baixar quatro gigabytes de cada canal nem gravar canais indefinidamente.
Começar com UM canal ativo, um feeder e ring de 128 MiB. Durante uma troca,
make-before-break pode abrir temporariamente a segunda fonte, com limite.

Esta implementação NÃO está autorizada a interromper a sessão atual da TV.
Preparar código e testes no host; publicar binários/montar novo volume somente
quando o usuário informar que terminou de assistir. Alterar favorito no painel
não pode desconectar USB, reiniciar Live nem modificar um NTFS já montado.

## 1. O que foi encontrado — leia antes de programar

### 1.1 Repositórios e ambientes

- Workspace principal: `/home/sam/Code/usb-stream-tv-rust`.
- Implementação C anterior: `/home/sam/Code/usb-stream-tv-prod`.
- VPS: `ssh oracle`, usuário `sam`, host `129.146.5.64`.
- Aplicação da VPS: `/opt/containers/apps/usb-stream-tv`.
- Container: `usb-stream-tv`; compose real: `compose.gdrive.runtime.yaml`.
- O servidor remoto tem diferenças em relação ao checkout. Comparar e aplicar
  patches seletivos; preservar rede, mounts, `.env`, VOD/Drive e histórico/UI.

Leia `AGENTS.md`, `docs/LIVE_EDGE_LOCK_AND_PROBE_ISOLATION.md` e os documentos
RC Live aplicáveis. Não tratar hashes antigos de releases como versão atual.

### 1.2 Caminho atual de Live

```text
provedor -> FFmpeg/VPS -> broadcast HTTP -> stream_fetcher/tablet -> FIFO
         -> ring Live/FUSE -> setores de tv_stream.img -> USB -> TV
```

`server.py:HUB.switch_channel` faz a troca em uma thread. O retorno `True`
significa que a solicitação foi aceita; NÃO prova que a fonte nova está pronta.
O primeiro chunk e o commit do novo processo acontecem depois. HTTP 200 do
endpoint atual não é confirmação de entrega do novo canal no tablet.

`FavoritesManager` persiste os IDs em `favorites.json`. Não usar
`series_favorites.json`. Os canais podem vir de `CH_MGR` ou do catálogo Live:
verificar ambos. IDs numéricos de Xtream não são automaticamente aliases de
`CH_MGR`. Resolver fonte no servidor com as credenciais já existentes.

### 1.3 Protótipo no C: aproveitar conceitos, não ativar sem correções

Arquivos em `/home/sam/Code/usb-stream-tv-prod/src/ntfs/`:

- `fuse_ntfs.c`: em `ntfs_serve_disk`, aproximadamente linhas 2240–2330, resolve
  setor para `file_idx`, offset local e fim de extent usando `virtual_file_lookup`.
- `catalog_favorites.h`: nove canais fixos, incluindo SBT e Record; caminhos
  `.ts`, tamanhos virtuais de 4.000.000.000 bytes e extents NTFS separados.
- O header cita `build_favorites_hierarchy_fs.py`; esse gerador não foi
  localizado nos caminhos inspecionados. Não inventar que ele já está disponível.

O detector C acumula cerca de 1 MiB, aceita continuidade com tolerância de
256 KiB e aplica debounce de 2 segundos. Há falhas a corrigir:

1. Pode somar leituras repetidas/sobrepostas como se fossem progresso.
2. Marca canal ativo e altera âncora antes de confirmar a fonte nova.
3. Usa o mesmo ring para arquivos diferentes, sem identidade de geração nos bytes.
4. Dispara uma thread por pedido HTTP.
5. O pedido antigo usa PIN na query de `/api/switch_channel`; não reutilizar
   query, fallback de credencial fixa nem expor segredo em logs.

`scripts/switch_tv_mode.sh` tem `favorites|channels` usando
`favorites_template.bin`. É um ponto de partida, não feature pronta:
usa inquiry fixo `CHANNELS`, chama `/system/xbin/fuse_ntfs` diretamente e não
repete visivelmente todos os cuidados anti-cache do modo Live convencional.
O daemon C tem randomização própria do serial; verificar o efeito completo
daemon+script, em vez de concluir pela ausência de uma chamada no shell.
Outro detalhe: o C infere catálogo/modo por substrings do nome do template
(`favorites`, `hierarchy`, `vod_hierarchy`). Um nome novo pode selecionar
silenciosamente o catálogo errado. Preferir modo/manifesto explícitos validados;
se o MVP conservar essa convenção, documentar e testar o nome exigido.

### 1.4 Armadilha importante no Rust

`crates/fuse_ntfs/src/vfs/catalog.rs` já contém diretórios e `NodeKind::Live`.
Porém são nós do FUSE do HOST. A TV lê setores do arquivo `tv_stream.img`.
Criar `Catalog.add_live_channel()` não cria esse arquivo dentro do disco USB.

`vfs/dispatcher.rs:serve_disk` usa `DiskRegion::Data(foff)` e `serve_stream`,
que escolhe `catalog.active_node()`. `vfs/ntfs_mft.rs:probe_layout` procura
apenas `TV AO VIVO.trp` ou `TV AO VIVO 2.tp`. Essa rota ainda precisa identificar
o arquivo de canal por seus extents reais no volume.

Não substituir C por Rust apenas para entregar esta feature. Primeiro descobrir
qual binário e template estão instalados e escolher uma única rota de implementação.

### 1.5 Limites reais encontrados nesta consulta

A API da VPS retornou **91 IDs favoritos**. Isso é a quantidade de favoritos
armazenados, ainda sujeita à validação de cada ID como Live. O arquivo
`/app/favorites_template.bin` não existe no container; isso não prova ausência
no tablet. ADB local e da VPS estavam sem dispositivos disponíveis.

O header C define `MAX_VIRTUAL_FILES=32` e `MAX_EXTENTS_PER_FILE=8`.
O Rust tem volume padrão de128 GiB e varredura limitada por `MFT_RECORDS=256`.
O protótipo C de4GB por arquivo, aplicado a91 entradas, precisaria de364GB
decimais somente para dados lógicos, além dos metadados: não cabe em128 GiB.

Portanto, NÃO exportar91 entradas copiando o protótipo ou truncando silenciosamente.
O MVP será explicitamente2 canais. Para exportar todos, definir e testar capacidade:
limite/estrutura de arquivos em memória, contagemMFT/diretórios, geometria do
volume e tamanho virtual de cada canal. Reduzir o tamanho virtual pode fazer a TV
chegar aEOF mais cedo numa sessão longa; aumentar volume depende de compatibilidade
com o firmware. Medir bitrate e horizonte de reprodução e documentar essa decisão.
O tamanho lógico não é RAM e não implica alocar os364GB fisicamente.

## 2. Fase obrigatória: inventário e experiência mínima

Comandos de leitura no host:

```sh
cd /home/sam/Code/usb-stream-tv-rust
git status --short
rg -n 'favorites\|channels|FAV_TMPL|FUSE_BIN' scripts/switch_tv_mode.sh
rg -n 'serve_disk|serve_stream|note_read' crates/fuse_ntfs/src/vfs/dispatcher.rs
rg -n 'virtual_file_lookup|g_is_favorites_mode|trigger_vps_channel_switch' \
  /home/sam/Code/usb-stream-tv-prod/src/ntfs/fuse_ntfs.c
adb devices -l
ssh oracle 'adb devices -l'
```

Se ADB estiver sem dispositivo, continuar testes de host; registrar que o
binário instalado e comportamento físico permanecem não confirmados.
Não executar `deploy_tablet.sh`, `switch_tv_mode.sh live/favorites`, reconnect,
killall ou reboot durante o inventário.

Quando já houver uma conexão ADB disponível, usando o serial correto:

```sh
adb -s "$TV_ADB_SERIAL" shell 'getprop ro.product.cpu.abi; uname -m'
adb -s "$TV_ADB_SERIAL" shell "su -c 'sha256sum /system/xbin/fuse_ntfs /system/xbin/stream_fetcher; ps | grep -E \"fuse_ntfs|stream_fetcher\"'"
```

Identificar fonte, hash, arquitetura, argumentos do processo, template em uso,
modo atual e tamanho físico/lógico do backing. Fazer backup de bytes instalados
em `scratch/` antes do deploy posterior. Não se apoiar somente no nome do binário.

O primeiro teste físico será somente `SBT.trp` e `Record.trp` na raiz; depois
uma pasta `Favoritos`; por último subpastas. Medir navegação, varredura inicial,
thumbnail, cabeçalho/cauda, início de play, troca rápida e retorno ao canal anterior.
Não afirmar que um limiar de leituras distingue intenção sem essa medição.

## 3. Contratos a fechar antes da implementação

### 3.1 Manifesto versionado, sem URLs privadas

Exemplo ILUSTRATIVO de contrato a implementar, não arquivo existente:

```json
{
  "schema_version": 1,
  "manifest_id": "sha256-do-conteudo-canonico",
  "generation": 7,
  "channels": [
    {"channel_id": "id-real-sbt", "folder": ["Favoritos", "Abertos"], "filename": "SBT.trp"},
    {"channel_id": "id-real-record", "folder": ["Favoritos", "Abertos"], "filename": "Record.trp"}
  ]
}
```

Gerar a partir do snapshot do `FAV_MGR`, resolvendo somente canais Live válidos.
Rejeitar VOD, torrent, URLs arbitrárias e favoritos que não possam ser resolvidos.
Não renomear arquivos automaticamente por semelhança perigosa: sanitizar nomes
NTFS, retirar `/`, `..`, caracteres de controle e nomes reservados. Resolver
colisão de nomes determinística com um sufixo baseado no ID. Limitar número
de canais e tamanho de cada campo, com erros claros antes de alterar o volume.
Tratar também colisões sem diferenciar maiúsculas/minúsculas (`SBT.trp` e
`sbt.trp`), ponto/espaço final e limites de nomes em UTF-16, não só bytes UTF-8.

Usar categoria do catálogo para as pastas; fallback `Favoritos/Outros` quando
não existir categoria. Não criar organização dependente de heurísticas por nome.
Se o mesmo canal aparecer em mais de uma pasta, cada entrada precisa ter mapa
de extents único e apontar para o mesmo ID lógico. No MVP evitar duplicatas.

O manifesto e seu template/mapa são IMUTÁVEIS enquanto montados. Novos favoritos
criam uma próxima geração; aplicação da geração exige remontagem planejada.
Não atualizar índices, runlists ou listas de canais no meio de uma sessão.

### 3.2 Pacote de NTFS e roteamento

Implementar um gerador reproduzível que produza juntos:

1. Metadados NTFS válidos: diretórios, MFT, índices, bitmap, `$DATA` e runlists.
2. Mapa `extent -> channel_id + offset_no_arquivo + tamanho_da_entrada`.
3. Manifesto, versão e hashes do pacote.
4. Artefato esparso/compactado e instruções de reconstrução.

O `manifest_id` deve identificar o conjunto compatível, vinculando hashes do
template e do mapa; rejeitar pacote misturado mesmo se cada arquivo tiver hash
individual válido. Publicar o conjunto validado como uma geração só.

Verificar que extents não se sobrepõem a metadados ou a outros arquivos.
Declarar intervalos meio-abertos `[inicio, fim)` e unidades (bytes/setores/clusters).
Leitura que cruza dois extents deve ser dividida; nunca misturar canal no mesmo
trecho. Leitura de diretório/metadados nunca solicita sintonia.

Não criar/commitar um arquivo binário cheio de 128 GiB. Não usar `dd` para gravar
quatro gigabytes de zeros por canal. Confirmar `stat` versus `du` e testar que
metadados de tamanho/initialized_size não fazem o host fabricar zeros em vez de
chegar aos setores de dados virtuais. Uma entrada NTFS sparse pode mudar esse
comportamento; comprovar a escolha no leitor NTFS e na TV.

### 3.3 Seleção e troca: estado explícito

```text
ACTIVE(A, epochA)
  -> CANDIDATE(B, leituras distintas)
  -> REQUESTED(B, request_seq, session, manifest)
  -> SOURCE_READY(B, epochB)
  -> TABLET_READY(B, epochB, primeiro PAT/PMT/IDR válido)
  -> ACTIVE(B, epochB)
```

`CANDIDATE` não muda âncora ativa nem ponteiro de continuidade. Exigir cadeia
de pelo menos três blocos distintos, sem somar bytes repetidos/sobrepostos.
Limiares de tempo/bytes devem ser configuração limitada e medidos fisicamente.
Probes de cabeçalho, fim, thumbnail e retorno esporádico a outro arquivo não
podem derrubar A. Um scan sequencial amplo ainda pode parecer play: documentar
esse limite e decidir o detector com as capturas reais, não só testes sintéticos.

Resolver o bootstrap: a TV pode desistir do arquivo antes de atingir o limiar.
Um arquivo candidato sem fonte pronta precisa ter uma estratégia de bytes
válidos e prazo compatível com o decodificador. Não mandar bytes de A como B.
Não bloquear FUSE esperando HTTP. Uma sequência neutra de bootstrap, se escolhida,
deve ser pequena, válida para o decoder, separada de dados de outro canal e
testada para não mascarar a seleção nem ficar presa no cache da TV.

Recomendação concreta do Astra para o primeiro experimento: prefixo TS finito
por arquivo com slate sintético "Selecionando SBT"/"Selecionando Record",
armazenado em disco com cache RAM global limitado. O prefixo já servido deve
permanecer imutável nos mesmos offsets durante a sessão. Definir fronteira de
transição alinhada a pacoteTS e início decodificável; normalizar PCR/PTS/DTS e
continuity counters entre prefixo e fonte. Concatenar dois TS arbitrários não
é uma transição válida. A leitura que confirma o limiar ainda pode receber o
prefixo; não depender de uma futura requisição para liberar a leitura atual.

No detector, contar união de intervalos e número de requisições originais;
fragmentos de um único pedido divididos entre extents não contam como pedidos
independentes. O primeiro canal também precisa de confirmação: índice0 não é ativo
implícito. Timeout/TTL monotônicos, evidência e estado com memória limitada.

Nenhum probe em B modifica `g_prev_Fend` de A. Não resetar ring/âncora no
primeiro pedido nem copiar o reset prematuro do protótipo C.

### 3.4 Contrato de sintonia assíncrona e identidade dos bytes

Implementar endpoints/worker novos ou adaptar existentes, mas deixar o contrato
documentado e testado. Nomes sugeridos, AINDA NÃO EXISTENTES:

- `POST /api/live-usb/select`: autenticação do tablet em header, sessão,
  manifesto, sequência monotônica e ID de canal permitido.
- `GET /api/live-usb/selection`: resultado pendente/pronto/falha da geração.
- Fonte de mídia associada ao mesmo `source_epoch` confirmado.

Autenticar também consulta de status e acesso à mídia dessa sessão, não somente
o POST de seleção. Validar sessão/manifesto/canal/epoch em todos os componentes.
Usar TLS ou transporte privado autenticado, com segredo carregado de arquivo
protegido em runtime. Header em HTTP público não protege o segredo em trânsito.

Uma resposta de aceitação deve dizer `pending`, nunca `ready` só porque
`switch_channel()` retornou `True`. Capturar conclusão do commit real no servidor.
Confirmar também a geração DOS BYTES no feeder: um ACK de controle sozinho não
retira bytes de A que ainda estão no socket/FIFO/HTTP/prebuffer.

Escolher e documentar protocolo concreto: conexão de mídia dedicada por geração,
ou framing de controle separado do payload TS decodificado pelo feeder. Não
inserir JSON/cabeçalho no MPEG-TS entregue à TV. Não depender de sleeps para
identificar a fronteira dos canais. O stream HTTP genérico atual não possui
necessariamente essa identidade; tratar como parte da implementação, não pressuposto.

Só efetivar B quando seus bytes corretos e bootstrap estiverem disponíveis.
Pedidos/ACKs de sessão antiga ou de B depois que o usuário pediu C são ignorados.
Fila limitada, coalescing do último candidato, uma sintonia em voo, timeout e
tentativas limitadas. Preservar o canal anterior se a fonte nova falhar, sem
exibir o canal anterior sob o nome B como se a troca tivesse funcionado.

Verificar a geração no servidor ANTES de publicar no HUB, e não só ao aceitar
ACK no tablet. Se outro consumidor estiver usando o broadcast global em sessão
incompatível, recusar a sintonia com motivo claro. O modo não pode alterar
silenciosamente a transmissão de VOD/TV de outra sessão.

O commit deve publicar junto canal/epoch/âncora/faixa válida do ring. Drenar
leituras antigas com prazo fora do mutex, não zerar128MiB sob lock. Cada resposta
FUSE fica vinculada à geração admitida: não trocar sua geração entre trechos.
Retenção/esgotamento do ring e reabertura A→B→A precisam de contrato testado,
porque o cache da TV pode reapresentar bytes antigos sem nova leituraUSB.
Se os dados de A já foram substituídos após o commit, voltar a A é nova
transação; não prometer rollback instantâneo dos bytes antigos.

Network/JSON/DNS/credenciais/hash/persistência ficam fora do handler FUSE e de
seus mutexes. Handler apenas consulta mapa imutável e atualiza estado curto.
Um worker gerencia pedidos. Não criar thread ilimitada por leitura.

## 4. Ordem de implementação para não misturar responsabilidades

1. **Inventário:** fechar binário ativo, código dono e baseline com hashes.
2. **Manifesto:** backend resolve favoritos Live, sanitiza, versiona; testes.
3. **NTFS:** gerador e parser/mapa offline; testar dois arquivos distintos.
4. **Sintonia:** pedido assíncrono e confirmação real de fonte/epoch no servidor.
5. **Feeder:** transportar/verificar geração e descartar backlog antigo corretamente.
6. **FUSE:** mapear LBA e integrar detector/worker, sem alterar Single Live padrão.
7. **Modo favoritos:** carregar pacote validado e aplicar anti-cache na montagem.
8. **Teste físico:** dois canais raiz, depois uma pasta, depois categorias.
9. **Integração discreta:** aproveitar favoritos existentes; sem novos botões por
   canal no painel. Mostrar apenas estado de catálogo aplicado/pendente se necessário.
10. **Rollout/rollback:** entregar comandos exatos para os artefatos efetivamente criados.

Sugestão de posse de arquivos quando usar agentes em paralelo: backend/manifesto,
geradorNTFS/testes de metadados, driver/feeder, e auditoria somente leitura.
Firmar interfaces antes da implementação dependente. Não deixar dois agentes
editarem `server.py` ao mesmo tempo.

Se C for o instalado, minimizar a alteração nessa base. Se Rust for o instalado,
estender sua geometria/mapa de disco e integração com o feeder, em vez de apenas
alterar `Catalog`. Não implementar as duas rotas simultaneamente.

## 5. Invariantes que não podem regredir

- H264 Live: `-tune zerolatency`, `bframes=0`, `slices=1`.
- Áudio: `-c:a ac3 -b:a 384k -ar 48000 -ac 2`.
- `-pcr_period 20`, GOP30, IDR/SPS/PPS por segundo, `repeat-headers=1`.
- Não aprovar passthrough sem conferir se a fonte cumpre essas propriedades.
- Feeder sem throttle; ring128 MiB; pacing do leitor até50ms e sem mutex no sono.
- Probes não mudam cadeia ativa; confirmação K>=3 distinta.
- Anti-cache na MONTAGEM: serial NTFS em0x48, identidade SCSI alternada e
  estratégia MFT compatível com nomes do catálogo. Não renomear `SBT.trp` para
  `TV AO VIVO` como efeito colateral do `patch_trp` antigo.
- A troca SBT→Record no MESMO volume não deve reenumerar USB; mudanças no catálogo
  e retorno de modo podem exigir remontagem planejada. São operações diferentes.
- Os nós de VOD/Drive, migração de episódios e progresso de séries ficam preservados.

## 6. Testes e critérios de aceite

### Host

- Favoritos vazios, IDs inválidos/removidos, IDs Xtream, fontes nãoLive.
- Colisões de nomes, Unicode, path traversal, categoria ausente, limites.
- Mesmo snapshot produz mesma relação ID/nome/extents/hash.
- Lookup de diretório e runlists do NTFS exportado, não só árvore FUSE do host.
- Extents fragmentados, limites, leituras cruzadas, MFT/bitmap/serial coerentes.
- Repetições e sobreposições não contam como blocos diferentes.
- Probes1–2blocos, cauda, interleaving, scan sequencial e candidata cancelada.
- ACK stale, fonte indisponível, fila cheia, timeout, novo catálogo em sessão antiga.
- Provar que bytes de A não são entregues como B; testar backlog socket/FIFO/ring.
- Compilação ARM32 estática e regressão da rota Single Live/VOD já existente.

No Rust, começar pelo comando REAL existente:

```sh
cargo test -p fuse_ntfs
```

Adicionar os testes específicos ao componente escolhido. Não inventar comandos
de testes/build que ainda não foram criados. Ao final registrar os comandos
reais usados, resultado e versão da toolchain. Não chamar produção para testar
erro/timeout de sintonia enquanto alguém assiste.

### TV física — todos pendentes nesta preparação

- Os dois arquivos aparecem; pasta `Favoritos` aparece; subpasta aparece.
- Abrir pastas, selecionar miniaturas e ler metadados não altera canal ativo.
- Play de SBT e Record apresenta imagem e AC3 válidos; medir clique→primeiro frame.
- Dez alternâncias, retorno ao canal anterior, seek/probe, autoplay entre arquivos.
- Fonte indisponível não provoca enxurrada de trocas/reconexões.
- Sem resets USB espontâneos, stalls SCSI nem regressão de continuidade.
- Medir `near_starve`, `nulls`, underrun e latência com baseline equivalente;
  qualquer exceção transitória deve ser explicada/quantificada, não escondida.
- ffprobe/inspeção do bitstream e capturas de logs sem URLs/credenciais.

## 7. Deploy e rollback

Não executar o `scripts/deploy_tablet.sh` atual sem revisar seu escopo: ele troca
vários binários/scripts, pode reconstruir128 GiB lógicos e reinicia Live.
Estender o script mestre com opção de componentes específicos e validações;
usar essa rota após a janela do usuário. Não substituir binário em execução.

Antes: backup de binário/template/mapa/scripts/config, hashes, estado do gadget,
serviços e mounts reais. Pacote candidato em caminho separado; validar antes
de marcar como atual. Instalar mapa/template e binário como conjunto compatível.
Plano de rollback restaura o conjunto anterior e o modo anterior, com verificações.

Não publicar `server.py` local inteiro sobre a VPS. Aplicar diff da feature à
versão remota real e conferir a configuração compose em uso. Backup privado
para `.env`; nenhum segredo em pacote Git ou documento.

## 8. Relatório final obrigatório do agente implementador

Entregar: arquivos alterados, contratos finais, comandos executados, testes,
artefatos/hashes, procedimentos seletivos de deploy e rollback e limitações.
Separar testes de host de testes físicos. Não declarar pronto só porque os
arquivos aparecem em um mount Linux; a TV precisa vê-los e trocar corretamente.

## 9. Consulta ao Astra

Consulta concluída com `ask-astra --deep`, modelo `gpt-6-astra`, raciocínio alto,
em sessão isolada e somente leitura. A [revisão completa do Astra](TV_AO_VIVO_FAVORITOS_ASTRA.md)
foi preservada como referência; este roteiro incorpora suas recomendações e
os limites adicionais de capacidade encontrados na VPS/header C.

Conclusão da revisão: a arquitetura é candidata à implementação, mas a
viabilidade exclusivamente pelo controle físico depende de provar na Samsung
que bootstrap e detector funcionam sem trocas durante navegação. Se leituras
de indexação forem indistinguíveis de play, ou a TV exigir todas as fontes
simultâneas, registrar esse bloqueio e preservar Single Live. Não remover essa
condição aumentando indefinidamente limiar, NULLs, buffers ou timeout.

Para iniciar o outro agente, fornecer apenas:

```text
Leia docs/prompts/TV_AO_VIVO_FAVORITOS_PASTAS.md e implemente em ordem.
Use TV_AO_VIVO_FAVORITOS_ASTRA.md como revisão de arquitetura.
Comece pelo inventário e MVP SBT/Record. Preserve o VOD em uso.
Não faça deploy/reinício do tablet ou servidor até a janela do usuário.
Entregue os testes de host e um pacote seletivo de implantação/rollback.
Informe separadamente o que depende da TV física.
```
