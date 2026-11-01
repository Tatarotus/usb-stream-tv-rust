# Integração de Mídia Mista: TV ao Vivo, Séries e Filmes no Mesmo Pendrive Virtual

**Data de Atualização**: 28/10/2026 (segunda rodada de revisão)
**Branch**: `feat/tv-live-grade-completa`
**Objetivo**: Live TV + séries + filmes na raiz do mesmo volume virtual NTFS,
com atualização automática após novos downloads, sem interromper reprodução.

> **Leia antes de ativar:** qualquer pacote gerado antes desta rodada
> (`scratch/mixed-staging/gen-2-*`, `mixed-gen7`, `mixed-gen8` já copiados para o
> tablet) está **inválido** e deve ser descartado/regenerado. Ver §6 (defeito de
> empacotamento corrigido).

---

## 1. Arquitetura

```text
/ (Volume Virtual NTFS 128 GiB)
├── Abertos/            SBT.trp, Record.trp        (MPEG-TS ao vivo)
├── Series/<Série>/     <Episódio>.mp4             (VOD agrupado por série)
└── Filmes/             <Filme>.mp4
```

- Nenhum vídeo é gravado na flash do tablet: o template NTFS é esparso e as
  mídias são servidas por extents virtuais sob demanda.
- Live: `tablet → túnel 18190 → manager Live (VPS)`, TS 1080p, 0 B-frames,
  AC-3 384k/48k/2ch, fatia única, PCR ≤ 20 ms.
- VOD: `tablet → túnel 18191 → proxy VOD (VPS) → servidor → Drive/disco`, faixas
  de até 4 MiB por requisição autenticada.

### 1.1 Identidade de conteúdo (cadeia de revisão)

O mesmo conteúdo precisa ser reconhecido de ponta a ponta, senão bytes antigos
podem ser servidos sob uma geração nova:

| Camada | Como a revisão é usada |
|---|---|
| `vod_revision.py` (raiz, no container) | `content_revision(task)` → `md5:<32hex>` \| `fs:<32hex>` \| `rev:<32hex>`; `None` = não verificável → falha fechada |
| `server.py` `/api/vod/status` | expõe `content_revision` por tarefa |
| `build_mixed_media_package.py` | grava a revisão no manifesto (JSON) e na allowlist; tarefa sem revisão é **rejeitada** |
| `run_mixed_vod_proxy.py` | exige `X-Vod-Revision` do cliente e igual à allowlist; repassa ao origin |
| `fuse_ntfs` (Rust) | valida allowlist (task+tamanho+revisão), envia o header e separa o cache por revisão |
| `server.py` `/api/internal/vod-range/<id>` | compara com a revisão autoritativa; divergência = **412**, identidade ausente = **503** |
| `vod_playback_cache.py` | revisão faz parte da chave (disco + cache secundário) |

A revisão **não** entra nos bytes canônicos do manifesto: `manifest_id` e o
formato lido pelo Rust permanecem idênticos.

---

## 2. Correções desta rodada

### 2.1. Publicação automática real (`scripts/mixed_generation_service.py`)
- `step()` deixa de apenas preparar: quando há geração pendente e o tablet está
  acessível, ele **transfere, verifica, ativa e confirma**.
- `promote_pending_generation(confirmation)` exige confirmação verificada do
  tablet (`verified=1`, geração e `manifest_id` iguais aos pendentes). Sem
  confirmação levanta `PublicationError` e **não** altera o estado ativo.
- Fases persistidas em `state["publication"]`: `prepared → incomplete →
  publishing → transferred → activating → active`, com `failed`/`unconfirmed`
  registrando a causa. `resume_publication()` revalida o tablet antes de
  reaplicar; uma ativação não confirmada é **revertida** (rollback) e nunca
  marcada como ativa.
- `staging_is_complete()`: `SHA256SUMS` obrigatório, não vazio, sem caminhos
  inseguros, cobrindo todos os artefatos e arquivos de runtime; qualquer hash
  divergente ou arquivo listado ausente bloqueia a publicação.
- `manifest_identity()` recalcula o `manifest_id` a partir dos bytes canônicos.

### 2.2. Detecção de reprodução estrita (fail-closed)
`is_playback_active()` agora exige o payload completo, tipado e **fresco**:
`active_vod`, `tv_on`, `tv_usb_state`, `in_standby`, `listeners`,
`stream_bitrate_kbps`, `tablet_online`, `server_time`.

