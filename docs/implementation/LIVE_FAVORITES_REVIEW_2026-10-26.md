# Live Favoritos — auditoria e continuação de26/10/2026

## Situação real

O OpenCode entregou biblioteca e testes dehost, sem ligação ao driver, sem
templateNTFS real, bootstrap ou transporte de mídia. Não havia funcionalidade
de seleção SBT/Record pronta para ativar no tablet.

O usuário autorizou avançar, corrigir e testar fisicamente. A restrição anterior
de não interromper uma sessão ocupada foi revogada por essa instrução; ainda
assim só será instalado um conjunto validado e com rollback, não a biblioteca
incompleta como se fosse um player operacional.

## Hardware e rollback preservado

- ADB acessível naVPS por127.0.0.1:25555; SM-T110/ARMv7.
- DriverLive instalado éRust:
  `1d18940401a8b6098c3ba9f4cbf0b766402e31cc38e79f1a7311f3c905778814`.
- stream_fetcher:
  `e2c203e0d809d12cdb07155c800fc80c76e9680c8485b55c4ffe59057644806d`.
- Cópias dos dois binários e dos scripts de modo/watchdog em
  `/home/sam/Code/tv-live-favoritos/scratch/hardware-baseline/` naVPS.
- Na inspeção o processo emuso era `fuse_direct`, comflagVOD1daf38f3.
- Gadget habilitado (`enable=1`), mas estadoUSB`DISCONNECTED`.
  Foi solicitado ao usuário ligarTV/conectar o cabo; não há teste físico aprovado.

## Defeitos reproduzidos e corrigidos

1. **ExtentMap:** ordem física dos extents podia descartar runlógico válido;
   mapa admitia arquivos4GB cobertos por apenas1MiB; metadados sobrepostos
   podiam ser aceitos. Agora há cobertura integral não-sparse, validação de
   VCN/LCN/overflow/reservas, rejeição transacional e tratamento de fragmentação.
2. **Detector:** varredura no fim do arquivo confirmava canal, candidatos
   cresciam sem limite e duplicatas renovavam evidência vencida. Agora há
   janela absoluta de abertura, limite32, TTL de evidência útil e debounce
   medido no commit real.
3. **Seleção:** aceitava188bytes estruturalmente inválidos e anunciava faixa
   de4GB. Agora retém payload limitado, valida headers/adaptaçãoTS e sequências,
   exige faixa alinhada a188 e respaldada pelos bytes realmente retidos.
4. **Fixtures:** tamanhos de arquivo, alocação e faixas de dados foram corrigidos;
   bytesTS têm campos de cabeçalho válidos. São fixtures de transporte, não
   streamsH264/AC3 decodificáveis nem prova de procedência de fonte.
5. **Digest:** o teste de borda que comparava a função consigo mesma foi
   substituído por hashes independentes calculados comPythonhashlib.

Um transporte que relabele bytes antigos continua sendo um problema de runtime:
a correção dos tipos/structs não prova a geração da conexão ou doFIFO.

## TemplateNTFS real criado nesta revisão

Novo gerador: `scripts/build_live_favorites_template.py`.

- `mkntfs` e libntfs-3g geram diretórios reais e alocam clusters sem escrever
  gigabytes de payload. Não usa volume/dispositivo do tablet.
- Árvore: `Favoritos/Abertos/SBT.trp` e `Record.trp`.
- Cada arquivo:4.000.000.000bytes lógicos e inicializados, runlists não-sparse.
- Volume:137.438.953.472bytes lógicos;71.811.072bytes físicos.
- Validação independente: `ntfsinfo`, `ntfsls`, índiceI30 ebitmap de clusters.
- EmpacotamentoSPARSE01/GZIP:193.181bytes.
- `favorites_package.json` liga hashes do arquivo compactado e do mapa.

Artefatos originais locais em `scratch/live-favorites-template-gf6hszqv/`;
cópia compactada naVPS em
`/home/sam/Code/tv-live-favoritos/artifacts/live-favorites-mvp/`.

SHA256 do pacotecompactado:
`583b837b02da06121bcc5927d29de184465cc8e18c47358d3d7fc6b63293a0d6`.

Nenhum dadoLive real ou token está nesse pacote. Ele não é sozinho um runtime
que transmiteSBT/Record: os dados virtuais ainda exigem backend integrado.

## Validação e continuação

- Favoritos corrigidos:70testes passaram.
- Integração decontratos:2testes passaram.
- GeradorNTFS:4testes passaram, incluindo imagem real e pacote sparse.
- A regressão completa do crate passou com `--test-threads=1` (exit0): os testes antigos
  `phase_b_tests` possuem seams globais e documentam execução serial. Uma
  execução emparalelo falhou; o teste isolado passou. Não alterar aengine
  antiga para mascarar interferência entre testes.

As correções e os artefatos foram enviados à cópia isolada daVPS. O agente
DeepSeek recebeu `TASK_RUNTIME.md` para completar bootstrap, transporte por
source_epoch, worker e integração Rust opt-in. Produção permanece protegida;
o operador revisa e publica quando o conjunto estiver operacional.

Pendências reais de código antes de física: prontidão PAT/PMT/IDR, transição
bootstrap→Live, isolamento do tráfego por epoch, ligação no roteador rawNTFS,
CLI/worker, compilaçãoARM e pacote seletivo de instalação/rollback.
Não chamar essas pendências de apenas "teste físico".
