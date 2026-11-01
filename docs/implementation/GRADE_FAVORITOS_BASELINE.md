# Baseline físico aprovado — grade de Favoritos Live

Data da apuração: 27/10/2026. Nenhuma credencial, URL privada ou token aparece
neste documento. Toda a evidência foi coletada em modo leitura (hash, listagem,
processo e estado do gadget); nada foi instalado, reiniciado ou desmontado.

## 1. O que o usuário aprovou fisicamente

A implementação de SBT e Record descrita em
`docs/implementation/LIVE_FAVORITES_ACTIVATION.md` foi testada na TV Samsung
PL51F4000 (MStar 2013) com a imagem NTFS multicanal no tablet SM-T110. O
comportamento confirmado pelo usuário foi:

1. abrir o ConnectShare e escolher um arquivo de canal;
2. o canal abre com preparação de aproximadamente 20 s;
3. Return e outro arquivo: o novo canal entra AO VIVO;
4. a navegação A→B→A foi obtida com esta receita.

Os 20 s são comportamento aceito e **não** foram otimizados nesta entrega.

## 2. Binário que produziu o teste físico

| Item | Valor |
| --- | --- |
| SHA-256 do FUSE em execução no ensaio | `82dfb420fe8c4fe138cdae3bcdb48a720b63c741a333db1afa7b4ddfaef54283` |
| Caminho no tablet | `/data/local/tmp/live-favorites/test-mvp/favorites_fuse` |
| Cópia idêntica no tablet | `/data/local/tmp/live-favorites/test-mvp/fuse_ntfs` |
| Cópia de trabalho | `scratch/favorites-runtime-review/fuse_ntfs-test2` |
| Build ARM32 release | `target/armv7-unknown-linux-gnueabihf/release/fuse_ntfs` (2.582.556 bytes, ELF32 ARM EABI5 estático, stripped) |
| Código-fonte | branch `feat/tv-live-grade-completa`, commit `5b6c4cd`; módulos `favorites/` introduzidos em `8e6fbc4` |
| Toolchain | `rustc 1.98.1`, target `armv7-unknown-linux-gnueabihf`, linker `arm-none-linux-gnueabihf-gcc` 15.2.1, `+crt-static` |

O binário de 26/10 é o artefato físico de referência. Recompilar o mesmo commit
hoje produz o mesmo tamanho, mas SHA-256 diferente no layout de link
(`bf5070…`), portanto **rollback usa a cópia preservada, não uma recompilação**.
As cópias de rollback ficam em
`/home/sam/Code/tv-live-favoritos/scratch/hardware-baseline/` na VPS e em
`/data/local/tmp/live-favorites/test-mvp/` no tablet.

## 3. Pacote físico aprovado

Pacote `/data/local/tmp/live-favorites/test-mvp/`:

| Arquivo | SHA-256 | Bytes |
| --- | --- | --- |
| `favorites_template.sparse.gz` | `583b837b02da06121bcc5927d29de184465cc8e18c47358d3d7fc6b63293a0d6` | 193.181 |
| `favorites_layout.json` | `5c898a9699f637f246a64601a52891502c87f4fb96e108fee5d4e237110ca9df` | 776 |
| `favorites_manifest.json` | `28c76a65196d7e592015256c786e04cf68811e3648cbae54e558931a7e49a2c9` | 759 |
| `favorites_package.json` | `abcf173f3de005480b867deb6b56700ed0f57540a1028165f86094b407f29fdd` | 471 |
| `sparse_unpack` | `7d40bb6054263df4b1ac2741560218ff8aca1dd941386af21014ef8dd1b5acf2` | 40.928 |
| `activate_live_favorites_tablet.sh` | `ae20cc9565d6b7ab7c2eeca40d2971871735c8e3f59d8a5b7c8e58807fdcd1b7` | 12.847 |

Conteúdo lógico do pacote aprovado:

```text
/Favoritos/Abertos/SBT.trp    (inode 66, LCN 4195432, 976563 clusters)
/Favoritos/Abertos/Record.trp (inode 67, LCN 5176088, 976563 clusters)
```

Volume lógico de 137.438.953.472 bytes, cluster 4096, uso físico do template
71.811.072 bytes. `SBT.trp` = ID 207198, `Record.trp` = ID 214640, ambos com
4.000.000.000 bytes lógicos e `initialized_size` igual ao tamanho lógico.

O bootstrap embutido no binário aprovado era o slate 720p30 de 2 s
(SHA-256 `7c5073f816580c811647688b3b4dd349e474d98e58773ae5f5448208d3cc7a20`,
1.480.876 bytes), com AC-3 384 kbit/s/48 kHz/estéreo, PCR ≤ 20 ms. Uma cópia
está em `scratch/grade-1080p/baseline/bootstrap-720p.ts`.

## 4. Modo de produção e rollback instalado

| Item | Valor |
| --- | --- |
| FUSE de produção instalado | `/system/xbin/fuse_ntfs` — `1d18940401a8b6098c3ba9f4cbf0b766402e31cc38e79f1a7311f3c905778814` |
| Feeder instalado | `/system/xbin/stream_fetcher` — `e2c203e0d809d12cdb07155c800fc80c76e9680c8485b55c4ffe59057644806d` |
| `switch_tv_mode.sh` | `64d5adb272c1ec980e4175ba092f4babc92b3bbe281e4d18b7b373646008b3c3` |
| `tv_watchdog.sh` | `96e48f5e5095aa39e0e9de06b3d04bd287e5330c7acbd392b71af6be1e867a8c` |
| LUN ativo na apuração | `/data/local/tmp/ntfs_lab/mnt/tv_stream.img` (Single Live, não o ensaio) |
| Inquiry SCSI | `LIVETV1` |
| Flag de favoritos | ausente (`/data/local/tmp/favorites_mode.flag` não existe) |
| Resultado do último ensaio | `restored` |

O modo de produção, o `stream_fetcher`, o watchdog e o VOD **não** foram
alterados. O pacote de ensaio permanece no tablet apenas como evidência.

## 5. O que é baseline preservado nesta entrega

Não foram alterados:

- detector de intenção, contagem K≥3, isolamento de probes e âncoras;
- ring ativo de 128 MiB, feeder sem throttle, pacing ≤ 50 ms;
- seleção por geração/epoch, transporte HMAC `TVFT`, ACK após dados;
- bootstrap imutável por arquivo, splice de CC/PCR/PTS/DTS;
- startup fail-closed de layout/pacote/manifesto/imagem;
- scripts de ativação/rollback do tablet e watchdog.

Foram alterados somente o que a grade completa e o 1080p exigem:

- limite de entradas (2 → 32) na validação e no manager;
- categorias na raiz do volume (sem `Favoritos/`);
- configuração de exportação única, com candidato escolhido e proveniência;
- perfil de saída 1080p30 e bootstrap 1080p30 correspondente;
- geradores de template/manifesto e testes/fixtures.

## 6. Limites desta apuração

- Não houve novo teste físico nesta etapa; o avaliador GPT Sol fará a ativação
  e o teste com o usuário.
- O hash do binário físico é a prova de identidade; a recompilação do mesmo
  commit não é bit-a-bit reproduzível com o toolchain atual.
- Nenhuma conclusão sobre áudio foi tomada apenas por decodificação de host:
  as medições de lacunas estão em `GRADE_FAVORITOS_RESULTADOS.md` e as
  perguntas abertas em `GRADE_FAVORITOS_REVISAO_SOL.md`.
