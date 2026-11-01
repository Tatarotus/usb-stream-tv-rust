# Ativação seletiva e rollback — grade de Favoritos (staging)

Procedimento para o GPT Sol executar **depois** da revisão. Nada abaixo foi
executado por este agente. Produção, container, VOD e a mídia USB atual não
foram alterados.

## 1. Pré-condições

- Janela autorizada pelo usuário; TV fora do ConnectShare.
- Tablet SM-T110 com root/ADB (túnel do watchdog em `127.0.0.1:25555`).
- Pacote de staging copiado para
  `/data/local/tmp/live-favorites/grade-20261027/` (o caminho precisa começar
  por `/data/local/tmp/live-favorites/`, conforme o supervisor).
- Binários e artefatos conferidos por `SHA256SUMS` (seção 4).
- Nenhum `patch_trp` deve rodar nesta imagem multicanal.
- Um manager de fonte dedicado por sessão, em loopback, com o mesmo token do
  pacote; nunca expor a porta publicamente.

O supervisor preserva `/system/xbin/fuse_ntfs`, `stream_fetcher`, scripts e
watchdog; ele roda um executável separado (`favorites_fuse`) com nome próprio
para não ser morto por `killall fuse_ntfs` durante a troca.

## 2. Manager na VPS (loopback)

Exemplo de linha de comando (sem credenciais em argumentos; credenciais vêm do
ambiente do processo, nunca do manifesto):

```sh
cd /home/sam/Code/tv-live-favorites
XTREAM_UPSTREAM=... XTREAM_USER=... XTREAM_PASS=... \
python3 scripts/run_live_favorites_manager.py \
  --manifest "$PKG/favorites_manifest.json" \
  --live-cache catalog_cache/live_streams.json \
  --export-config config/live_favorites_export.json \
  --token-file "$PRIVATE/transport.token" \
  --session grade-20261027-01 \
  --profile 1080p30 \
  --port 18189
```

- `--export-config` restringe a allowlist exata aos 16 candidatos escolhidos;
  não é mais necessário que cada candidato esteja nos favoritos pessoais.
- O processo valida `manifest_id`, `file_id`, bootstrap id e a correspondência
  exata manifesto ↔ configuração de exportação antes de abrir uma fonte.
- Uma fonte FFmpeg ativa por vez; geração/epoch monotônicos.

## 3. Ativação no tablet

Testar o túnel dedicado primeiro (não altera USB):

```sh
PKG=/data/local/tmp/live-favorites/grade-20261027
/system/xbin/busybox sh "$PKG/activate_live_favorites_tablet.sh" \
  tunnel-start "$PKG" 18189
```

Depois, com sessão exclusiva:

```sh
/system/xbin/busybox setsid /system/xbin/busybox sh \
  "$PKG/activate_live_favorites_tablet.sh" start "$PKG" \
  127.0.0.1:18189 grade-20261027-01 \
  > "$PKG/supervisor.log" 2>&1 &
```

O script valida o pacote, descompacta a imagem SPARSE01 em
`template.grade-20261027-01.bin`, monta o candidato em ponto separado, só então
desliga o gadget, troca o LUN, randomiza o serial NTFS (no binário) e alterna
`LIVETV1`/`LIVETV2`. Ele mantém o lock de modo e `nocmd=1` durante o ensaio.

## 4. Verificação antes de ligar a TV

```sh
cd "$PKG"
sha256sum -c SHA256SUMS
/system/xbin/busybox sh activate_live_favorites_tablet.sh status "$PKG"
```

Esperado: `result=active`, candidato vivo, LUN apontando para
`$PKG/mnt/tv_stream.img`, imagem com 137.438.953.472 bytes lógicos e uso físico
≤ 256 MiB.

Árvore esperada na raiz do pendrive (sem `Favoritos/`):

