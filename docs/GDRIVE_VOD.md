# VOD no Google Drive

Os novos vídeos podem usar o Google Drive como armazenamento permanente.
Os VODs locais existentes continuam funcionando; habilitar o Drive não os migra.

## Operação

1. Autorize o remote com rclone, fora do repositório.
2. Configure no `.env` privado:

   ```dotenv
   VOD_GDRIVE_REMOTE=gdrive:USB-Stream-TV/VOD
   RCLONE_CONFIG_DIR=/caminho/privado/rclone
   VOD_GDRIVE_CACHE_MB=128
   VOD_CURRENT_CACHE_GB=6
   VOD_MIN_FREE_GB=8
   ```

3. Suba preservando os overrides existentes da instalação:

   ```sh
   docker compose -f compose.yaml -f compose.gdrive.yaml up -d --build
   ```

O diretório privado do rclone é montado em `/run/rclone`. A escrita permite
renovar o token OAuth; o arquivo não é copiado para a imagem. Nenhuma porta nova
é exposta: o servidor HTTP auxiliar do rclone escuta apenas no loopback interno.

O servidor prepara um vídeo por vez e ainda precisa de espaço temporário para
origem e convertido. O Drive não elimina esse requisito de transcodificação.
Uma reserva de disco interrompe os subprocessos de preparação se o espaço livre
cair abaixo do piso configurado. Arquivos temporários de tarefas com erro podem
exigir exclusão pelo painel para recuperar espaço.

Após preparar o MP4, o servidor envia ao Drive e confirma tamanho e MD5. Só
depois de persistir o catálogo de forma atômica libera a origem local. Uma falha
de upload preserva o MP4 para nova tentativa, sem baixar/transcodificar de novo.
Uploads interrompidos são retomados na inicialização. Catálogo, templates FAT32
e arquivos de controle continuam locais.

## Cache do vídeo atual

Há um cache descartável de apenas um vídeo, com orçamento padrão de 6 GiB e piso
de 8 GiB livres. O começo e a cauda são carregados antes do play; um único worker
traz o restante em segundo plano. Ao trocar de vídeo ou voltar ao modo ao vivo,
o cache é descartado. Leituras de probes de outros episódios não trocam o cache.

Quando o vídeo acabou de ser preparado e não há outro VOD selecionado, seu
arquivo verificado pode alimentar o cache por hardlink, sem duplicar seus bytes
no mesmo filesystem. A biblioteca permanente continua no Drive.

Leituras ainda não presentes no cache consultam o Drive. Existe também cache LRU
em RAM de 128 MiB, com no máximo duas buscas de 4 MiB simultâneas. Esse orçamento
de RAM não inclui os buffers transitórios dessas buscas e das respostas HTTP.
Se não houver espaço para cache em disco, a reprodução usa o caminho remoto.
Um salto para um trecho ainda não carregado mantém a latência do Drive.

O controle web exibe separadamente a quota do Google Drive e o disco da VPS.
A quota é consultada em segundo plano, cacheada por 60 s e invalidada após upload
ou exclusão. Quota indisponível é exibida como desconhecida, sem inventar zero.

O servidor auxiliar mantém os metadados por cinco minutos e recebe SIGHUP após
publicações/exclusões. Isso torna novos arquivos visíveis sem interromper as
leituras de outro vídeo nem consultar novamente o diretório a cada bloco.

## Medição inicial: VPS Oracle → Google Drive

Em 25/10/2026 foram feitas 13 leituras (84 MiB) de dois vídeos existentes,
com ranges corretos e checksums idênticos nas repetições. Rclone 1.60.1, sem
cache em disco nem read-ahead em RAM. A medição usa a VPS; não inclui Wi-Fi,
tablet, USB ou decodificação física da TV.

| Bloco | Tempo total observado |
| --- | --- |
| 1 MiB, 4 amostras | 1,668–2,080 s |
| 4 MiB, 4 amostras | 1,616–2,246 s |
| 8 MiB, 4 amostras | 2,036–2,152 s |
| 32 MiB, 1 amostra | 11,946 s (22,47 Mb/s) |

O primeiro byte de mídia levou 0,623–1,913 s. Repetições nessa medição não eram
cache hits de disco. A validação do cache otimizado deve medir também os hits
locais e os saltos frios. Esses resultados sustentam o teste físico, sem garantir
que toda leitura futura do Drive terá a mesma duração.

## Verificação

```sh
python3 -m unittest discover -s tests -p 'test_vod_*.py'
python3 -m unittest discover -s tests -p 'test_server_*.py'
```

Validar em produção upload com MD5, leitura HTTP 206 de início/meio/fim,
compatibilidade do bitstream com ffprobe e reprodução/seek no hardware.
O endpoint autenticado `POST /api/vod/warm` aceita `{"task_id":"<id>"}` e o
mesmo PIN da reprodução para preparar o cache sem enviar comandos ao tablet.
Ele recusa aquecer outro título quando já existe uma seleção VOD ativa/pendente.
Para voltar a salvar novos vídeos localmente, esvazie `VOD_GDRIVE_REMOTE` e
recrie o serviço. Títulos já remotos precisam do Drive habilitado para tocar;
o rollback de código/configuração não deve apagar a biblioteca remota.

## Primeiro VOD validado no Drive

Bluey S01E01 (7min18s), 103.826.306 bytes, foi preparado pelo catálogo e enviado
ao Drive. O MD5 confirmado na origem e no objeto remoto é
`f3f77df4ecf70ba371812fe8006ad539`. A origem de preparação foi removida e uma única
cópia descartável ficou no cache. As respostas HTTP 206 de início, meio e fim
coincidiram byte a byte com leituras independentes da cópia do Google Drive.

Com o cache do vídeo atual, as leituras completas na VPS levaram:

| Trecho | Tempo total (três amostras) |
| --- | --- |
| Início, 8 MiB | 133,90 / 45,25 / 37,48 ms |
| Meio, 4 MiB | 26,51 / 29,83 / 11,86 ms |
| Fim, 1 MiB | 3,87 / 3,93 / 6,89 ms |

A verificação independente sem cache de dados variou de 2,98 a 10,02 s nessa
rodada, com consulta de diretório a cada bloco. Essa variação motivou o cache
de metadados de cinco minutos com invalidação por sinal. Cache local e remoto
são caminhos distintos; números de loopback não representam latência até a TV.

ffprobe confirmou H.264 High, 1280×720, yuv420p, áudio AC-3 estéreo a 48 kHz e
duração de 438,01 s. O MP4 VOD copiado da origem contém B-frames
(`has_b_frames=2`); esta tarefa não alterou a codificação nem o pipeline TS Live.
A reprodução e os saltos na TV precisam ser conferidos no teste físico.
