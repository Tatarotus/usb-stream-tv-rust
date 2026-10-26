# Prompt para o agente executor

Execute as duas tarefas abaixo. O usuário autorizou a migração dos episódios
antigos e a comunicação com o agente produtor da sessão tmux `istanbullu-sync`.
Trabalhe até concluir, sem pedir aprovação a cada arquivo. Preserve os originais
quando uma verificação falhar e reporte a causa concreta.

## Objetivos

A. Migrar para o Google Drive os episódios **já cadastrados no VOD** de
**A Noiva de Istambul — Star 526p/528p**, preservando seus IDs, metadados e acesso
pela TV. Liberar as respectivas cópias permanentes da VPS após verificação.

B. Comunicar ao agente que está produzindo os próximos episódios que a entrega
deve terminar no Drive, e atualizar sua confirmação de publicação. O pipeline
de qualidade, render e sincronismo não deve ser alterado.

## Contexto conferido em 25/10/2026

Confirme novamente: o produtor pode ter avançado depois deste levantamento.

- SSH disponível por `ssh oracle`, para a VPS `129.146.5.64`.
- Projeto do servidor neste computador: `/home/sam/Code/usb-stream-tv-rust`.
- Aplicação em produção na VPS: `/opt/containers/apps/usb-stream-tv`.
- Container: `usb-stream-tv`; HTTP interno na porta 8080.
- Compose efetivamente usado: `compose.gdrive.runtime.yaml` no diretório da aplicação.
- Remote do rclone: `sam:USB-Stream-TV/VOD`.
- Configuração privada dentro do container: `/run/rclone/rclone.conf`.
- Catálogo dentro do container: `/app/vod_cache/vod_meta.json`.
- Mídias finais locais: `/app/vod_cache/<task_id>.mp4`.
- Templates: `/app/vod_cache/<task_id>.bin`.
- Há **48 episódios locais ready, 131–178, somando 27,47 GiB**.
- Os títulos reais são `A Noiva de Istambul N - Star 528p`.
- Primeiro exemplo: episódio 131, ID `54b7855c`.
- Último exemplo: episódio 178, ID `1b26cbaf`.
- Projeto produtor na VPS: `/home/sam/Code/istanbullu-sync`.
- Sessão/pane observada: `istanbullu-sync:0.0`, programa `agy-bin`.
- Na observação, o agente estava trabalhando no episódio **179**.

O acesso SSH pode usar `sam@129.146.5.64` se o alias oracle não estiver disponível.
Não inicie outra instância do produtor nem reinicie a série do episódio 131.

## Entenda isto antes de agir

O servidor JÁ manda novos VODs ao Drive quando passam por `/api/vod/prepare`.
O produtor documentado JÁ usa essa API. Portanto B não exige adicionar um upload
bruto ao Modal: exige manter a API e confirmar `ready` **e** `storage=gdrive`.

Os antigos não foram migrados automaticamente. Fazer `rclone move vod_cache ...`
sozinho quebra o catálogo. O servidor mantém `VOD_TASKS` em memória: editar seu
JSON por outro processo enquanto ele está rodando pode ser sobrescrito e perder
publicações novas. **Não importe server.py num segundo processo para migrar e
salvar o catálogo.** É permitido importar apenas `vod_storage` para verificações
independentes, que não alteram o catálogo.

Também não use `/api/vod/prepare` com URLs inventadas para os antigos: isso gera
novos task_ids ou devolve o ready local existente, em vez de migrá-lo corretamente.

Não apague templates, metadados, candidatos em processamento, fontes Star TV,
arquivos IPTV ou episódios de outras séries. Migre os MP4 finais registrados.
Não abra portas públicas, não mostre `.env`/rclone.conf nem imprima PIN/tokens.

## 1. Conferir o estado e preparar backup privado

Leia o AGENTS.md de cada projeto antes de alterá-lo. No servidor, leia
`docs/GDRIVE_VOD.md`, `vod_storage.py` e os trechos VOD de `server.py`.

Comandos iniciais, somente leitura:

```sh
ssh oracle 'tmux list-panes -t istanbullu-sync -F "#{session_name}:#{window_index}.#{pane_index} #{pane_current_command} #{pane_current_path}"'
ssh oracle 'docker inspect usb-stream-tv --format "{{index .Config.Labels \"com.docker.compose.project.config_files\"}}"'
```

Leia o catálogo por dentro do container: o arquivo pode ser de root e não ser
legível diretamente pelo usuário SSH.

```sh
ssh oracle 'docker exec -i usb-stream-tv python3 -' <<'PY'
import json, re, unicodedata
from pathlib import Path

def normalizar(texto):
    return ''.join(c for c in unicodedata.normalize('NFKD', texto)
                   if not unicodedata.combining(c)).lower()

tasks = json.loads(Path('/app/vod_cache/vod_meta.json').read_text())
selecionados = []
for tid, task in tasks.items():
    titulo = task.get('title', '')
    nome = normalizar(titulo)
    if ('noiva' in nome and ('istambul' in nome or 'istanbul' in nome)
            and re.search(r'star\s*(?:526|528)\s*p', nome)):
        selecionados.append({
            'id': tid, 'title': titulo, 'status': task.get('status'),
            'storage': task.get('storage', 'local'),
            'bytes': task.get('file_size', 0),
            'local_exists': Path(task.get('file_path') or '/invalid').is_file(),
        })
print(json.dumps(selecionados, ensure_ascii=False, indent=2))
print('Total:', len(selecionados))
PY
```

Guarde um manifesto dos IDs selecionados e tamanhos. Crie backup privado, com
permissão 700 no diretório e 600 nos dados sensíveis, de catálogo, server.py,
vod_storage.py, configuração de deploy e imagem anterior. Use a árvore de
backups da VPS: `/opt/containers/backups/`. Não coloque credenciais no Git.
O backup de catálogo é para recuperação controlada: restaurá-lo inteiro depois
de novas publicações pode apagar os registros novos.

## 2. Avisar o produtor e combinar uma janela de implantação

Faça B primeiro no que diz respeito ao aviso: ele evita reiniciar o container
durante a ingestão de um candidato. A migração dos arquivos vem depois.

A mensagem pronta está neste arquivo do projeto do servidor:
`docs/prompts/ISTANBULLU_GDRIVE_PARA_PRODUTOR.txt`.

Copie e cole na sessão existente. Não faça attach interativo bloqueando seu
terminal; não use Ctrl-C para interromper o agente. Primeiro confira a pane e
seu programa. Se estiver num shell ou diálogo diferente de entrada de mensagem
do agente, não cole o texto como se fosse comando shell.

Exemplo, executado neste computador, na raiz do projeto do servidor:

```sh
scp docs/prompts/ISTANBULLU_GDRIVE_PARA_PRODUTOR.txt oracle:/tmp/istanbullu-gdrive-instruction.txt
ssh oracle 'tmux load-buffer -b gdrive-handoff /tmp/istanbullu-gdrive-instruction.txt'
ssh oracle 'tmux paste-buffer -p -d -b gdrive-handoff -t istanbullu-sync:0.0'
```

Inspecione se a mensagem foi colada na entrada correta; então envie:

```sh
ssh oracle 'tmux send-keys -t istanbullu-sync:0.0 Enter'
```

Confira a resposta com `tmux capture-pane -p -t istanbullu-sync:0.0 -S -100`.
Não despeje transcrições inteiras com URLs privadas ou credenciais no relatório.
Se estiver ocupado, use a fila de mensagens da própria interface e aguarde a
confirmação. Não presuma que colar texto equivale a ele ter entendido.

Exija confirmação do episódio corrente, adoção do critério ready+gdrive e janela
sem ingestão antes de reiniciar o container. Render/QA podem continuar. Se um
arquivo já estiver em `/tmp/serve`, preserve também o candidato original fora
do container; uma recriação pode perder o diretório temporário.

## 3. Criar a operação de migração no servidor vivo

Na inspeção original NÃO havia endpoint de migração. Confirme se outro agente
já o adicionou. Se não houver, implemente uma extensão pequena e testada:

**POST `/api/vod/migrate`**, corpo `{"task_id":"54b7855c"}`, autenticado pelo
mesmo `X-Auth-PIN` de `/api/vod/prepare`.

Contrato obrigatório:

1. Validar ID de 8 caracteres hexadecimais, autenticação e Drive habilitado.
2. Consultar a tarefa no `VOD_TASKS` do processo que atende a API.
3. Se já for ready/gdrive, responder sucesso idempotente sem reenviar.
4. Aceitar ready/local e repetição de falha de upload com MP4 preparado intacto.
5. Recusar ou adiar tarefa ativa na TV, selecionada/aquecendo, em preparação,
   excluindo ou já migrando. Não interromper o episódio em reprodução.
6. Registrar o worker no mecanismo de deduplicação do servidor. Não criar dois
   workers para o mesmo ID. Responder 202 e executar o upload em background.
7. Usar o mesmo `VOD_SEMAPHORE`: apenas um upload/preparo por vez. Não segurar
   `VOD_TASKS_LOCK` durante upload, hashing ou chamadas de rede.
8. Depois de obter o semáforo, revalidar identidade, estado e seleção sob lock;
   marcar a tarefa como uploading antes de liberar o lock para impedir uma
   reprodução concorrente começar justamente nessa transição.
9. Reutilizar **`upload_prepared_vod(task_id, task)`** e
   **`GDRIVE_STORAGE.upload(local_path, task_id)`**, sem re-renderizar.
   O helper já verifica tamanho+MD5 remoto, atualiza o registro, salva por
   `save_vod_tasks(strict=True)` e só depois libera o MP4 local.
10. Preserve ID, título, pôster, `display_name`, `template_path`, `series_id`,
    favoritos e identidade da tarefa. Destino: `sam:USB-Stream-TV/VOD/<ID>.mp4`.
11. Em falha, mantenha a origem e o estado de retomada. Não anuncie sucesso.
    Limpe o registro de worker em finally, inclusive após cancelamento.
12. Não use o cache do vídeo atual para aquecer cada um dos 48 arquivos.
    Acrescente ao helper, se necessário, parâmetro opcional `seed_cache=True`,
    preservando o padrão atual; a migração chama com `seed_cache=False`.
    Não altere o cache do vídeo que estiver selecionado na TV.

Não faça um programa que carrega o catálogo fora do servidor, envia 48 arquivos
e sobrescreve o JSON ao final. O ponto central desta API é ter um único dono
dos metadados e não perder os episódios que o produtor publica em paralelo.

Teste: autenticação, idempotência, exclusão/cancelamento, tarefa ativa recusada,
erro mantém origem, hash inválido impede unlink, commit do catálogo falho impede
unlink e publicações simultâneas não somem. Reaproveite os testes existentes em
`tests/test_server_gdrive.py` e `tests/test_vod_storage.py`.

Preserve as alterações preexistentes e compare o código remoto antes do deploy.
Não substitua todo server.py por uma cópia local desatualizada. Não altere
pipeline Live, codecs, QA, sincronização ou configurações USB nesta tarefa.

Na janela combinada, mantenha as montagens, rede gateway_net e configuração
atual. O deploy existente usa:

```sh
cd /opt/containers/apps/usb-stream-tv
docker compose -p usb-stream-tv -f compose.gdrive.runtime.yaml build
docker compose -p usb-stream-tv -f compose.gdrive.runtime.yaml up -d --no-build
```

Não volte ao compose básico isoladamente: ele difere da configuração real e
pode perder montagens ou publicar uma porta que hoje é interna.

Depois que a API responder, avise o agente produtor na mesma pane que o serviço
voltou e o Passo 11 pode prosseguir. Se /tmp/serve foi perdido, ele deve repor o
candidato preservado e o HTTP interno, sem repetir o render aprovado.

## 4. Migrar um episódio piloto

Comece pelo 131, ID `54b7855c`, se ainda estiver local e não estiver ativo.
Se já estiver migrado, selecione o primeiro ID local do manifesto.

Exemplo de chamada, SOMENTE depois de implementar a API:

```sh
ssh oracle 'docker exec -i usb-stream-tv python3 -' <<'PY'
import json, os, urllib.request
pin = os.environ.get('TV_PIN')
if not pin:
    raise SystemExit('TV_PIN ausente; consultar a configuração privada sem imprimir')
request = urllib.request.Request(
    'http://127.0.0.1:8080/api/vod/migrate',
    data=json.dumps({'task_id': '54b7855c'}).encode(),
    headers={'Content-Type': 'application/json', 'X-Auth-PIN': pin},
)
with urllib.request.urlopen(request, timeout=15) as response:
    print(response.status, response.read().decode())
PY
```

Consulte `/api/vod/status` pelo mesmo localhost interno. Exija:

```text
status = ready
storage = gdrive
remote_path = <ID>.mp4
remote_md5 = checksum válido
file_size = tamanho anterior
```

Faça uma verificação independente com rclone `lsjson --stat --hash` do objeto
e compare tamanho e MD5. A versão instalada pode retornar a chave `md5` em
minúsculas: normalize o nome, não confunda isso com arquivo corrompido.
Confirme GET Range 206 com corpo exato e template ainda disponível. Não chame
`/api/vod/play` durante estes testes: a tarefa é armazenar, não trocar a TV.

Só confirme remoção da origem após esses critérios. Não exclua arquivos na mão
para contornar falha do helper. Se o piloto falhar, corrija antes de fazer o lote.

## 5. Executar o lote de forma retomável

Crie um runner operacional em `scripts/`, com modo dry-run e manifesto/relatório
em `scratch/` ou outro diretório privado apropriado, nunca scripts temporários
soltos na raiz. Ele deve chamar a API, não escrever o catálogo.

- Refaça o inventário por `/api/vod/status` e mantenha a seleção série+Star526/528.
- Um ID por vez; não paralelize 48 uploads nem duplique todos os vídeos no disco.
- Antes de iniciar outro ID, dê prioridade a publicações novas em fila/processando.
  Não deixe o produtor esperar pela migração da coleção inteira.
- Ignore idempotentemente os que já têm ready/gdrive.
- Pule temporariamente o episódio ativo; retente depois sem interromper a TV.
- Aguarde cada migração terminar; em erro registre o ID e mantenha a origem.
- Persistir progresso a cada arquivo permite retomar sem reenviar os concluídos.
- Registre contagem e bytes migrados, pendências e espaço real recuperado.

Pode rodar numa NOVA sessão tmux separada, por exemplo `vod-gdrive-migrate`.
Não substitua, renomeie ou reutilize `istanbullu-sync` para executar o lote.
Cache atual ou outros hardlinks podem fazer a liberação de disco diferir da soma
dos arquivos; confirme `df`, não invente um total recuperado.

## 6. Confirmar a publicação dos episódios futuros

O outro agente deve continuar: candidato aprovado → `/api/vod/prepare` →
aguardar ready+gdrive → validar Range → favoritos/registro → limpar temporários.
Ele deve persistir essa regra no Passo 11 do seu AGENTS.md e handoff aplicável.

Confira a confirmação dele e o estado do primeiro episódio novo entregue depois
do aviso. Se o 179 já tiver sido publicado localmente antes da mudança, inclua-o
na migração e use o próximo como prova. Não recomece episódios já aprovados.

## Critérios finais de conclusão

- Todos os IDs selecionados migrados, ou pendências concretas listadas sem perda.
- Catálogo conserva os mesmos IDs, títulos e favoritos.
- MP4 remoto existe com tamanho+MD5 corretos; template e HTTP Range funcionam.
- Origens locais dos concluídos foram liberadas pelo fluxo seguro.
- Espaço livre real medido antes/depois.
- Agente produtor confirmou e persistiu o novo critério de entrega.
- Ao menos um novo episódio foi publicado como ready/gdrive, ou o produtor segue
  trabalhando e essa comprovação está explicitamente pendente. Não declare uma
  publicação futura como concluída apenas porque enviou a mensagem.
- Relatório curto com quantidades, episódios/IDs, falhas, testes, bytes e disco.
