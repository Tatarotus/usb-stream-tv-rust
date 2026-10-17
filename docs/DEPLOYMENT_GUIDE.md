# Guia de Implantação Passo a Passo

Este guia detalha o procedimento completo para configurar o **USB-Stream-TV** tanto no servidor remoto (Oracle Cloud VPS / Servidor Linux) quanto no dispositivo Android emulador (Tablet Samsung Galaxy Tab 3 Lite SM-T110 ou equivalente).

---

## Parte 1: Implantação no Servidor (Oracle Cloud VPS / Ubuntu)

### 1.1. Pré-Requisitos
- Servidor Linux com Ubuntu 22.04 / 24.04 ou Debian 12 (mínimo: 2 vCPUs, 2 GB RAM).
- **Docker Engine** e **Docker Compose** instalados.
- Um domínio ou subdomínio apontado para o IP da VPS (ex.: `tv.seudominio.com`).
- Portas 80 e 443 liberadas no firewall da nuvem e no `ufw`.

### 1.2. Instalação e Configuração

1. Copie o diretório do projeto para a VPS:
   ```bash
   scp -r ~/Code/usb-stream-tv-prod usuario@sua-vps:/opt/containers/apps/usb-stream-tv
   ```

2. Acesse o servidor e entre no diretório:
   ```bash
   ssh usuario@sua-vps
   cd /opt/containers/apps/usb-stream-tv
   ```

3. Configure o arquivo `.env` com suas credenciais:
   ```bash
   cp .env.example .env
   nano .env
   # Preencha TV_PIN, XTREAM_UPSTREAM, XTREAM_USER, XTREAM_PASS
   ```

4. Suba o container com Docker Compose:
   ```bash
   docker compose up -d --build
   ```

5. Verifique a integridade do serviço:
   ```bash
   curl -s http://localhost:8080/api/status | jq .
   ```

### 1.3. Configuração do Proxy Reverso (Caddy com SSL Automático)

No arquivo `/etc/caddy/Caddyfile`:
```caddy
tv.seudominio.com {
    reverse_proxy localhost:8080 {
        header_up Host {host}
        header_up X-Real-IP {remote}
        header_up X-Forwarded-For {remote}
        header_up X-Forwarded-Proto {scheme}
    }
}
```

Recarregue o Caddy:
```bash
sudo systemctl reload caddy
```

---

## Parte 2: Implantação no Dispositivo Android (Tablet SM-T110)

### 2.1. Pré-Requisitos do Tablet
1. **Acesso Root Completo**: SuperSU ou Magisk instalado.
2. **BusyBox Instalado**: Utilitários do BusyBox em `/system/xbin/` (incluindo `pgrep`, `pkill`, `sed`, `mkfifo`, `setsid`).
3. **Depuração USB (ADB) Ativada**: Habilitada nas Opções do Desenvolvedor.
4. **Cabo Micro-USB Confiável**: Capaz de transferir dados em alta velocidade.

### 2.2. Deploy Automatizado via Script

A partir da máquina de desenvolvimento com o tablet conectado via ADB:

```bash
# Se o tablet estiver conectado via cabo USB local
adb devices

# Ou se conectado via rede local / ADB Wi-Fi
adb connect 192.168.1.150:5555

# Executar o deploy automatizado
cd ~/Code/usb-stream-tv-prod
./scripts/deploy_tablet.sh 192.168.1.150:5555
```

O script `deploy_tablet.sh`:
- Cria a estrutura `/data/local/tmp/ntfs_lab/`.
- Envia o template esparso de 128 GiB (`templates/ntfs_template.sparse.gz`) e descompacta via `src/tools/sparse_unpack_arm32` em `/data/local/tmp/ntfs_lab/ntfs_template.bin` (ocupando apenas ~69 MB reais).
- Instala os binários compilados estáticos ARM32 (`fuse_ntfs`, `fuse_direct_arm32`, `stream_fetcher`, `patch_trp`, `sparse_unpack_arm32`) com permissões de execução.
- Configura o watchdog de energia, wakelocks, túnel reverso e CPU governor para `performance`.
- Inicia o serviço de TV ao vivo com pré-buffer de segurança e validação do barramento USB.

---

## Parte 3: Conexão e Reprodução na TV Samsung

1. **Conexão Física**:
   - Conecte o cabo Micro-USB do tablet à porta USB da TV Samsung (preferencialmente na porta rotulada como **USB HDD** ou **USB 5V 1A**, que fornece mais corrente).
2. **Reconhecimento**:
   - A TV exibirá uma notificação na tela: *"Novo dispositivo conectado: LIVETV1"* (ou `LIVETV2`).
3. **Reprodução**:
   - Pressione o botão **Source** no controle remoto da TV e selecione a unidade USB conectada.
   - Entre na pasta **Vídeos**.
   - Selecione o arquivo exibido: **`TV AO VIVO 2.tp`** ou **`TV AO VIVO.trp`**.
   - Pressione **Play** no controle da TV. A transmissão iniciará imediatamente em Full HD.
4. **Troca de Canais**:
   - Abra o controle remoto no celular acessando `https://tv.seudominio.com`.
   - Clique em qualquer canal desejado. O canal mudará na TV suavemente sem tela preta.
   - Se desejar invalidar totalmente a unidade e forçar nova leitura do zero, clique no botão **"TV Ao Vivo"** no cabeçalho do controle remoto.
