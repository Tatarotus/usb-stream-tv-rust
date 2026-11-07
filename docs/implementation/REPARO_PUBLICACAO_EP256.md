# Publicação bloqueada após download — episódio 256

Diagnóstico em 2026-11-07, após clique real no controle web.

## Falhas medidas

1. O bind-mount `/app/scripts/mixed_bridge.py` carregava uma revisão antiga sem `--operation-id`, embora o daemon no host já tivesse a revisão compatível. O processo do cliente terminava antes de criar o pedido na ponte; a operação ficava em `preparing`. A imagem nova não substitui o conteúdo de um bind-mount antigo.
2. Depois de sincronizar a ponte, a preparação falhou em `invalid NTFS component`: a pasta `Cyberpunk Mercenários` continha caracteres fora do conjunto ASCII aceito pelo catálogo do tablet. O erro de build era capturado e o estado ainda apontava à geração anterior, resultando na mensagem menos específica `task_not_in_generation` para o episódio 256, embora o problema fosse outra série.
3. A transferência terminou, mas o handoff foi recusado: o ADB concatena argumentos para um shell remoto e `su -c` interpreta novamente. Sem proteger o comando inteiro, os escapes de `tr` eram consumidos cedo: os NULs continuavam presentes e caracteres `0` viravam `n`. Assim o host não reconhecia o caminho do supervisor e não apresentava a identidade exigida pelo lease. Medido no tablet antes/depois: a leitura corrigida produziu nove linhas, zero NULs e a identidade completa com `answered=True`.

## Correções e verificação

- Arquivo da ponte em produção atualizado com a revisão já rastreada, preservando inode e backup; `client --help` passou a anunciar `--operation-id`. Sem reinício de container para essa correção.
- Nomes de pastas de séries e arquivos VOD normalizados para ASCII na construção do catálogo. Títulos originais e metadados do servidor permanecem intactos. Séries sem transliteração recebem nome estável derivado do título; arquivos sem transliteração usam o ID da tarefa.
- Regressão reproduz o caso Cyberpunk junto ao episódio 256 e passa pelo validador real de entradas. Outro teste cobre nomes inteiramente não latinos.
- `pytest tests/test_mixed_media_package.py tests/test_mixed_media_template.py -q`: 23 testes e 7 subtestes aprovados.
- Após o reparo, a operação original foi retomada e terminou como falha segura; uma nova operação pela API iniciou a preparação da geração 20. O conteúdo da geração só pode ser declarado aplicado após `verify-active` e inspeção do manifesto real.
- `TabletTransport.root_shell` protege agora o comando completo com `_quote` antes do primeiro shell remoto. O mock de transporte do serviço passou a simular esse primeiro parse, em vez de entregar a string intacta diretamente ao parser do dispositivo.
- Revisão independente com `ask-astra --deep` confirmou a causa e a correção de quoting; o resultado foi conferido contra comandos reais do tablet. `pytest tests/test_mixed_generation_service.py tests/test_host_tablet_identity.py -q`: 137 testes e 59 subtestes aprovados.
- Um Pi criou `tests/test_tablet_transport_quoting.py`, que executa a concatenação do ADB, shell remoto e shim de `su` com um segundo shell, incluindo a camada SSH. Os testes exercitam tokens com pipes, escapes de NUL, aspas e substituições literais, com contraprovas do método antigo. O painel do Pi foi encerrado antes do relatório final; o auditor leu e executou a entrega: 14 testes aprovados. Bateria final combinada de serviço, identidade e transporte: **151 testes e 59 subtestes aprovados**.

## Resultado operacional conferido

Geração **20**, sessão `mixed-gen20-66a1ab08`, `verify-active verified=1 lock_owned=1`, USB `CONFIGURED` e LUN da geração 20. Manifesto `f0ac19bf168ea1976813cc6c83f1ba78355723b2749c552979bfadcfe027ceb4` contém `Series/A Noiva de Istambul/A Noiva de Istambul S01E256.mp4`, tarefa `4fbf7a8f`, tamanho `283496756`, revisão `md5:b8864eddf6de64a26135590792e17e88`.

O proxy ativo respondeu `206 bytes 0-63/283496756`, 64 bytes com cabeçalho MP4 `ftyp`. API informa geração 20 ativa e nenhuma pendência de geração. Aviso enviado ao usuário pelo Telegram para selecionar novamente o pendrive e testar; imagem e som do episódio ainda dependem de confirmação humana.

## Cuidados de implantação

Conferir separadamente os hashes dos módulos da imagem, dos bind-mounts e do publicador do host. Testar o contrato do cliente implantado, não apenas o checkout local. Não usar sucesso antigo do journal como confirmação de um clique novo. Erros terminais precisam continuar associados ao ID de operação e disponíveis no painel.

Pendente de melhoria além deste reparo: propagar o erro específico do build em vez de reutilizar um journal antigo, detectar saída precoce do cliente lançado em background e aplicar transferência por allowlist conforme a arquitetura V2. Não declarar essas melhorias implementadas por esta correção.