```text
Abertos/        Band.trp  Globo.trp  Record.trp  RedeTV.trp  SBT.trp  TV Cultura.trp
Noticias/       Band News.trp  BBC World.trp  CNN Brasil.trp  Globo News.trp
                Record News.trp  SBT News.trp
Filmes/         Telecine Action.trp
Documentarios/  Discovery Channel.trp  History.trp
Variedades/     Discovery HH.trp
```

## 5. Teste físico (usuário + GPT Sol)

1. Abrir o ConnectShare e confirmar as cinco pastas na raiz.
2. Selecionar `Abertos/SBT.trp`; medir preparação e confirmar imagem + áudio.
3. Return → `Abertos/Record.trp`; confirmar AO VIVO.
4. A→B→A dentro de uma categoria e entre categorias.
5. Navegar por miniaturas sem abrir reprodução e confirmar que o canal ativo
   não troca (K≥3).
6. Deixar um canal 1080p nativo por vários minutos, observando o trecho em que
   antes havia silêncio.
7. Coletar `$PKG/activation/fuse.log` (sem URLs/segredos) e a telemetria do
   manager.

Critérios de aceite: canal único por identidade, sem canal errado/bookmark
antigo, preparação próxima de 20 s, áudio AC-3 contínuo, sem reconexão USB
espontânea. Qualquer regressão material → rollback.

## 6. Rollback

```sh
PKG=/data/local/tmp/live-favorites/grade-20261027
/system/xbin/busybox sh "$PKG/activate_live_favorites_tablet.sh" rollback "$PKG"
/system/xbin/busybox sh "$PKG/activate_live_favorites_tablet.sh" tunnel-stop "$PKG"
```

O rollback encerra somente o candidato pelo PID/caminho próprios, desmonta o
ponto do ensaio, libera o lock e restaura o VOD anterior quando havia um; senão
chama `switch_tv_mode.sh live` e restaura o Single Live instalado. Se o
supervisor recebeu SIGKILL, o mesmo comando limpa PID/lock do ensaio.

`result=restored` significa que o comando de restauração terminou, não que a TV
já decodificou o modo anterior.

## 7. Rollback de binário (se necessário)

O binário físico aprovado de 26/10 está preservado em:

- tablet: `/data/local/tmp/live-favorites/test-mvp/favorites_fuse`
  (`82dfb420fe8c4fe138cdae3bcdb48a720b63c741a333db1afa7b4ddfaef54283`);
- VPS: `/home/sam/Code/tv-live-favoritos/scratch/hardware-baseline/`
  (`fuse_ntfs.installed` = produção `1d189404…`, `stream_fetcher.installed`).

Não substituir `/system/xbin/fuse_ntfs` pelo candidato da grade. O candidato é
opt-in e roda apenas via supervisor.

## 8. Conjunto indivisível (bloqueio de coordenação)

O perfil padrão do manager passou a ser `1080p30`; o bootstrap aprovado
fisicamente era 720p. Ativar **somente o manager** com o bootstrap/binário
antigos quebra a coerência. A ativação da grade deve trocar, na mesma janela e
com o mesmo `SHA256SUMS`:

1. `favorites_fuse` (binário ARM32 `70fe033b…`);
2. bootstrap `live_favorites_bootstrap.ts` 1920×1080 (asset embutido no binário,
   validado no build);
3. `run_live_favorites_manager.py` com `--profile 1080p30` e `--export-config`;
4. `favorites_manifest.json` + `favorites_layout.json` + `favorites_package.json`;
5. imagem SPARSE01 multicanal e o `manifest_id` correspondente.

Se qualquer item acima for de geração diferente, o manager deve recusar o
manifesto e a ativação deve ser abortada. O rollback para o conjunto anterior
exige: binário `82dfb420…`, bootstrap 720p
(`scratch/grade-1080p/baseline/bootstrap-720p.ts`), manager na revisão anterior
com perfil 720p30, imagem/LUN anteriores e manager reiniciado com o perfil
antigo. Sem essa troca conjunta, o rollback não é considerado válido.
