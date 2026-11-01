# Avaliação de 1080p e cópia de vídeo — fontes RJ

Medições pontuais de 27/10/2026. Os valores de bitrate abaixo incluem o
transporte MPEG-TS e variam conforme a programação; não representam uma
medição prolongada nem são equivalentes ao bitrate de vídeo isolado.

| Fonte | Amostra | Vídeo | IDR | Bitrate TS aproximado |
| --- | --- | --- | --- | --- |
| SBT RJ FHD¹, 207198 | 22 s | 1080p30, H.264 High, 0 B-frames, 1 slice | 2 s | 2,07 Mbps |
| SBT RJ HD¹, 207199 | 10,8 s | 720p30, H.264 High, 0 B-frames, 1 slice | 2 s | 1,76 Mbps |
| Record Rio FHD¹, 214639 | 21,35 s | 720p29,97, H.264 Main, 0 B-frames, 1 slice | 2,002 s | 2,93 Mbps |
| Record Rio FHD², 214640 | 23,12 s | 1080p29,97, H.264 Main, 0 B-frames, 1 slice | principalmente 1,001 s | 2,88 Mbps |

As fontes testadas tinham áudio AAC estéreo 48 kHz. Uma captura adicional de
8 s da Record 214640 foi decodificada sem erros; isso não garante que a fonte
permaneça estável. A comparação não confirma nem descarta os erros de SPS
observados em outro horário no SBT RJ. A Record 214639 estava em 720p apesar
do nome FHD. Nas amostras originais da Record, o intervalo de PCR chegou a
aproximadamente 67 ms, e o contêiner precisaria ser refeito para a TV.

## Capacidade da VPS

- Uma transcodificação curta de 3 s da Record RJ nativa 1080p, com libx264
  ultrafast, zerolatency, uma thread de vídeo, zero B-frames, uma slice,
  3,8 Mbps de vídeo e AC-3, atingiu 90 quadros a 40,38 fps (1,34×) sob a
  carga existente, usando `nice 19`. Foram relatados zero quadros duplicados ou
  descartados. A saída foi descartada; não houve teste na TV.
- Um teste de 3 s ampliando 720p para 1080p atingiu 32,47 fps (1,07×), com
  dois quadros duplicados. Cena, decodificação e carga são diferentes.
- Havia também um processo FFmpeg de produção configurado para 1080p. Sua
  existência e longevidade não demonstram que mantém 30 fps sem perdas.

Essas medições desmentem uma impossibilidade geral de 1080p, mas não provam
funcionamento contínuo nem determinam a margem com outras tarefas da VPS.
O resultado de 0,96× informado anteriormente, se medido corretamente, indica
que aquela configuração não acompanhou o tempo real naquele ensaio.

## Possibilidade de modo econômico

`-c:v copy` evita recodificar o vídeo, mas mantém as características da fonte.
Não elimina B-frames, transforma duas slices em uma, nem cria novos IDRs.
Áudio AAC precisaria ser convertido para AC-3 e o MPEG-TS, remultiplexado com
PCR adequado. O Record RJ 214640 é o candidato inicial mais promissor devido
ao H.264 Main, zero B-frames, uma slice e IDRs aproximadamente a cada 30
quadros, mas precisa de validação na TV e de observação prolongada da fonte.
O SBT RJ amostrado usa perfil High e IDR a cada 2 s; a implementação de
favoritos atual rejeita perfil High e as regras atuais exigem IDR a cada 1 s.

O botão ou configuração ECO do servidor principal não altera automaticamente
o processo isolado de favoritos: este usa `ffmpeg_command` próprio, ainda
configurado para recodificar em 720p. A preparação do modo ECO também precisa
resolver a normalização de PCR: no teste físico anterior, a bridge encerrou
fluxos com `pcr_no_null_slot`. Não publicar uma mudança de canal ou de
resolução sem verificar esse comportamento e a continuidade na TV.

Próximo ensaio recomendado: manter os IDs RJ, preparar um fluxo 1080p em
ambiente isolado, medir PCR/bitstream e estabilidade sob carga, e só então
solicitar que a TV seja ligada para testar Record e SBT. Preservar os modos
atuais e o procedimento de retorno.
