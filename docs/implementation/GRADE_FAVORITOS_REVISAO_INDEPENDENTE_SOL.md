# Revisão independente — grade de favoritos

> Atualização: a revisão abaixo registra o estado inicial. Uma nova rodada de correções e medições está em andamento; não use os números antigos como resultado final.

Revisão parcial em 27/10/2026, enquanto a implementação e as medições ainda estavam em andamento. Resultado: **não aprovada para ativação**. Os achados abaixo são sobre os validadores e a evidência disponível; não demonstram, por si só, que a reprodução física vai falhar.

## Bloqueios

1. `scripts/probe_live_favorites_output.py` aceita PCR de até 50 ms, mas as instruções do projeto exigem no máximo 20 ms. O teste usa apenas `FFmpegProvider` e não inclui o `PcrBounder` da bridge real. Medir os pacotes após o pipeline efetivamente usado pelo manager, inclusive o filtro, e verificar o limite real. Não confundir `-pcr_period 20` com uma medição bem-sucedida.
2. O mesmo verificador pode retornar `output_ok` com FFmpeg encerrado com erro e áudio de 128 kbit/s. Reproduzido com dados sintéticos: PCR 49 ms, `returncode=1` e AC-3 128 kbit/s foram aceitos. Reprovar erros de processo e validar todos os requisitos de saída, incluindo AC-3 384 kbit/s e intervalos efetivos de IDR, não apenas a mediana.
3. `record_stability` em `scripts/build_live_favorites_export.py` trata cobertura ou lacunas de áudio ausentes como aceitáveis e não verifica adequadamente a segunda janela. Uma segunda janela com `probe_error`, duração de 1 s e largura de 640 foi considerada estável. Exigir dados presentes, válidos e independentes nas duas janelas, resolução 1920×1080 durante a captura, duração/cobertura medidas e continuidade de áudio; falhas ou dados ausentes devem resultar em pendência.
4. A captura das fontes usa `-c copy` e o `ffprobe` não registra seu stderr como resultado de decodificação. Portanto, a ausência de erros na captura não prova decodificação limpa. Acrescentar verificação de decodificação efetiva, guardar contagens sanitizadas e não rotular como livre de erros sem essa evidência.
5. `validate_plan` não confere se todos os favoritos Live do snapshot estão representados: uma cópia do plano sem a identidade Globo foi aceita. A guarda `used_channels` também não registra os IDs já vistos. Validar cobertura e duplicatas explicitamente, com testes negativos; manter pendências visíveis quando uma fonte não for aprovada.
6. O novo perfil padrão do manager é 1080p; a versão previamente testada fisicamente usa bootstrap 720p. A ativação precisa coordenar manager, perfil, bootstrap, binário e manifesto, e documentar rollback para o conjunto anterior. Não atualizar apenas o manager em produção.

## Estado da evidência no momento da revisão

- O snapshot relatado contém 19 favoritos Live, agrupados em 16 identidades. A medição de finalistas e o código permaneciam em andamento.
- Existiam 3 JSONs de segunda janela, nenhum JSON em `scratch/grade-1080p/output-evidence` e o arquivo `config/live_favorites_export.json` ainda não existia. A segunda janela de Band News tinha cerca de 225 s de mídia em 300 s solicitados, apesar de `candidate_ok`; não constitui aprovação final.
- Os relatórios continham marcadores não preenchidos. O relatório de resultados chama o perfil de validado apesar de registrar PCR máximo de 20,3 ms em 1080p e 20,55 ms em 720p antes do filtro. É necessário medir depois da bridge e relatar o valor final, com o método e eventual precisão da medição.
- Há relatos de lacunas de áudio nas fontes; não é possível afirmar que a causa dos trechos sem áudio observados na TV esteja resolvida. Considerar também lacunas abaixo de 2 s e silêncio sem descontinuidade de PTS na avaliação física.
- A documentação registra cobertura mínima de 97%, diferente da recomendação atribuída ao Astra de 99,9%. A alteração de critério e seus efeitos devem ser explicitados, sem tratar uma captura incompleta como prova de reprodução contínua.
- O relatório identifica sete identidades sem 1080p nativo na triagem. Deixar cada uma pendente com a melhor alternativa e evidência, sem afirmar que o pacote parcial contém todos os favoritos nem declarar 720p como 1080p nativo.

## Validação e entrega solicitadas ao executor