- Campo ausente/inválido → `missing_telemetry:<campo>` / `invalid_telemetry:<campo>`.
- `server_time` defasado (> 60 s) ou no futuro → `stale_status_fail_closed` /
  `skewed_status_fail_closed`.
- `tablet_online=false` → `tablet_telemetry_offline_fail_closed`.
- `tv_usb_state` diferente de `DISCONNECTED` (inclusive valor desconhecido) bloqueia.
- Ociosidade só é declarada com **todos** os sinais presentes e livres.
- `in_standby=true` é informativo (o FFmpeg IPTV para sozinho após 90 s sem
  ouvintes, portanto não é reprodução); os sinais de consumo da TV (USB/power)
  continuam bloqueando.

### 2.3. Estado, concorrência e fingerprint
- `flock` exclusivo e reentrante por instância (`<state>.lock`): duas instâncias
  não preparam/promovem/publicam ao mesmo tempo (`StateLockedError`).
- Estado salvo de forma atômica: arquivo temporário único + `fsync` + `os.replace`
  + `fsync` do diretório; nenhum `.tmp` residual.
- Fingerprint inclui `id`, tamanho, título, `display_name`, **`series_id` e
  `series_title`** (ambos), temporada/episódio, `storage`, `remote_path`, a
  revisão confiável e o digest dos canais Live aprovados.
- Staging reconstruído do zero (`rmtree`) e nomeado `mixed-gen<N>-<hash8>`.

### 2.4. Integridade e limpeza (`scripts/activate_live_favorites_tablet.sh`)
- `SHA256SUMS` obrigatório: ausente/vazio aborta antes de qualquer artefato.
- O trap de limpeza é armado **antes** da cópia do binário, da expansão da
  imagem e da aquisição do lock. Falha em qualquer etapa remove apenas os
  artefatos desta tentativa (`template.<sessão>.bin`, `favorites_fuse`,
  `preflight.vod`), preserva o LUN anterior e a reprodução em curso.
- Sucesso só é reportado após `verify-active` confirmar geração + manifesto +
  LUN + processo candidato.

### 2.5. Preflight ponta a ponta
- `/probe` do proxy executa uma **leitura real** (`bytes=0-63`) do primeiro item
  da allowlist contra o origin e só responde 200 se o status, `Content-Range`,
  `Content-Length` e o corpo exato conferirem. Listener sem mídia → 503.
- Ação `preflight` no tablet: checksums + túnel Live + túnel VOD + **leitura
  Range real** por `tablet → túnel → proxy → servidor`, validando `206`,
  tamanho total (`Content-Range: bytes 0-63/<size>` com o tamanho da allowlist),
  `Content-Length` e bytes reais (box `ftyp` do MP4).
- Caminho Live: `scripts/probe_mixed_live_endpoint.py` faz o handshake `OPEN`
  real e autentica/decodifica frames, exigindo payload múltiplo de 188 bytes com
  sync `0x47` em cada pacote (executar na VPS contra 127.0.0.1:18190).

### 2.6. Roteiro executável na VPS
- `scripts/start_mixed_vod_proxy_vps.sh`: lê `TV_PIN` do `.env` protegido (nunca
  em argv), exige upstream HTTPS ou loopback HTTP, exige token ≥ 32 bytes,
  comprova `206` real do primeiro item da allowlist antes de iniciar o proxy e
  mantém o PIN apenas no ambiente do processo filho.
- Allowlist e token vivem na VPS em `$APP_DIR/mixed-vod/` (0600), copiados do
  pacote gerado; o tablet recebe a sua própria cópia dentro do pacote.

---

## 3. Uso do serviço de publicação

```bash
# Preparar + publicar (uma vez), a partir da máquina que fala com o tablet
python3 scripts/mixed_generation_service.py \
  --server-url http://127.0.0.1:8080 \
  --binaries-dir target/armv7-unknown-linux-gnueabihf/release \
  --private-dir scratch/mixed-private \
  --ssh-host oracle --adb-serial 127.0.0.1:25555 \
  --publish

# Daemon contínuo (monitora downloads, gera e publica quando a TV está ociosa)
python3 scripts/mixed_generation_service.py --daemon --poll-interval 60 ...
```

- `--private-dir` deve conter `transport.token` e `chisel.auth` (0600, nunca
  versionados). Sem esses arquivos a geração fica `incomplete` com
  `missing: transport.token,chisel.auth` e **não** é publicada.
- `--no-publish` apenas monta e valida o pacote (nada toca o tablet).
- `--dry-run` registra os comandos do tablet sem executá-los.
- Como a TV vê arquivos novos: uma nova geração só é publicada após a troca
  confirmada do LUN (reconexão do gadget USB). Durante reprodução o serviço
  permanece bloqueado, por desenho.

