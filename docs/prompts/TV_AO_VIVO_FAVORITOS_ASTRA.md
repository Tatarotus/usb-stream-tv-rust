> Revisão solicitada ao GPT-6 Astra via `ask-astra --deep`, raciocínio alto, em 25/10/2026. Resposta de arquitetura recebida em modo somente leitura; nenhum patch de implementação foi aplicado. O [roteiro de execução](TV_AO_VIVO_FAVORITOS_PASTAS.md) acrescenta os limites reais descobertos depois: 91 favoritos, teto C de32 arquivos e volume de128 GiB.

# Plano de implementação: favoritos Live em pastas NTFS reais

Documento de planejamento; nenhuma implementação, chamada de troca ou implantação foi executada nesta consulta.

## 1. Objetivo, evidências e limites

Entregar arquivos como `Favoritos/Abertos/SBT.trp` e `Favoritos/Abertos/Record.trp` navegáveis pelo controle da Samsung PL51F4000, dentro do NTFS apresentado pelo dispositivo USB.

Estados usados neste documento:

- **Existente:** informado pelo contexto ou demonstrado pelos trechos fornecidos.
- **Proposto:** trabalho ainda necessário.
- **Dependente de hardware:** comportamento que exige medição na TV.

**Existente:** a TV lê um único dispositivo de blocos respaldado por `tv_stream.img`; não executa `LOOKUP` ou `READDIR` nos diretórios FUSE do host.

**Existente:** o C possui catálogo de arquivos com extents distintos e roteamento por setor, mas compartilha FIFO/ring e troca o índice ativo antes da confirmação da fonte.

**Existente:** no Rust, `dispatcher.rs::serve_disk` recebe `DiskRegion::Data(foff)` e chama `serve_stream`, que consulta `catalog.active_node()`. A identidade do arquivo NTFS não acompanha o endereço físico. `catalog.rs::note_read` altera diretamente o ativo e pode confirmar com uma única leitura grande.

**Existente:** há favoritos persistidos e infraestrutura de troca no servidor, incluindo preparação da nova fonte antes da publicação; isso não comprova identidade da fonte nos bytes recebidos pelo tablet.

**Não confirmado:** binário efetivamente instalado, compatibilidade dos IDs entre catálogo e `CH_MGR`, gerador do template de favoritos, comportamento de `no_reconnect` e aceitação desta arquitetura pela TV.

Restrições obrigatórias:

- Não interromper Live/VOD em uso para investigar ou testar.
- Não migrar C para Rust apenas para entregar favoritos.
- Manter um ring ativo de 128 MiB; não reservar um ring por canal.
- Manter uma fonte sustentada; preparação temporária de uma segunda fonte deve ser limitada e medida.
- Não executar rede, JSON, hash, build ou resolução de catálogo no handler FUSE.
- Não alterar metadados NTFS de um volume já apresentado à TV.
- Não prometer troca instantânea, reprodução simultânea ou identificação infalível de intenção por leituras.

## 2. Arquitetura mínima recomendada

Estender o caminho realmente instalado, reutilizando o roteamento por extents do C caso esse seja o binário em produção.

Fluxo proposto:

1. Exportador resolve favoritos Live para identidades estáveis.
2. Gerador produz manifesto, imagem/template NTFS e índice de extents consistentes.
3. Montagem congela esses artefatos numa sessão.
4. Roteador traduz cada intervalo de bytes do disco para metadados ou arquivo específico.
5. Detector observa leituras por arquivo e emite intenção, sem publicar canal ativo.
6. Worker serializa preparação e troca com o servidor.
7. Feeder verifica identidade/epoch dos dados antes de entregá-los ao ring.
8. Commit publica conjuntamente canal, epoch, âncora e intervalo válido.

O modo single Live existente continua disponível e deve ser o caminho de rollback.

Se o instalado for Rust, será necessário ampliar o layout NTFS e o roteamento bruto; criar mais nós em `Catalog` não resolve a apresentação à TV.

### Limitação física central

USB Mass Storage transporta leituras de setores, sem evento confiável de “usuário abriu SBT”. Miniaturas, indexação e reprodução podem produzir sequências indistinguíveis.

