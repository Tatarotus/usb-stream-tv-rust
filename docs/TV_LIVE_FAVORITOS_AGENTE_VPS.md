# Agente OpenCode de TV Live Favoritos na VPS

Criado em 25/10/2026 para implementar e testar o plano Astra em ambiente isolado,
sem publicar mudanças enquanto usuários assistem ao VOD/Live.

## Acesso

```sh
ssh sam@129.146.5.64 -t "tmux attach -t tv-live-favoritos"
```

Para sair da visualização mantendo a sessão: `Ctrl+B`, depois `D`.
A sessão existente `istanbullu-sync` não foi modificada.

## Configuração

- Diretório independente: `/home/sam/Code/tv-live-favoritos`.
- Modelo principal e auxiliar: `opencode/deepseek-v4.1-flash-free`.
- OpenCode instalado na VPS: versão1.18.35 na preparação.
- Conta de execução: `tv-live-agent`, sem login interativo ou grupoDocker.
- Serviço isolado: `tv-live-favoritos-agent.service`, associado ao terminaltmux.
- CPU limitada a35% de umCPU; memória1,5GiB;128tarefas; prioridade de I/O baixa.
- Sistema de arquivos global somente leitura; home da contaSam oculto, exceto
  workspace próprio e toolchainRust exposta somente leitura.
- `/opt/containers`, socketsDocker, credenciaisSSH e dispositivos reais não são
  acessíveis ao agente. `NoNewPrivileges` e capabilities vazias.
- Configuração/cache/histórico do OpenCode separados em `runtime/`.
- Plugins externos desativados, outros providers desativados, comandos de
  deploy/restart/SSH/ADB e subagentes negados na configuraçãoOpenCode.

O teste de conexão do modelo retornou `MODELO_GRATUITO_OK` e custo reportado0.
O modelo foi confirmado na lista real de modelos da instalação; não foi usado
provider pago como substituto.

## Material entregue ao agente

- `TASK.md` e `AGENTS.md`: escopo, ordem de execução e proibição de produção.
- `docs/prompts/TV_AO_VIVO_FAVORITOS_PASTAS.md`: roteiro operacional.
- `docs/prompts/TV_AO_VIVO_FAVORITOS_ASTRA.md`: revisão completa doAstra.
- Snapshot do códigoRust/Python, scripts, testes e templates compactados.
- `reference/legacy-c/`: fontesC/headers, sem binários ou dados de runtime.
- RepositórioGit próprio com baseline para comparar alterações.
- Não foram copiados `.env`, credenciais ou bibliotecaVOD da produção.

O primeiro alvo é um MVP dehost com SBT/Record, manifesto, NTFS por extents,
detector deprobes, contratos deepoch e testes. A identificação do binário no
tablet e os testes físicos ficam pendentes da janela do usuário.

## Acompanhar e entregar

O agente deve atualizar `STATUS.md` a cada marco e produzir
`IMPLEMENTATION_READY.md` com código, testes, limitações e implantação proposta.
Ao terminar o trabalho dehost, deve aguardar nova instrução; a sessão disponível
não equivale a autorização para deploy ou a produção automaticamente atualizada.

Leituras de acompanhamento:

```sh
ssh oracle 'tmux capture-pane -p -t tv-live-favoritos:0.0 -S -60'
ssh oracle 'cat /home/sam/Code/tv-live-favoritos/STATUS.md'
ssh oracle 'systemctl show tv-live-favoritos-agent.service -p ActiveState -p MemoryCurrent -p MemoryMax -p CPUQuotaPerSecUSec'
```

Somente quando o usuário pedir para parar ESTE agente:

```sh
ssh oracle 'sudo systemctl stop tv-live-favoritos-agent.service'
```

Esse comando refere-se apenas ao serviço do agente de desenvolvimento.
Não usar comandos amplos que paremDocker, a aplicaçãoTV ou a sessão da novela.
Para reiniciar o agente posteriormente, inspecionar primeiro se já existe serviço
ativo e usar `launch.sh` no workspace através da sessão própria. Não executar duas
instâncias simultâneas no mesmo workspace.

O resultado continua sujeito à revisão e ao teste físico na Samsung. A remoção
das proteções ou ativação de mudanças requer instrução explícita do usuário.
