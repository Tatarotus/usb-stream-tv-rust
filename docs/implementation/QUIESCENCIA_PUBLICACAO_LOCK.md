# Quiescência de publicação — corrida do `stop` e proposta de lock atômico

Status: **BLOQUEIO ABERTO — não implementado**. Este documento é entrega de
análise para revisão por outro agente. Nenhuma linha de `scripts/mixed_bridge.py`,
`scripts/mixed_generation_service.py` ou `scripts/migrate_export_to_tv.sh` foi
alterada para tratar deste assunto — os arquivos ficam com o outro agente.

Data: 30/10/2026. Origem: auditoria final da migração de export.

---

## 1. O defeito (reproduzível por leitura)

`scripts/migrate_export_to_tv.sh` chama `stop_bridge_verified()`, que:

1. conta o que está em `requests/` e `processing/` do spool (spool
   `/opt/containers/apps/usb-stream-tv/vod_cache/.mixed_bridge`);
2. procura processo de publisher vivo;
3. **só então** executa `systemctl stop usb-stream-tv-mixed-bridge.service`;
4. confere de novo o spool e o processo.

O problema é a janela entre (2) e (3): o daemon da bridge aceita uma requisição
(rename atômico para `processing/`) e **gera o filho publisher** exatamente nesse
intervalo. Como a unidade não declara `KillMode`, o systemd usa o padrão
`control-group` — o `stop` mata o **cgroup inteiro**, inclusive o publisher no meio
de uma publicação. O `ffmpeg`/publisher morto no meio deixa a publicação
inconsistente (`publisher_timeout_reconciliation_required`, estado `unknown`).

**As checagens antes/depois apenas DETECTAM a corrida; não a impedem.** Detectar
depois não desfaz o dano: a publicação já foi interrompida.

## 2. Por que os remendos usuais não resolvem

| Remendo | Por que não basta |
| --- | --- |
| Checar duas vezes (antes e depois do `stop`) | a corrida pode ocorrer entre a checagem e o `stop`; a segunda checagem só constata o estrago |
| `KillMode=process` na unidade | evita matar o filho, mas deixa o publisher órfão publicando contra um export já trocado |
| Esperar só o spool esvaziar | `processing/` é esvaziado pelo **próprio handler** depois do publisher terminar; entre o rename e a criação do filho não há nada no disco |
| Parar o timer primeiro | o timer é outra fonte de requisição; não fecha a janela da bridge |
| Matar/`pkill` por nome | atinge processo alheio; proibido pelo projeto |

## 3. Proposta — protocolo de quiescência com `flock` (reader/writer)

Um único arquivo de lock compartilhado pelos **três** participantes, por exemplo
`<spool>/quiesce.lock` (criado 0600 no diretório que já é 0700):

| Participante | Operação | Semântica |
| --- | --- | --- |
| bridge, antes de **reivindicar** qualquer requisição de `requests/` | `flock(LOCK_SH \| LOCK_NB)` | falhou ⇒ **recusa a requisição** com resposta explícita ("janela de manutenção"); sucesso ⇒ mantém o lock **até o publisher terminar** e só então libera |
| publisher / `mixed_generation_service.py` iniciado direto por CLI | `flock(LOCK_SH \| LOCK_NB)` na inicialização | falhou ⇒ sai com código próprio e mensagem clara, **antes** de tocar o tablet |
| migrador | `flock(LOCK_EX)` em laço com `LOCK_NB` + espera limitada | só é concedido quando **nenhum** claim está em voo; com ele em mãos, nenhum `LOCK_SH` novo é concedido ⇒ a bridge **não consegue** iniciar publicação |

Propriedades que isso entrega:

1. **Exclusão mútua real**: com `LOCK_EX` concedido, nenhuma publicação nova
   começa — a recusa é feita pela bridge (requisições novas) e pelo publisher
   (CLI direto), sem depender de checagem oportunista.
2. **Requisjções já aceitas terminam**: o `LOCK_EX` só é concedido **depois** que
   os `LOCK_SH` em voo são liberados, ou seja, depois que o publisher em execução
   terminou. Ninguém é morto.
3. **Segurança a crash**: o lock é mantido pelo **kernel**. Se o migrador morrer
   (`kill -9`, panne, sinal), o `flock` é liberado automaticamente e a bridge
   volta a aceitar requisições em tempo limitado. Não existe "arquivo órfão"
   bloqueando para sempre — diferente de um marcador por existência de arquivo.
4. **Sem `kill`**: o migrador não precisa mais matar nada; o `systemctl stop` passa
   a ser permitido **somente** depois de o `LOCK_EX` estar em mãos e o spool
   drenado, quando não há publisher vivo para ser interrompido.
5. **Falha fechada**: se o lock não puder ser adquirido no prazo (publicação longa
   em curso), a migração **aborta** e reporta; não força nada.

## 4. Testes exigidos (barreiras + processos simulados, offline)

| # | Cenário | Resultado exigido |
| --- | --- | --- |
| T1 | bridge simulada mantendo `LOCK_SH` (publicação em voo) | migrador **não** obtém `LOCK_EX`; aborta com mensagem clara; **nenhum** `stop` é executado |
| T2 | migrador com `LOCK_EX` em mãos; bridge tenta `LOCK_SH` | bridge **recusa** a requisição (nenhum claim, nenhum publisher gerado) |
| T3 | CLI direto do publisher durante a janela | sai com código próprio, sem tocar no tablet |
| T4 | migrador morto com `kill -9` segurando `LOCK_EX` | bridge volta a obter `LOCK_SH` dentro de um limite curto (o kernel liberou) |
| T5 | caminho feliz: sem holders, spool vazio | `LOCK_EX` concedido, spool drenado, `stop` executado depois disso, migração segue e libera |
| T6 | publisher em execução + janela | o migrador espera o término; **não** mata; se estourar o prazo, aborta |

Barreiras: usar `flock` real em `/tmp`, processos filhos reais e sincronização por
arquivo-sentinela entre eles (nada de `sleep` como prova de ordem).

## 5. Recomendação enquanto o bloqueio não for fechado

**Manter a operação automatizada de migração DESABILITADA** (recusa explícita em
`apply`) e documentada como bloqueada — em vez de declará-la segura. Trocar o
export hoje exige: janela autorizada, bridge parada à mão pelo operador e
conferência dos cinco leitores. Nenhum caminho automático deve parar a bridge
enquanto houver publisher possível.