Um ring não contém transmissões atuais de vários canais. Arquivos inativos só podem oferecer bootstrap autorizado, conteúdo retido explicitamente identificado ou indisponibilidade; nunca o stream do canal ativo por conveniência.

Se a TV exigir vídeo atual de todos os favoritos durante a navegação, ou se seus probes forem indistinguíveis da reprodução, a seleção automática não atende às restrições. Nesse caso, registrar inviabilidade do fluxo exclusivamente pelo controle e manter single Live ou propor seleção explícita externa.

## 3. Tarefa 1 — inventário sem interferência

**Responsável:** integração.
**Ownership:** relatório de inventário; nenhuma alteração de runtime.
**Dependências:** nenhuma.

Comandos de leitura para o agente executar posteriormente:

```sh
git -C /home/sam/Code/usb-stream-tv-rust status --short
git -C /home/sam/Code/usb-stream-tv-prod status --short
rg --files /home/sam/Code/usb-stream-tv-prod /home/sam/Code/usb-stream-tv-rust -g AGENTS.md -g '*favorites*' -g '*ntfs*' -g '*deploy*' -g '*switch*'
rg -n 'virtual_file_lookup|g_cand_bytes|trigger_vps_channel_switch|no_reconnect|switch_channel|resolve_channel_url' /home/sam/Code/usb-stream-tv-prod /home/sam/Code/usb-stream-tv-rust
adb devices -l
ssh oracle 'docker ps --filter name=usb-stream-tv --format "{{.Names}} {{.Image}} {{.Status}}"'
ssh oracle 'docker inspect --format "{{.Image}}" usb-stream-tv'
```

Não imprimir ambientes, configuração Compose expandida, favoritos brutos ou logs completos: podem conter credenciais. Registrar somente campos necessários, com redação de segredos.

Se ADB estiver disponível, identificar processo, executável real via `/proc/<pid>/exe`, hash, ABI, versão, cmdline sanitizada, mounts, origem do backing e atributos atuais do gadget, apenas por leitura.

Não assumir `/system/xbin/fuse_ntfs` como executável ativo apenas porque o script o referencia. Não iniciar ADB no dispositivo, reconectar USB ou reiniciar processos para obter inventário.

No servidor, localizar os arquivos reais que implementam `FAV_MGR`, `CH_MGR`, `HUB`, autenticação e transporte. Examinar `compose.gdrive.runtime.yaml` preservando rede e mounts particulares.

Verificar também:

- Consumidores do broadcast global: uma troca pode afetar outras TVs ou VOD?
- Escopo real do lock de troca e cancelamento de operações antigas.
- Semântica de `no_reconnect` em todas as chamadas transitivas.
- Identidade e retenção dos bytes em cada FIFO, socket, buffer e fila.
- Caminho de build correspondente ao hash instalado.

**Aceite:** tabela “componente → arquivo real → versão/hash → função → alteração mínima”.
Se hardware estiver ausente, continuar testes host e marcar testes físicos como pendentes.

## 4. Tarefa 2 — manifesto imutável e resolução de favoritos

**Responsável:** catálogo/API.
**Ownership:** exportador e resolvedor nos arquivos encontrados na tarefa 1; schema de manifesto proposto.
**Dependências:** tarefa 1.

Manifesto proposto:

- `schema_version`: versão do formato.
- `manifest_id`: digest de representação canônica, calculado fora do FUSE.
- `layout_version`: versão do contrato de imagem/índice.
- `entries`: identidades, nomes, caminhos, tamanho lógico e referência ao bootstrap.
- `artifact_digests`: hashes do template, mapa de extents e bootstraps.

Cada entrada deve conter:

- `file_id` estável, independente da posição na lista.
- `channel_id` opaco, resolvível no servidor.
- Namespace e identificador do catálogo de origem, se necessários.
- `kind=live`.
- Caminho relativo em componentes validados.
- Tamanho lógico e identificação do bootstrap.

Separar `manifest_id`, identidade persistente do canal e `mount_session_id`, que muda em cada montagem.

Não incluir URL, token, senha, PIN ou credenciais de provedor. O servidor resolve a URL em runtime.

