# Ativação supervisionada de Live Favoritos no tablet

Este procedimento prepara um candidato independente. Só executar depois da
revisão de runtime/transporte, testes serializados e build ARM32 estático.
O usuário autorizou a interrupção para o teste físico com a TV livre.

## Limites do instalador existente

`deploy_tablet.sh` substitui todos os binários e termina reiniciando Single
Live. Não é o instalador deste ensaio. O ramo `switch_tv_mode.sh favorites`
atual também não serve: inicia o driver sem flags de manifesto/transporte e
usa o feeder global. O ensaio usa um caminho independente e preserva
`/system/xbin/fuse_ntfs`, `stream_fetcher`, scripts e templates instalados.

## Transporte antes da ativação

Iniciar o manager dedicado na VPS, limitado ao manifesto SBT/Record, em
`127.0.0.1:18189`, com o mesmo token privado distribuído ao tablet. Testar uma
conexão autenticada real Python→Rust e a fonte FFmpeg antes de desligar o USB.

O manager não deve escutar em `0.0.0.0`. O HMAC protege os frames, mas o
handshake `OPEN` contém o token: usar apenas túnel privado autenticado.

O teste no Android 4.2.2 confirmou que `adb reverse` falha com `error: closed`.
Usar o Chisel já instalado, por um segundo cliente dedicado. O servidor
existente escuta em `127.0.0.1:8888` na VPS e é publicado pelo caminho
`http://tv.smre.run.place/chisel`; não abrir portas públicas adicionais.

**O segundo executável não pode se chamar `chisel`.** O watchdog conta
`pidof chisel` e, ao encontrar dois, encerra ambos e recria sua conexão,
derrubando o ADB. O comando `tunnel-start` abaixo copia o binário para
`favorites_tun`, mantém PID e log próprios e usa o forward local
`127.0.0.1:18189:127.0.0.1:18189`. Não alterar o cliente reverso do watchdog,
seu serviço, seu lock ou sua configuração.

