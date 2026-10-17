# Especificações de Hardware e Limites de Decodificação

Este documento consolida os limites de engenharia, formatos aceitos e restrições críticas do decodificador de hardware presente na televisão **Samsung Plasma PL51F4000** (SoC **MStar**, plataforma **ConnectShare 2013**), bem como do hardware Android utilizado como emulador USB.

---

## 1. Perfil da TV: Samsung Plasma PL51F4000

| Componente | Especificação |
| :--- | :--- |
| **Modelo** | Samsung PL51F4000 (51" HD/Full HD Plasma TV) |
| **Ano do Firmware** | 2013 |
| **Processador / SoC** | MStar Semiconductor (MStar MSD / DTV SoC) |
| **Interface Multimídia** | Samsung ConnectShare Movie (USB 2.0 Host) |
| **Sistemas de Arquivos USB** | FAT32, NTFS (suporte nativo leitura/gravação limitada) |
| **Extensões de Vídeo Válidas** | `.ts`, `.tp`, `.trp`, `.mpg`, `.mpeg`, `.avi`, `.mkv`, `.mp4` |

---

## 2. Limites do Decodificador de Vídeo

### 2.1. Codec H.264 / MPEG-4 AVC (Part 10)
- **Perfis Suportados**:
  - `Baseline Profile (BP)`
  - `Main Profile (MP)` — **Recomendado**
  - `High Profile (HP)`
- **Nível Máximo (Level)**: Até **Level 4.1**. Níveis superiores (ex.: 5.0, 5.1) causam erro *"Codec não suportado"*.
- **Resolução Máxima**:
  - `1920 × 1080` (1080p ou 1080i) a no máximo **30 fps**.
  - `1280 × 720` (720p) a no máximo **60 fps**.
  - *Nota*: Vídeos 1080p a 60 fps **não são decodificados por hardware** pelo SoC MStar desta geração.
- **Formato de Pixel**: `yuv420p` (8-bit 4:2:0). Canais em 10-bit (Hi10P) resultam em tela preta ou travamento.

### 2.2. Restrições Críticas de Temporização e Estrutura de Quadros

As seguintes condições provocam **congelamento imediato da imagem** no chip MStar:

1. **B-Frames com Lead Time Zero**:
   - Se o contêiner contiver quadros bidirecionais (B-frames) com timestamps onde `DTS == PCR`, o buffer de imagem codificada (*Coded Picture Buffer - CPB*) da TV esvazia antes que o quadro seja decodificado.
   - **Solução Mandatória**: Transcodificação com `-tune zerolatency` que desativa B-frames (`bframes=0`).
2. **Intervalo de Keyframe (GOP)**:
   - O intervalo entre quadros IDR não deve exceder **1.0 a 2.0 segundos** (`gop_size <= 60` em 30 fps).
   - O bitstream **deve** conter conjuntos de parâmetros de sequência e imagem (SPS/PPS) antes de **cada** frame IDR (`repeat-headers=1`).
3. **Fatiamento Multi-Slice**:
   - Transmissões de TV com múltiplos slices por quadro (ex.: 2 ou 4 slices) sobrecarregam o pipeline VLD (Variable-Length Decoder) do chip MStar quando há leitura via SCSI USB.
   - O FFmpeg deve gerar sempre quadros de fatia única (`slices=1`).

---

## 3. Limites do Decodificador de Áudio

| Codec de Áudio | Suporte no Contêiner TS | Avaliação Técnica |
| :--- | :--- | :--- |
| **Dolby Digital (AC-3)** | **100% Nativo** | **Padrão Obrigatório**. Decodificação perfeita em 48 kHz (192k a 640k). |
| **MPEG-1 Layer II (MP2)** | Nativo | Suportado, porém com fidelidade acústica inferior. |
| **AAC / HE-AAC** | Parcial / Instável | Frequentemente causa *"Formato de áudio não suportado"* ou mudo em contêineres `.ts`/`.trp` no firmware 2013. |
| **PCM / LPCM** | Suportado | Consome largura de banda excessiva sem ganho auditivo perceptível. |

**Configuração Padrão Aplicada**:
```bash
-c:a ac3 -b:a 384k -ar 48000 -ac 2
```

---

## 4. Requisitos do Contêiner MPEG-TS

Para leitura contínua via emulação de pendrive USB, o stream de transporte deve satisfazer o padrão ISO/IEC 13818-1 com as seguintes exigências:

- **Tamanho Estrito do Pacote**: `188 bytes`. Pacotes com timestamp de cabeçalho de 192 bytes (M2TS) ou 204 bytes (com FEC Reed-Solomon) são rejeitados pelo demuxer do ConnectShare.
- **Byte de Sincronismo**: `0x47` no primeiro byte de cada pacote.
- **Intervalo de PCR (Program Clock Reference)**:
  - O firmware da TV utiliza uma malha de captura de fase (PLL a 27 MHz) para sincronizar o clock de apresentação (STC).
  - O intervalo entre pacotes contendo PCR deve ser **$\le 40\text{ ms}$** (padrão DVB TR 101 290). No servidor, cravamos `-pcr_period 20` (a cada 20 ms).
- **Mapeamento de PIDs Fixo**:
  - `0x0000`: PAT (Program Association Table)
  - `0x1000`: PMT (Program Map Table)
  - `0x0100`: Stream de Vídeo H.264
  - `0x0101`: Stream de Áudio AC-3
  - `0x1FFF`: Pacotes de preenchimento nulo (Stuffing)

---

## 5. Especificações do Dispositivo Android Emulador

### 5.1. Dispositivo Validado: Samsung Galaxy Tab 3 Lite (SM-T110)
- **SoC**: Marvell PXA986 (Dual-core ARM Cortex-A9 @ 1.2 GHz)
- **Memória RAM**: 1.0 GiB (128 MiB alocados exclusivamente para o Ring Buffer FUSE)
- **Armazenamento**: Partição `/data` com pelo menos 1.5 GiB livres para imagem esparsa e templates
- **Sistema Operacional**: Android 4.4.2 KitKat (Linux Kernel 3.4.5)
- **Controlador USB Gadget**: `android_usb` (`/sys/class/android_usb/android0`)
- **Modo USB Configurado**: `mass_storage,adb`

### 5.2. Ajustes de Kernel Necessários
- **Wakelock Ativo**:
  ```bash
  echo "tv_stream" > /sys/power/wake_lock
  ```
- **Governador de CPU**: `performance` (evita quedas de frequência que causem jitter SCSI):
  ```bash
  for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
      echo performance > "$g"
  done
  ```
- **Wi-Fi Power Save**: Desativado (`iwconfig wlan0 power off`) para eliminar latência de recepção de pacotes.