---

## 4. Roteiro operacional (VPS + tablet)

### 4.1. Pré-condições
1. Servidor atualizado (container) com `server_time`, `content_revision` e a
   validação de revisão no endpoint interno, e `vod_revision.py` na imagem.
   Sem isso o serviço bloqueia por telemetria incompleta (fail-closed) —
   comportamento esperado, não um defeito.
   *Estado observado em 28/10/2026 (somente leitura):* o container em produção
   ainda **não** tem `server_time` em `/app/server.py` nem `vod_revision.py`, e
   a porta 8080 **não** é publicada no host (`docker port usb-stream-tv` vazio).
   A publicação automática permanece bloqueada até o deploy do servidor.
2. TV livre (`/api/status`): `active_vod: null`, `tv_on: false`,
   `tv_usb_state: DISCONNECTED`, `tablet_online: true`.
3. Sessão `istanbullu-sync` (tmux) intocada.
4. Pacote íntegro em staging (`staging_is_complete` → `ok`).

### 4.2. VPS — proxy VOD (porta 18191)
```bash
# 1) Copiar allowlist + token do pacote para o caminho privado da VPS
install -d -m 700 /opt/containers/apps/usb-stream-tv/mixed-vod
install -m 600 <pacote>/vod_allowlist.json  /opt/containers/apps/usb-stream-tv/mixed-vod/
install -m 600 <pacote>/transport.token     /opt/containers/apps/usb-stream-tv/mixed-vod/

# 2) Escolher o upstream REALMENTE alcançável a partir do host da VPS
docker port usb-stream-tv          # hoje: vazio -> 8080 é interno ao container
curl -s -o /dev/null -w '%{http_code}\n' https://tv.smre.run.place/api/status

# 3) Iniciar (o script faz o preflight real de bytes antes de subir)
APP_DIR=/opt/containers/apps/usb-stream-tv \
PKG_DIR=/opt/containers/apps/usb-stream-tv/mixed-vod \
UPSTREAM=https://tv.smre.run.place PORT=18191 \
sh /opt/containers/apps/usb-stream-tv/scripts/start_mixed_vod_proxy_vps.sh
```
- `http://127.0.0.1:8080` só é válido se a porta for publicada no host; hoje não
  é (o runtime mantém 8080 interno e o Caddy publica o hostname HTTPS).
- **Não** use `http://172.20.0.5:8080`: HTTP sem loopback é recusado de propósito.
- `TV_PIN` vem do `.env` protegido; nunca aparece em argv, log ou neste roteiro.

### 4.3. VPS — manager Live (porta 18190) e prova de bytes
O manager roda a partir do checkout de operação (`/home/sam/Code/tv-live-favoritos`),
com `--manifest/--live-cache/--token-file` do pacote e `--port 18190`. Em seguida:
```bash
python3 scripts/probe_mixed_live_endpoint.py --port 18190 \
  --session <session> --manifest-id <manifest_id> --channel-id <channel_id> \
  --file-id <file_id> --generation <generation> --epoch <epoch> \
  --token-file /opt/containers/apps/usb-stream-tv/mixed-vod/transport.token
# esperado: {"ok": true, ... "payloads_are_ts": true}
```

### 4.4. Tablet — túneis, preflight e ativação
```bash
adb -s 127.0.0.1:25555 shell "su -c 'busybox sh <pkg>/activate_live_favorites_tablet.sh tunnel-start <pkg> 18190 18191'"
adb -s 127.0.0.1:25555 shell "su -c 'busybox sh <pkg>/activate_live_favorites_tablet.sh preflight   <pkg> 18190 18191'"
adb -s 127.0.0.1:25555 shell "su -c 'busybox sh <pkg>/activate_live_favorites_tablet.sh start <pkg> 127.0.0.1:18190 <session> 127.0.0.1:18191'"
adb -s 127.0.0.1:25555 shell "su -c 'busybox sh <pkg>/activate_live_favorites_tablet.sh verify-active <pkg> <generation> <manifest_id>'"
# esperado: verify verified=1 generation=<generation> manifest_id=<manifest_id>
```

### 4.5. Rollback
```bash
adb -s 127.0.0.1:25555 shell "su -c 'busybox sh <pkg>/activate_live_favorites_tablet.sh rollback <pkg>'"
```