Distribuir a credencial já autorizada do servidor em `PKG/chisel.auth`, no
formato `usuario:senha`, fora de Git e com modo 600. O launcher a lê para a
variável de ambiente `AUTH` apenas no processo filho. Não usar `--auth` com a
senha em argumentos, não imprimir o arquivo e não ativar `set -x`. O suporte
a `AUTH` e a sintaxe de encaminhamento estão no
[código oficial do Chisel](https://github.com/jpillora/chisel/blob/master/main.go).
Confirmar também que a versão instalada aceita essa variável de ambiente.

Após iniciar o manager da VPS e preparar o diretório do pacote, executar como
root no tablet:

```sh
PKG=/data/local/tmp/live-favorites/mvp-20261026
/system/xbin/busybox sh "$PKG/activate_live_favorites_tablet.sh" \
  tunnel-start "$PKG" 18189
```

Esse comando ainda não altera USB. Ele espera a conexão do Chisel e envia
`PROBE` pelo loopback; exige a resposta exata `ERR source unavailable` do manager, que
rejeita o preâmbulo antes de abrir qualquer canal. Isso comprova o caminho
TCP tablet→manager, sem enviar token de mídia ou iniciar FFmpeg. A prova
autenticada Python→Rust e a validação de TS continuam necessárias antes da
ativação. O script recusa uma porta de loopback já ocupada e, em falha,
encerra somente o PID do túnel dedicado.

Para repetir a sondagem: `... tunnel-check "$PKG" 18189`. O log privado fica
em `PKG/activation/tunnel.log`; não publicar seu conteúdo bruto. O túnel fica
independente do supervisor USB, permitindo verificar a conectividade antes
do ensaio e preservar o caminho ADB durante o rollback.

## Pacote independente

Criar, por exemplo, `/data/local/tmp/live-favorites/mvp-20261026/`, sem
symlinks. Copiar para ele:

- `fuse_ntfs`: candidato ARM32 estático, revisado.
- `sparse_unpack`: ferramenta ARM32 estática.
- `favorites_template.sparse.gz`, `favorites_layout.json` e
  `favorites_package.json`: conjunto real de hashes correspondentes.
- `favorites_manifest.json`: manifesto canônico gerado com os IDs reais do
  catálogo, os caminhos exatos e os tamanhos do layout.
- `transport.token`: token aleatório exclusivo, fora de Git, chmod 600.
- `chisel.auth`: credencial existente do servidor Chisel, fora de Git,
  chmod 600; usada apenas pelo túnel dedicado.
- `activate_live_favorites_tablet.sh`: este supervisor.

Não transferir a imagem lógica de 128 GiB pela rede. O supervisor descompacta
o arquivo SPARSE01 em uma imagem nova por sessão e confirma tamanho lógico de
137438953472 bytes e uso físico máximo de 256 MiB. Cada sessão precisa de um
identificador novo; a imagem anterior não é sobrescrita.

Antes do ensaio, conferir espaço físico livre no tablet (pelo menos 100 MiB
mais a margem operacional desejada), hashes dos binários, ferramentas e os
três artefatos. O pacote original compactado tem SHA256
`583b837b02da06121bcc5927d29de184465cc8e18c47358d3d7fc6b63293a0d6`.

## Ativação

Executar como root no tablet, após testar o túnel, com sessão exclusiva:

```sh
PKG=/data/local/tmp/live-favorites/mvp-20261026
/system/xbin/busybox setsid /system/xbin/busybox sh \
  "$PKG/activate_live_favorites_tablet.sh" start "$PKG" \
  127.0.0.1:18189 physical-20261026-01 \
  > "$PKG/supervisor.log" 2>&1 &
```

O script primeiro monta e valida o candidato em um ponto separado. Até essa
etapa, o LUN antigo permanece conectado. Depois desliga o gadget, desanexa o
LUN, encerra os drivers antigos, monta a mídia de favoritos e reconecta em
somente leitura. Mantém `mode_switch.lock` com PID vivo e a flag favoritos;
o watchdog mantém ADB/Chisel/telemetria e envia `nocmd=1` ao controle Web.
Durante o ensaio, trocar canais pela lista da TV, não pelo controle Web.

Os caminhos `SBT.trp` e `Record.trp` são parte do pacote. O `patch_trp` legado
nunca deve rodar nessa imagem multicanal. A mudança da árvore NTFS para
Favoritos altera os nomes em relação ao modo antigo; o driver randomiza o
serial NTFS e o supervisor alterna `LIVETV1`/`LIVETV2`. Uma remontagem futura
deve preservar esses caminhos e o manifesto correspondente.

## Evidência física

Conferir `activation/result=active`, processo vivo e LUN apontando para
`mvp-20261026/mnt/tv_stream.img`. Na TV, sair do navegador antigo e reabrir a
mídia após a reconexão USB. Esperado:

```text
Favoritos/
  Abertos/
    SBT.trp
    Record.trp
```

Testar imagem e áudio SBT, Record, SBT novamente; deixar reprodução contínua,
pausar/retomar e voltar à lista sem reconexão USB. Coletar o log do candidato,
contadores de epoch/canal, telemetria e amostra `ffprobe`. Só a visualização da
árvore não comprova reprodução. Não declarar teste físico aprovado sem a
confirmação visual/sonora do usuário.

## Rollback

```sh
PKG=/data/local/tmp/live-favorites/mvp-20261026
/system/xbin/busybox sh "$PKG/activate_live_favorites_tablet.sh" rollback "$PKG"
/system/xbin/busybox sh "$PKG/activate_live_favorites_tablet.sh" tunnel-stop "$PKG"
```

O supervisor desconecta apenas o LUN que pertence ao ensaio, encerra o
candidato pelo PID e caminho próprios, desmonta seu ponto, libera o lock e
restaura o VOD anterior quando havia um, ou chama `switch_tv_mode.sh live` para restaurar Single Live; não
retoma automaticamente uma tarefa VOD anterior. `activation/result=restored`
indica que o comando de restauração terminou, não que a TV já decodificou.
Um erro de recuperação fica registrado como `rollback_failed` e exige
inspecionar gadget e logs. Se o supervisor sofreu SIGKILL, o mesmo comando
faz a limpeza do PID/lock próprios e recupera o LUN do ensaio.
`tunnel-stop` só encerra `favorites_tun` pelo seu PID/caminho registrados e
recusa encerrar a mídia enquanto o LUN do candidato ainda estiver ativo.
Nunca executar `killall chisel` para encerrar esse ensaio.

Não remover o diretório do pacote enquanto ele estiver ativo. As cópias de
rollback dos binários instalados também estão na VPS em
`/home/sam/Code/tv-live-favoritos/scratch/hardware-baseline/`.