Validar explicitamente o mapeamento Xtream → canal aceito pelo `CH_MGR`. Se não existir, implementar adaptador no servidor; não tratar números semelhantes como identidades equivalentes.

Rejeitar entradas VOD, tipos desconhecidos, IDs ambíguos ou fontes não resolvíveis. Mostrar diagnóstico por entrada; não publicar silenciosamente catálogo incompleto.

Sanitização:

- Rejeitar caminhos absolutos, `..`, componentes vazios e separadores embutidos.
- Normalizar Unicode e limitar tamanho em unidades UTF-16 conforme o gerador.
- Tratar caracteres proibidos, controles, nomes reservados e pontos/espaços finais.
- Resolver colisões conforme comparação NTFS sem distinção de caixa, incluindo efeitos da normalização.
- Preservar extensão `.trp`; usar sufixo estável derivado da identidade quando necessário.
- Não usar posição ordinal como sufixo persistente.

Para o primeiro incremento, deduplicar o mesmo canal em uma localização canônica. Se forem necessários aliases em pastas diferentes, dar a cada arquivo identidade e extents próprios; mapear ambos ao mesmo canal sem compartilhar silenciosamente estado de reprodução.

Uma edição em `/api/favorites` prepara uma nova revisão. Aplicá-la ao USB exige janela explícita de remontagem; um clique de favorito nunca desconecta o gadget.

**Aceite:** schema validado, exportação determinística e testes de IDs, tipos, colisões e segredos.

## 5. Tarefa 3 — imagem NTFS real e identidade por setores

**Responsável:** NTFS.
**Ownership:** gerador localizado ou novo gerador documentado; template, mapa e integração de layout.
**Dependências:** tarefa 2.

Localizar `build_favorites_hierarchy_fs.py` em arquivos e histórico. O comentário no cabeçalho não prova que o script está disponível.

Se não for recuperado, construir gerador reproduzível a partir de ferramenta NTFS ou biblioteca verificável; documentar versão, entradas e validação independente. Não editar manualmente centenas de extents no cabeçalho.

O volume precisa conter:

- Diretórios reais com registros MFT e referências pai/filho.
- `$FILE_NAME`, `$STANDARD_INFORMATION` e `$DATA` coerentes.
- Índices `$I30`, incluindo `$INDEX_ROOT`, `$INDEX_ALLOCATION` e bitmap quando necessários.
- Runlists, tamanhos lógico/alocado/inicializado e bitmap de clusters consistentes.
- Fixups dos registros, estruturas de sistema e boot sector de backup válidos.
- Capacidade suficiente para metadados e todos os extents, sem sobreposição.

Os aproximadamente 4.000.000.000 bytes por arquivo do protótipo são uma entrada a validar, não um limite físico conhecido da TV. Dimensionar volume e aritmética com 64 bits.

**Distinção obrigatória:** a imagem no host pode ser esparsa para economizar armazenamento; os dados Live precisam ter clusters NTFS endereçáveis e exclusivos. Um run NTFS sparse representa zeros e pode ser resolvido pela TV sem emitir leitura dos setores esperados.

O mapa deve ser derivado das runlists efetivamente gravadas no template, e não de uma segunda estimativa de layout.

Contrato conceitual:

`route(disk_byte) → Metadata | File(file_id, file_offset, available) | Unmapped`

Para um extent iniciado no byte físico `p`, associado ao offset lógico `f`:

`file_offset = f + (disk_byte - p)`

Recortar cada trecho no menor limite entre pedido, extent, arquivo e volume. Uma leitura pode atravessar metadados, arquivo A e arquivo B; cada trecho conserva sua identidade.

No C, reutilizar `virtual_file_lookup`, auditando limites e overflow.
No Rust, substituir a classificação sem identidade `Data(foff)` por classificação equivalente à acima; o caminho bruto não pode escolher o arquivo consultando `active_node()`.

Metadados, padding, clusters não atribuídos e acessos fora do volume não alimentam o detector. Responder conforme o contrato de disco existente, sem transformar bytes estruturais em pacotes TS.

Validar a imagem com parser NTFS independente e, se necessário, montagem somente leitura de artefato offline. Nunca montar ou reparar o backing em uso.