### 4.6. Verificação física (exige a TV livre)
Estrutura `Abertos/`, `Series/`, `Filmes/`; zap `SBT.trp ↔ Record.trp` e retorno;
episódio de série com seek; um filme; após um download novo, reenumerar o pendrive
e confirmar que a nova geração é a ativa (`verify-active`).

---

## 5. Matriz de testes (host)

| Suíte | Escopo | Resultado |
|---|---|---|
| `tests/test_mixed_generation_service.py` | telemetria estrita, fingerprint, lock entre instâncias/threads, ciclo completo download→publicação confirmada (device falso pelo caminho real de comandos), rollback quando não confirmado, retomada pós-restart, journal corrompido falha fechado | 24 PASS (33 subtestes) |
| `tests/test_mixed_vod_proxy.py` | `/probe` com leitura real, revisão obrigatória (409), upstream 412/503, corpo curto, ranges | 10 PASS (36 subtestes) |
| `tests/test_mixed_media_end_to_end.py` | servidor → proxy → cliente, local e Drive, seek/EOF, conteúdo mutado com mesmo id/tamanho **não** é servido | 9 PASS |
| `tests/test_mixed_media_package.py` | agrupamento, allowlist com revisão, manifesto canônico imutável, arquivos privados 0600, rejeição sem revisão | 7 PASS |
| `tests/test_mixed_media_template.py` | layout/sizes + **round-trip byte-a-byte do `.sparse.gz`** e MFT completo | 3 PASS |
| `tests/test_probe_mixed_live_endpoint.py` | handshake OPEN, HMAC, TS 188/0x47, negação/erro | 7 PASS |
| `tests/test_activate_favorites_tunnel.sh` | SHA256SUMS obrigatório, limpeza precoce, preflight por bytes reais, verificação de ativação | 10/10 PASS |
| `tests/test_start_mixed_vod_proxy_vps.sh` | caminhos da VPS, PIN só no ambiente, upstream loopback, prova 206 | 5/5 PASS |
| `tests/test_server_gdrive.py` | cache Drive, revisão autoritativa (412/503), status expõe revisão | 44 PASS (28 subtestes) |
| `tests/test_vod_playback_cache.py` | invalidação por revisão (disco + secundário) | 23 PASS (4 subtestes) |
| Rust `cargo test -p fuse_ntfs --offline` | allowlist/revisão no cliente, extents mistos, ring buffer, retorno | 317 PASS + integração |
| Pacote real validado pelo binário Rust (host) | 122 arquivos, `manifest_id` confere, NTFS/VBR/catálogo OK | OK |

---

## 6. Defeito de empacotamento corrigido (crítico, pré-existente)

`build_live_favorites_template.pack_sparse()` intercalava `os.lseek(fd)` com um
arquivo **bufferizado**. O `read()` servia bytes de read-ahead do offset errado e
o `.sparse.gz` saía com zeros onde estavam os registros de MFT a partir do inode
112. Resultado: 78 dos 122 arquivos apontavam para registros inexistentes, o
template era rejeitado no candidato (`template invalid: MFT 112 has no FILE
signature`) e o pacote **nunca poderia ser ativado** — algo invisível a uma
auditoria que só compara `sha256sum`.

Correção: toda a caminhada de extents e a leitura usam o descritor bruto
(`os.pread`), em `pack_sparse` e em `verify_data_zero` (que tinha o mesmo vício e
por isso não detectava a corrupção). Teste de regressão: round-trip byte a byte
do `.sparse.gz` + presença de todos os registros de MFT.

Verificação: o pacote reconstruído passa no validador do binário Rust
(`122 arquivo(s)`, `manifesto=23307d13…`, NTFS/VBR/catálogo OK) e, com
`--mixed-vod-address` + `--mixed-vod-allowlist`, chega até a tentativa de mount
(que falha no host por falta de privilégio FUSE — esperado).

---

## 7. Pacote em staging

- Caminho: `scratch/mixed-staging/mixed-gen2-32abe7d7/`
- 122 arquivos (2 Live + 120 VOD), 0 rejeitados, 37,41 GiB livres, geração 2,
  `manifest_id = 23307d13dc420a58c479ea346fad355cb07dc3aa183c5422ff5cc746695ee50b`.
- Seus `SHA256SUMS` cobrem artefatos + `fuse_ntfs` + `sparse_unpack` +
  `activate_live_favorites_tablet.sh`. Os dois arquivos privados
  (`transport.token`, `chisel.auth`) **não** existem nesta máquina: o pacote
  permanece `incomplete` (`missing: transport.token, chisel.auth`) até que sejam
  fornecidos via `--private-dir`, exatamente como o portão de integridade exige.