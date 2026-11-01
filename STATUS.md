# STATUS — USB-Stream-TV

Atualizado: 01/11/2026.

## Onde estamos

**Em produção e validado fisicamente na TV.**

| Item | Estado | Evidência |
| --- | --- | --- |
| Grade ao vivo | **40 canais** em produção | `config/live_favorites_export_tv.json` |
| VOD | 124 tarefas publicadas | allowlist da geração ativa |
| Geração ativa | `mixed-gen15-5573015e` | journal `.mixed_state.json` |
| Volume | 256 GiB (utilizável de ponta a ponta) | `getattr` = 274877906944 |
| Teste físico | aprovado pelo operador | SBT, Record, Globo SP + novos |
| Testes automatizados | verdes | ver "Verificação" abaixo |

## O que mudou desde a última atualização

### 1. O driver honrava 128 GiB fixos

Havia **dois** limites independentes, ambos medidos no tablet:

- `favorites/manifest.rs` validava a capacidade com `CapacityPolicy::default()`
  (128 GiB) e rejeitava qualquer pacote maior com `CapacityExceeded`.
- `vfs/ntfs_mft.rs` reportava `VOLUME_BYTES` = 128 GiB em `getattr`/`lookup`,
  então o kernel anunciava à TV um volume de 128 GiB mesmo com o BPB do NTFS
  declarando mais. Medido na gen11: imagem 206158430208 bytes, `total_sectors`
  402653183 (192 GiB), `getattr` 137438953472 (128 GiB).

Correção: a capacidade passa a vir do volume declarado pelo pacote
(`Manifest::from_json_with_capacity`, `manifest_file_ids_for_volume`) e o
tamanho reportado passa a vir do BPB (`NtfsTemplate::volume_bytes`). O default
de 128 GiB continua valendo para quem não informa volume, e há teste de
regressão (`from_json_with_capacity_accepts_a_package_above_the_128_gib_baseline`).

### 2. O switch não conseguia parar a geração anterior

A publicação falhava com `vps_previous_services_down`. Causa medida: o switch
roda como `sam`, mas os serviços da geração em curso pertencem a `root` —
`kill -0` retorna `Operation not permitted` e `cat state.json` retorna EACCES,
então as portas compartilhadas (18190/18191) ficavam presas e o proxy novo
morria com `invalid proxy configuration`.

Correção: o switch local é invocado como `sudo -n -E sh ...`. O `-E` não é
cosmético: o sudo limpa o ambiente por padrão e `SWITCH_ROOT`/`ENV_FILE`/
`EXPORT_CONFIG_FILE` cairiam nos defaults errados — a regressão que já custou
uma publicação.

### 3. Fragmentação estourava o record MFT

Com 39 Live + 137 VOD o template falhava em `MFT 252 runlist mismatch`. Um
único arquivo (2,68 GB) fragmentou em 144 runs e o runlist de 990 bytes não
cabe nos ~832 bytes úteis de um record MFT de 1 KiB. Correção: alocar do maior
para o menor. Medido: 144 → 35 runs, nenhum arquivo acima de 100.

## A grade (40 canais)

| Categoria | Canais |
| --- | --- |
| Abertos (8) | GLOBO SP, Record, SBT, TV Gazeta SP, BAND RJ, REDE TV, GLOBO RJ, TV CULTURA |
| Documentários (6) | ANIMAL PLANET, CURTA!, Discovery Channel, Discovery Turbo, H2, History |
| Filmes (5) | AMC, AXN, TNT, Telecine Fun, HBO |
| Notícias (7) | CNBC TIMES BRASIL, JOVEM PAN NEWS, RECORD NEWS, CNN BRASIL, BBC WORLD, BAND NEWS, GLOBO NEWS |
| Outros (3) | ESPN 2, SPORTV 2, X-SPORTS |
| Variedades (11) | ARTE1, Discovery H&H, E!, FUTURA, GLOOB, GLOOBINHO, GNT, HGTV, UNIVERSAL TV, WOOHOO, ZOOMOO |

**Rótulo de resolução.** O nome do arquivo diz a resolução **da fonte**, não a
da saída: 32 canais são `1080`, 8 são `720`. A saída do transpiler é sempre
1080p30, então chamar um 720p de 1080 seria vender upscale como nativo.

## Regras aplicadas

- Fonte 1080p nativa é o alvo; 720p só entra com **exceção explícita do
  operador**, registrada no export (`resolution_exception_authorized_by`).
- Verificação de decode segue `AGENTS.md` §2.4: sempre de arquivo salvo,
  `-threads 1`, saídas cruas, nunca `-f null`.
- O artefato `non-existing SPS 0 referenced in buffering period` é aceito
  apenas com a cadeia de prova completa (uma única mensagem, ordem
  AUD→SEI→SPS→PPS→IDR, 100% dos quadros), canal por canal.
- Nenhuma credencial é versionada. `.env` fica fora do Git; os exports usam
  placeholders `{XTREAM_UPSTREAM}`/`{XTREAM_USER}`/`{XTREAM_PASS}`.
- Conteúdo adulto é excluído da grade por decisão do operador.

## Verificação

```sh
cargo test -p fuse_ntfs --release            # 324 testes + teste de regressão
python3 -m pytest tests/test_mixed_generation_service.py   # 99
python3 -m pytest tests/test_mixed_bridge.py                # 22
bash tests/test_switch_mixed_generation_vps.sh              # suíte completa
bash tests/test_migrate_export.sh
bash scripts/validate_mixed_package.sh <staging>            # PACOTE VALIDADO
```

## Operação

```sh
# publicar a grade (a TV é interrompida; reconectar o pendrive depois)
CONFIRM_PUBLISH=1 INTERRUPT_TV=1 sh scripts/deploy_grade_40canais.sh

# desfazer o export
ROLLBACK=1 sh scripts/deploy_grade_40canais.sh

# voltar a geração
sh /opt/usb-stream-tv/scripts/switch_mixed_generation_vps.sh stop <sessão>
```

## Pendências conhecidas

- **Timer de preparação**: `usb-stream-tv-mixed-prepare.timer` roda a cada
  2 min com `--no-publish`. Ele prepara gerações sem publicar, o que faz a
  numeração avançar além das que o operador aprova. Não interrompe a TV.
- **`apply.state` preso em `in_progress`** quando uma publicação falha: o
  painel passa a recusar novos cliques até reconciliação.
- **Consumidores no cgroup do bridge**: um `systemctl stop` no bridge mata
  manager e proxy juntos (`KillMode=control-group`). Já causou uma queda de
  ~30 min.
