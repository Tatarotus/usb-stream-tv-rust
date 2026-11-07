# Progresso aproximado de séries na mídia mista

## Problema e comportamento

O histórico antigo só registrava ações explícitas no controle web. Abrir outro episódio diretamente nas pastas da TV, sem trocar a geração do pendrive, não atualizava o histórico. A detecção nova registra o **possível episódio reproduzido**, inclusive ao navegar por vários episódios na mesma geração. Não registra minuto de reprodução nem marca episódios como concluídos.

## Como funciona

Depois de uma resposta HTTP de mídia interna ser enviada, o servidor observa leituras qualificadas de arquivos de séries. A regra conservadora exige seis blocos distintos de 1 MiB na parte interna do arquivo, em ao menos três intervalos de 15 segundos e ao longo de pelo menos 45 segundos. Leituras distantes formam grupos independentes; cabeçalhos, finais e arquivos menores que 32 MiB não qualificam. A gravação de histórico é feita em uma thread separada, com fila limitada e nova verificação de tarefa pronta e revisão de conteúdo.

Essa regra é uma **heurística**: sondas ou leituras antecipadas suficientemente prolongadas podem imitá-la, e cache, seeks, falhas de rede ou padrão diferente de leitura podem impedir a detecção. A interface apresenta “Possível reprodução na TV”. O cache prévio no servidor acessa o armazenamento diretamente; não equivale a comprovação de visualização. A regra precisa de validação com a TV real e pode exigir ajuste com base em medições. Não há garantia de que todos os episódios serão detectados.

Uma seleção ou checkpoint explícito de outro episódio recebe prioridade por 60 segundos. A ordem de eventos novos usa um contador persistido, para preservar a ordem mesmo se o relógio mudar. A inferência não desfaz uma marcação explícita de conclusão. Leituras antigas não foram armazenadas, portanto não é possível reconstruir com certeza o episódio já visto antes da ativação.

## Controle e implantação

`MIXED_WATCH_INFERENCE=0` desativa o detector; o padrão é `1`. Para implantar é necessário incluir `scripts/mixed_watch_signal.py` junto ao servidor, atualizar `vod_history.py` e `dashboard.html` e reiniciar o processo Python em uma janela que não interrompa pessoas assistindo. A atualização de `Dockerfile` inclui o módulo em imagens novas; confira os mounts e a configuração reais antes de qualquer recriação. O desenvolvimento e os testes locais não alteram a VPS.

Valide fisicamente com uma série pronta: inicie um episódio na TV, aguarde leituras sustentadas e verifique o rótulo no controle web; troque para outro episódio dentro da mesma geração e repita. Teste navegação sem reproduzir, seek, retorno a um episódio anterior, pausa e pré-carregamento. Não altere ou conclua episódios históricos como parte do teste.