**Aceite:** um leitor NTFS independente enumera as pastas e arquivos esperados e confirma os mesmos offsets do roteador.

## 6. Tarefa 4 — probes e máquina de intenção

**Responsável:** detector.
**Ownership:** detector do caminho instalado; fixtures de traces.
**Dependências:** tarefa 3.

Separar estados:

`IDLE → CANDIDATE → PREPARING → READY → COMMITTING → ACTIVE`

Falha/cancelamento antes do commit mantém o ativo anterior; sem ativo anterior, retorna a `IDLE` com motivo observável.

Manter separados:

- Arquivo/epoch atualmente publicados.
- Candidato e evidências de leitura.
- Requisição de preparação e sua geração.
- Âncora de reprodução de cada seleção efetivamente confirmada.

O handler só classifica, atualiza evidência limitada e tenta enfileirar evento. Fila cheia deve produzir descarte/coalescimento observável, nunca espera de rede.

Regra inicial para replay, sujeita à medição:

- Bloco lógico de evidência de 256 KiB.
- Pelo menos três blocos distintos.
- Pelo menos três requisições originais com avanço útil.
- Pelo menos 1 MiB de cobertura inédita na janela de início.
- Continuidade progressiva dentro de tolerância calibrada.
- Debounce inicial de 2 s entre commits.

Esses números não comprovam intenção; são parâmetros iniciais baseados no protótipo.

Contar união de intervalos, não soma de tamanhos. Releituras e sobreposições não aumentam evidência. Fragmentos produzidos pelo roteamento de uma única requisição não viram múltiplas requisições independentes.

Usar bitmap/intervalos de capacidade fixa e TTL monotônico. Se atingir capacidade, descartar o candidato ou reduzir a janela de forma explícita; não alocar memória sem limite.

Regras:

- Um ou dois blocos de probe não mudam ativo, âncora nem cadeia publicada.
- Probe isolado em outro arquivo não apaga uma candidatura válida.
- Leituras do ativo continuam sendo servidas e não confirmam retorno automático.
- Saltos para tail/EOF não somam evidência à sequência inicial.
- Candidaturas concorrentes usam política determinística e expiram.
- Uma leitura gigante isolada não confirma.
- O primeiro canal passa pelo mesmo protocolo; não existe ativo implícito por índice zero.

Se a TV iniciar sempre no meio do arquivo, só ampliar a janela de confirmação após registrar esse comportamento e distingui-lo dos probes.

A mesma requisição que completa o limiar pode receber bootstrap. Não depender de uma requisição futura para liberar a atual.

Observabilidade limitada, sem payload:

`timestamp, request_id, session, file_id, foff, len, classe, cobertura_nova, estado, request_generation, source_epoch, latência, motivo`

Capturar traces físicos somente na janela autorizada; usar traces sintéticos no host antes disso.

**Aceite:** replay demonstra decisões determinísticas, sem alteração de âncora por probes e sem crescimento ilimitado de estado.

## 7. Tarefa 5 — bootstrap sem dependência circular

**Responsável:** mídia/feeder.
**Ownership:** geração de bootstrap, cache limitado e contrato de offsets.
**Dependências:** tarefas 2 e 4.

Problema a resolver: a TV pode exigir TS decodificável para continuar lendo, enquanto o detector espera mais leituras para confirmar a seleção.

Proposta inicial: prefixo finito e estável por arquivo com slate sintético identificado como “Selecionando SBT”, “Selecionando Record” etc.; não copiar vídeo do canal A para preencher o arquivo B.

Armazenar os bootstraps fora do ring, preferencialmente em disco, com cache RAM global limitado. O armazenamento pode crescer com o catálogo; o orçamento de RAM não cresce por canal. Preparar/prefetchar fora do caminho crítico.

Cada bootstrap precisa:

- PAT/PMT e início decodificável com SPS/PPS/IDR.
- Perfil compatível com o Live existente.
- PCR/PTS/DTS e continuity counters coerentes.
- Tamanho suficiente para a evidência inicial mais margem medida de leitura antecipada.
- Mapeamento determinístico entre offset lógico e byte retornado.

O prefixo já servido permanece imutável durante a montagem. Não devolver posteriormente vídeo Live diferente nos mesmos offsets apenas porque chegou um ACK.

