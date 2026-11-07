# Publicação bloqueada após download — episódio 256

Diagnóstico em 2026-11-07, após clique real no controle web.

## Duas falhas medidas

1. O bind-mount `/app/scripts/mixed_bridge.py` carregava uma revisão antiga sem `--operation-id`, embora o daemon no host já tivesse a revisão compatível. O processo do cliente terminava antes de criar o pedido na ponte; a operação ficava em `preparing`. A imagem nova não substitui o conteúdo de um bind-mount antigo.
2. Depois de sincronizar a ponte, a preparação falhou em `invalid NTFS component`: a pasta `Cyberpunk Mercenários` continha caracteres fora do conjunto ASCII aceito pelo catálogo do tablet. O erro de build era capturado e o estado ainda apontava à geração anterior, resultando na mensagem menos específica `task_not_in_generation` para o episódio 256, embora o problema fosse outra série.

## Correções e verificação

- Arquivo da ponte em produção atualizado com a revisão já rastreada, preservando inode e backup; `client --help` passou a anunciar `--operation-id`. Sem reinício de container para essa correção.
- Nomes de pastas de séries e arquivos VOD normalizados para ASCII na construção do catálogo. Títulos originais e metadados do servidor permanecem intactos. Séries sem transliteração recebem nome estável derivado do título; arquivos sem transliteração usam o ID da tarefa.
- Regressão reproduz o caso Cyberpunk junto ao episódio 256 e passa pelo validador real de entradas. Outro teste cobre nomes inteiramente não latinos.
- `pytest tests/test_mixed_media_package.py tests/test_mixed_media_template.py -q`: 23 testes e 7 subtestes aprovados.
- Após o reparo, a operação original foi retomada e terminou como falha segura; uma nova operação pela API iniciou a preparação da geração 20. O conteúdo da geração só pode ser declarado aplicado após `verify-active` e inspeção do manifesto real.

## Cuidados de implantação

Conferir separadamente os hashes dos módulos da imagem, dos bind-mounts e do publicador do host. Testar o contrato do cliente implantado, não apenas o checkout local. Não usar sucesso antigo do journal como confirmação de um clique novo. Erros terminais precisam continuar associados ao ID de operação e disponíveis no painel.

Pendente de melhoria além deste reparo: propagar o erro específico do build em vez de reutilizar um journal antigo, detectar saída precoce do cliente lançado em background e aplicar transferência por allowlist conforme a arquitetura V2. Não declarar essas melhorias implementadas por esta correção.