1. Corrigir os bloqueios; criar testes de regressão para PCR acima de 20 ms, falha do FFmpeg, áudio incorreto, segunda janela ausente/inválida, favorito omitido e candidato duplicado.
2. Reexecutar as medições necessárias com os validadores corrigidos. Manter a preferência por RJ quando a fonte satisfizer os critérios; comparar alternativas equivalentes de SP quando necessário.
3. Entregar matriz por identidade com fonte escolhida e alternativas, resolução nativa, região, duas janelas, cobertura, interrupções de áudio, resultado da decodificação, saída após bridge e motivo de pendências. Não inferir estabilidade longa de duas amostras de cinco minutos.
4. Gerar e validar o pacote em staging, identificando se é parcial ou completo; completar os relatórios e hashes. Não ativar, reiniciar serviços ou trocar a mídia USB durante esta revisão.
5. Encaminhar o resultado para nova revisão Sol e posterior teste físico com o usuário: abertura, Return, troca A→B→A e observação dos cortes de áudio.

Verificação local desta revisão: 13 testes de exportação e 19 testes da bridge, PCR e source manager passaram. Eles não cobrem os casos negativos reproduzidos acima e não substituem o teste físico. Nenhuma alteração de produção foi realizada pela revisão.

## Continuação: correções e teste parcial

- Os validadores agora rejeitam ausência de medições, captura ou decode com erro, janelas menores que 300 s e saída incompatível. A exportação reavalia os dados de saída e decode, em vez de confiar apenas nos rótulos de aprovação.
- A geração 4 antiga, em `config/live_favorites_export.json`, não é a entrega revisada. As medições atuais estão em `scratch/grade-1080p/decode-revisao-sol` e `output-revisao-sol`; os exports de revisão são `export-revisao-sol.json` e `export-revisao-sol-gen6.json` nesse diretório.
- A geração 5 contém SBT RJ e Record Rio; a geração 6 acrescenta Discovery Channel. Ambas são parciais. O arquivo ausente na cópia `delivery-20261027/package` foi encontrado em `staging-20261027-gen4`, cuja estrutura foi validada; essa validação não resolve as evidências inconsistentes da geração 4.
- Na nova checagem, SBT, Record e Discovery Channel passaram. History teve erro de decodificação; Telecine Action e Discovery H&H não satisfizeram a cobertura de vídeo medida. Um teste de 60 s não prova ausência de defeitos fora da amostra.
- Testes automatizados: 65 Python e 118 Rust passaram; três testes Rust dependentes de artefatos ou captura real foram ignorados. A integração Rust passou em quatro casos.
- A geração 5 foi validada offline e ativada apenas para teste físico, com manager e túnel próprios. O usuário confirmou imagem e som e a troca SBT → Record → SBT ao vivo, sem cortes de áudio observados na amostra. O manager registrou as três sessões e dados em todas elas; essa observação curta não garante estabilidade por horas. A geração 6 ainda não foi ativada.
- O usuário prefere manter pendentes os canais sem 1080p nativo e buscar alternativas. Uma busca somente no catálogo identificou 12 opções adicionais para triagem, incluindo afiliadas Globo RJ e variantes nacionais; `scratch/grade-1080p/candidatos-adicionais-sol.json` é somente um plano de sondagem, não uma lista aprovada. Afiliadas regionais podem ter programação local diferente da capital.
- Triagem adicional curta: Band News 206085, SBT News 3030729 e Globo SP 10001 entregaram 1080p; não são aprovadas sem duas janelas longas, decode e saída validados. A sondagem longa foi interrompida quando o usuário relatou travamentos no SBT.
- Na sessão física, o manager registrou múltiplos encerramentos do FFmpeg seguidos de reinício. Após a pausa das sondagens paralelas, o mesmo processo continuou ativo por mais de dois minutos. Isso sugere, mas não comprova, conflito de conexões com o provedor; os erros registrados eram genéricos. Não há telemetria suficiente para excluir fonte, rede ou outras causas.
- A implementação local passou a solicitar reconexão HTTP ao FFmpeg. Em teste sintético, duas reconexões foram tratadas pelo mesmo processo e a saída passou pelo filtro PCR com AC-3; a alteração ainda não foi ativada nem validada com a fonte real. Reconexão não garante ausência de perda ou silêncio na fonte.
- Posteriormente, com autorização para interromper o canal de teste, a reconexão foi ativada somente no manager isolado da geração 5. O FFmpeg manteve a sessão do SBT aberta durante uma sondagem simultânea de 60 s da Band News. A confirmação visual e teste mais longo continuam necessários. Ao substituir o manager, o encerramento gracioso excedeu 10 s; o processo dedicado e seu FFmpeg foram encerrados para concluir a troca. A causa do atraso de encerramento permanece indeterminada.