Definir fronteira de transição alinhada a pacote TS e a ponto decodificável. A passagem bootstrap → fonte nova exige remux/normalização de timestamps, counters e sinalização de descontinuidade compatível; concatenar dois TS arbitrários não basta.

Offsets de tail podem usar regiões sintéticas estáveis, se a TV precisar delas. Não fornecer o ring ativo nem inventar duração/índice incoerentes para satisfazer probes.

Se a preparação não terminar até a fronteira, aplicar política limitada e testada de indisponibilidade; não repetir indefinidamente bytes de prefixo em offsets novos nem preencher tudo com NULL.

Medir separadamente:

- Prazo de resposta de uma leitura USB/SCSI.
- Tempo que o player tolera bootstrap antes de desistir.
- Volume lido antecipadamente e comportamento de retries.
- Tempo de preparação da fonte, primeiro IDR e prebuffer.

O comentário Rust menciona abortos em 3,5–4 s e usa 3500 ms de deadline; isso não comprova tolerância da TV para troca de canal. Reservar margem em relação ao menor limite medido.

Sono FUSE máximo de 50 ms por espera, sempre fora de mutex, com orçamento total por requisição. O feeder continua consumindo a fonte sem throttle.

**Aceite:** bootstrap sozinho mantém o player no caminho necessário para confirmar a seleção, e a transição decodifica sem bytes de outro canal.

## 8. Tarefa 6 — controle autenticado e identidade da fonte

**Responsável:** API/worker.
**Ownership:** handlers encontrados na tarefa 1, worker do tablet e transporte do feeder.
**Dependências:** tarefas 2, 4 e 5.

Não reutilizar o GET com PIN em query string, fallback embarcado e thread detached por evento.

Propor operação autenticada por header, em TLS ou transporte privado autenticado, com credencial obtida de armazenamento protegido em runtime.

Campos de controle:

- `mount_session_id`, `manifest_id`.
- `file_id`, `channel_id`.
- `request_id` idempotente e geração monotônica de intenção.
- Operação de preparar, confirmar ou cancelar.

O servidor valida autenticação, posse da sessão, associação ao manifesto, tipo Live, identidade resolvida e autorização para usar o HUB.

Se houver outro consumidor incompatível no broadcast global, rejeitar a troca com motivo claro. A feature não pode retunar VOD/TV de outra sessão. Uma fila exclusiva não resolve sozinha compartilhamento global de fonte.

Worker único ou número fixo de workers; uma preparação pendente por sessão; coalescer para a intenção mais recente; rate-limit com relógio monotônico. Duplicatas retornam resultado idempotente.

Preparação deve manter a fonte anterior até que a nova esteja decodificável e o tablet possa efetuar a transição. Reutilizar o make-before-break existente, ampliando apenas o contrato necessário.

ACK de controle não comprova a origem dos bytes. Introduzir um dos contratos verificáveis:

- Transporte interno enquadrado, com frames contendo sessão, canal, epoch e sequência; ou
- Conexão de dados exclusiva por epoch, com handshake autenticado e sem reutilização de buffers entre conexões.

Preferir o menor contrato que o transporte instalado suporte. Headers internos são removidos antes da saída USB: a TV continua recebendo TS puro.

Não inserir um marcador ambíguo em FIFO TS bruto e presumir que todos os buffers anteriores foram drenados. Mapear e corrigir cada fronteira com dados potencialmente antigos.

`source_epoch` identifica uma instância da fonte; reconexão da mesma emissora pode exigir epoch nova. `request_generation` identifica intenção; não são intercambiáveis.

READY só é válido quando:

- Sessão, manifesto, canal e requisição correspondem à intenção vigente.
- O feeder recebeu dados daquela epoch pelo contrato de dados.
- Há início decodificável e prebuffer dentro do orçamento fixo.

ACK tardio, sessão antiga ou preparação cancelada não publica ativo. O servidor também verifica geração antes de publicar no HUB; ignorar ACK no tablet não desfaz uma troca indevida no servidor.

Perda de ACK exige consulta idempotente de estado, não repetição cega. Registrar falha e manter a fonte anterior antes do commit.

**Aceite:** testes com ACK e dados reordenados demonstram que confirmação de controle nunca autoriza bytes sem procedência.

## 9. Tarefa 7 — commit de epoch e proteção do ring

**Responsável:** engine.
**Ownership:** ring, feeder e serviço de leitura do binário instalado.
**Dependências:** tarefa 6.

Invariante obrigatório:

`bytes de mídia servidos a B pertencem a B/epoch publicada ou ao bootstrap sintético autorizado de B`

Nunca usar `active_channel` global como fallback para um pedido identificado como outro arquivo.

Procedimento proposto:

1. Manter A publicado enquanto B prepara em staging pequeno, com limite global fixo.
2. Validar READY e dados de B sem apagar ou resetar A.
3. Fechar a admissão de novas leituras de mídia da geração antiga; metadados continuam disponíveis.
4. Drenar leituras e cópias já admitidas, com deadline; esperar fora do mutex.
5. Suspender apenas a publicação do produtor antigo no ring, sem throttlar o consumo da fonte.
6. Inicializar nova faixa válida e inserir bootstrap de transição/prebuffer validado de B.
7. Publicar sob sincronização curta a tupla completa de sessão, arquivo, epoch, âncora, faixa válida e geração.
8. Liberar leitores de B e encerrar a preparação antiga conforme o protocolo de commit.

Memória antiga pode permanecer fisicamente no ring, mas nunca fica acessível pela faixa válida da nova epoch. Não zerar 128 MiB sob lock.

Cada requisição FUSE prende uma geração de serviço até completar sua resposta; trocar geração entre trechos de uma mesma resposta é proibido. Se a leitura atravessar arquivos, cada trecho mantém também sua identidade própria.

A implementação em ARMv7 precisa sincronizar corretamente campos de 64 bits e a tupla completa; múltiplos atomics independentes não substituem publicação coerente. Definir ordem de locks e ownership de escrita.

Se a drenagem ou preparação falhar antes do commit, abortar B e reabrir A. Se houver falha depois de reutilizar o ring, não prometer restauração instantânea dos bytes antigos: reaquisição de A é outra transação e pode apresentar interrupção.

Leituras tardias de A após commit de B não recebem B. Podem obter bootstrap autorizado/indisponibilidade ou originar nova candidatura segundo a mesma regra.

Retrys dentro da retenção precisam retornar bytes estáveis para a mesma seleção/offset. Quando dados já saíram do ring, não remapear silenciosamente o offset para o presente.

**Aceite:** nenhuma combinação de wraparound, cancelamento, reconexão ou corrida produz bytes de A numa resposta de B.

## 10. Tarefa 8 — anti-cache, montagem e limites do arquivo

**Responsável:** integração do tablet.
**Ownership:** `scripts/switch_tv_mode.sh` e geração de metadados.
**Dependências:** tarefas 3 e 7.

Aplicar o contrato anti-cache apenas na entrada/reinício de modo, dentro da janela autorizada:

- Alternância de nomes MFT adaptada ao catálogo.
- Atualização coerente dos índices e atributos de nome.
- Serial NTFS no offset 0x48 do VBR e cópia de backup correspondente.
- Alternância de inquiry conforme o mecanismo LIVETV1/2 validado.

Preservar IDs lógicos mesmo quando o nome visível alternar.

Não mudar serial, MFT ou inquiry a cada seleção de canal. A troca comum não deve reenumerar USB.

O inquiry `CHANNELS` fixo do protótipo não demonstra equivalência ao anti-cache single Live.

Cache interno da TV não pode ser invalidado arbitrariamente pelo servidor. Extents exclusivos impedem alias entre arquivos, mas não garantem frescor ao reabrir o mesmo arquivo em outra epoch.

Testar A → B → A, saída e retorno ao player, retry e retomada do meio. Se a TV reutilizar payload antigo sem novas leituras, documentar o limite; não prometer frescor que o dispositivo não consegue impor sem remontagem.

Arquivo lógico finito chega ao EOF: estimar duração aproximada como `8 × tamanho / bitrate` e medir comportamento real. Não presumir Live infinito, seek arbitrário ou crescimento visível sem atualizar metadados.

**Aceite:** montagem inicial reconhece catálogo completo; trocas não reconectam USB; limites de cache e EOF ficam demonstrados.

## 11. Tarefa 9 — testes host antes do hardware

**Responsável:** validação.
**Ownership:** testes de roteamento, detector, transporte e concorrência; fixtures determinísticas.
**Dependências:** tarefas correspondentes implementadas.

Executar primeiro os testes existentes pelo comando documentado no repositório. Para o workspace Rust, `cargo test --workspace` é candidato a confirmar pelo inventário; não inventar nomes de pacotes ou targets C.

Matriz mínima:

| Área | Casos | Aceite |
|---|---|---|
| Manifesto | IDs reorderados, duplicatas, casefold, Unicode, traversal, VOD | Resultado estável ou rejeição explícita |
| NTFS | Diretórios, MFT, índices, bitmap, runlists, backup VBR | Parser independente confirma estrutura |
| Setores | Leitura desalinhada, fim de extent, metadata→A→B, EOF, overflow | Identidade e limites corretos em cada trecho |
| Detector | 1–2 blocos, repetição, sobreposição, salto ao tail, leitura gigante | Nenhuma troca indevida |
| Detector | K blocos, cobertura inédita, interleaving e expiração | Uma intenção válida, sem reset por probe isolado |
| Bootstrap | Primeiro arquivo sem ativo, prefixo/cache, transição de TS | Sem dependência circular e início decodificável |
| Fonte | ACK antes dos dados, dados antigos após ACK, canal incorreto | Nenhuma publicação incorreta |
| Epoch | A→B→C, cancelamento, stale ACK, reconexão, sessão antiga | Só a intenção vigente pode publicar |
| Ring | Wraparound, produtores concorrentes, leitura durante commit | Sem mistura de canais/epochs ou deadlock |
| Falha | Timeout, fila cheia, servidor indisponível, ACK perdido | Estado limitado e recuperação determinística |
| Recursos | Catálogo crescente, navegação repetida, upstream lento | RAM limitada e nenhuma fila ilimitada |
| Regressão | Single Live e VOD | Contratos anteriores preservados |

Usar fontes TS sintéticas distinguíveis por conteúdo, não apenas PID, e conferir cada resposta por arquivo/epoch. Injetar falhas em todas as etapas de preparação e commit.

Não substituir os testes antigos de `note_read` por novas expectativas sem separar “detector emitiu intenção” de “fonte foi publicada”.

Validar perfil existente: H.264 com `bframes=0`, `zerolatency`, `slices=1`, GOP 30/IDR de aproximadamente 1 s conforme frame rate efetivo, `repeat-headers=1`, AC3 384k estéreo 48 kHz e `pcr_period=20`.

**Aceite:** relatório de resultados, limites de RAM, latências e falhas conhecidas; nenhum teste físico considerado aprovado por simulação.

## 12. Tarefa 10 — pacote mínimo e rollback

**Responsável:** release.
**Ownership:** `deploy_tablet.sh`, seleção de binário no script de modo e inventário de artefatos.
**Dependências:** testes host aprovados.

Estender o deploy com escopo explícito e opção de preparar sem ativar. Os nomes das flags serão definidos na implementação; não presumir que já existem.

O pacote deve conter somente:

- Binário necessário, compatível com ABI e kernel identificados.
- Worker/feeder alterado, se exigido pelo contrato de epoch.
- Script de modo com seleção explícita do executável.
- Manifesto, template, mapa e bootstraps compatíveis entre si.
- Alteração mínima de servidor, se necessária.

Evitar substituir todos os binários ou reiniciar Live como efeito colateral de instalar artefatos.

Antes de ativar, registrar hashes, permissões, caminhos e versões do conjunto anterior; manter backup verificável. Preservar Compose, mounts e rede efetivos.

Validar compatibilidade de schema/protocolo antes de tocar no modo ativo. Preferir servidor compatível com clientes antigos e feature desativada por padrão.

Não executar o deploy genérico atual para “testar”. Preparar artefatos em caminho inativo; ativar apenas após início confirmado da janela sem reprodução em uso.

Rollback:

1. Encerrar a sessão de favoritos e cancelar preparações pendentes.
2. Desativar o modo experimental de maneira controlada.
3. Restaurar o conjunto compatível anterior por hashes e permissões.
4. Reativar single Live pelo fluxo conhecido, com seu anti-cache.
5. Conferir executável real, backing, saúde do feeder e reprodução.

Rollback pode reenumerar USB e interromper reprodução; é uma operação da janela de manutenção, nunca uma ação silenciosa durante uso.

**Aceite:** pacote seletivo revisável e ensaio host de restauração; teste físico de rollback ainda obrigatório.

## 13. Tarefa 11 — matriz física e decisão de viabilidade

**Responsável:** validação com operador.
**Dependências:** pacote e rollback prontos; janela explícita sem TV/VOD em uso.

Começar com dois canais: SBT e Record em pastas reais. Só ampliar após aprovação.

| Ensaio | Evidência necessária | Aceite |
|---|---|---|
| Inserção/montagem | Enumeração e navegação pelo controle | Pastas e arquivos corretos, sem corrupção |
| Navegação sem abrir | Trace de setores e eventos de troca | Zero troca durante miniaturas/indexação |
| Primeiro canal | Timeline de bootstrap, intenção, READY e commit | Reprodução sem ativo implícito |
| SBT→Record | Identidade de payload e vídeo observado | Nenhum frame/áudio de SBT servido como Record |
| A→B→A | Cache, offsets iniciais e nova epoch | Comportamento de reabertura conhecido |
| Trocas rápidas | Cancelamentos e ACKs atrasados | Apenas intenção vigente publica |
| Tail/seek/retomada | Classificação e âncora | Sem troca acidental nem remapeamento silencioso |
| Fonte indisponível | Falha antes/depois do commit | Motivo claro e recuperação previsível |
| `no_reconnect` | Estado UDC/gadget, eventos USB e tela da TV | Nenhuma desconexão/reenumeração na troca |
| Sessão prolongada | Wraparound, retries e EOF | Sem mistura e limites documentados |
| Favoritos alterados | Revisão pendente e volume montado | Nenhuma mutação de NTFS ativo |
| Rollback | Hashes e reprodução single Live | Restauração comprovada |

Provar `no_reconnect` por inspeção do caminho completo e observação física: o nome do parâmetro não é evidência suficiente. Verificar ausência de escrita em UDC, backing e atributos que reiniciem o gadget.

Registrar p50/p95/máximo de preparação e troca, máximo de silêncio por leitura, comportamento do player, RAM e contagem de fontes simultâneas.

Critérios de bloqueio:

- Qualquer vazamento de conteúdo entre canais.
- Troca causada apenas por navegação no conjunto de testes.
- Probes indistinguíveis de reprodução sem solução dentro das restrições.
- Bootstrap insuficiente para a TV continuar lendo.
- Timeout, corrupção NTFS ou necessidade de reenumerar em toda troca.
- Alteração de outro consumidor do HUB.
- Memória ou upstreams crescendo proporcionalmente ao número de canais ativos no catálogo.

Ao bloquear, preservar evidências, restaurar single Live e registrar exatamente qual requisito físico não foi atendido. Não mascarar falha aumentando indefinidamente NULLs, limiar, buffers ou tempo de espera.

## 14. Ordem de execução e entrega final

Executar na ordem: inventário → manifesto → NTFS/roteamento → detector/bootstrap → controle de fonte → commit/ring → testes host → pacote seletivo → janela física.

Antes de cada alteração, localizar o arquivo real e confirmar instruções do repositório. Os responsáveis acima representam ownership de módulos, não autorização para executar agentes ou mudanças em paralelo.

O agente executor deve entregar:

1. Inventário do instalado e decisão fundamentada entre estender C ou Rust.
2. Schema, resolução de IDs e artefatos NTFS reproduzíveis.
3. Contratos de intenção, transporte, epoch e commit.
4. Testes host com resultados e traces sanitizados.
5. Pacote seletivo com hashes e rollback.
6. Matriz física preenchida, incluindo limitações e pendências.

Somente declarar a feature viável na Samsung após passar pela matriz física; catálogo FUSE, compilação bem-sucedida e testes host isolados não comprovam navegação e troca pelo controle.

