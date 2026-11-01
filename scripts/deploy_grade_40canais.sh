#!/bin/sh
# =============================================================================
# deploy_grade_40canais.sh — grade de 40 canais (29 atuais + 10 novos + Globo RJ)
#
# PREPARADO E TESTADO. NÃO PUBLICA SOZINHO.
#
#   sh scripts/deploy_grade_40canais.sh                 # dry-run (padrão)
#   CONFIRM_PUBLISH=1 INTERRUPT_TV=1 sh scripts/...     # publica de verdade
#   ROLLBACK=1 sh scripts/deploy_grade_40canais.sh      # desfaz
#
# O QUE ESTE DEPLOY ENTREGA
#   10 canais novos, rotulados pela RESOLUÇÃO REAL DA FONTE:
#     1080p -> Globo News, HBO, TV Futura
#     720p  -> Band RJ, Rede TV, Record News, CNN Brasil, BBC World,
#              TV Cultura, Band News, Globo RJ
#   Os 720p ficam com "720" no nome. A saída continua sendo 1080p30 do
#   transpiler; rotular a fonte como 1080 seria vender upscale como nativo.
#
# PRÉ-REQUISITOS JÁ CUMPRIDOS (não refeitos aqui)
#   * binário fuse_ntfs ARM32 com o patch de volume (capacidade do pacote +
#     tamanho do BPB) já instalado no --binaries-dir do publisher;
#   * volume do publisher já em 256 GiB;
#   * pacote de 39 construído e VALIDADO em staging:
#       /opt/usb-stream-tv/scratch/gen14-prep/staging
#       RESULTADO: PACOTE VALIDADO (164 arquivos: 40 Live + 124 VOD)
#   * export de 39 em staging (produção permanece com 29 até o aval):
#       /opt/usb-stream-tv/scratch/gen14-prep/export40.json
#
# POR QUE O EXPORT SÓ ENTRA NO AVAL
#   O manager que serve a TV lê o export UMA vez, no arranque
#   (run_live_favorites_manager.py:595). Trocar o arquivo em disco não afeta a
#   sessão em curso, mas deixaria o disco à frente do processo: se o manager
#   reiniciasse por qualquer motivo antes da publicação, ele encontraria um
#   export de 39 com um manifesto de 29 e recusaria subir. Instalar e publicar
#   no mesmo passo elimina essa janela.
#
# A TV É INTERROMPIDA NO PASSO 3 e precisa reconectar o pendrive no ConnectShare.
# =============================================================================
set -eu

REMOTE=${REMOTE:-oracle}
APP=${APP:-/opt/usb-stream-tv}
PREP=$APP/scratch/gen14-prep
STAGING=${STAGING:-$PREP/staging}
EXPORT40=${EXPORT40:-$PREP/export40.json}
EXPORT_PROD=${EXPORT_PROD:-$APP/config/live_favorites_export_tv.json}
SPOOL=${SPOOL:-/opt/containers/apps/usb-stream-tv/vod_cache/.mixed_bridge}
STATE=${STATE:-/opt/containers/apps/usb-stream-tv/vod_cache/.mixed_state.json}
CONFIRM=${CONFIRM_PUBLISH:-0}
INTERRUPT=${INTERRUPT_TV:-0}
ROLLBACK=${ROLLBACK:-0}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

say() { echo "[deploy40] $*"; }
die() { say "ERRO: $*"; exit 1; }
run() { ssh -o BatchMode=yes "$REMOTE" "sudo bash -s"; }

# ------------------------------------------------------------------- rollback
if [ "$ROLLBACK" = 1 ]; then
    say "desfazendo export (o pacote e o binário continuam válidos)"
    run <<ROLLBACK_EOF
set -eu
E=$EXPORT_PROD
if [ -f "\$E.bak-grade40" ]; then
    cp -a "\$E.bak-grade40" "\$E"
    echo "  export restaurado"
else
    echo "  sem backup .bak-grade40: nada a restaurar" >&2
fi
python3 -c "import json;d=json.load(open('$EXPORT_PROD'));print('  canais no export agora:',sum(1 for i in d['identities'] if i.get('status')=='approved'))"
ROLLBACK_EOF
    say "rollback do export feito. A geração ativa na TV NÃO muda com este comando."
    say "para voltar a geração: sh $APP/scripts/switch_mixed_generation_vps.sh stop <sessão>"
    exit 0
fi

# ------------------------------------------------------- 1. pré-requisitos
say "1/4 conferindo o que já está pronto"
run <<CHECK_EOF
set -eu
S=$STAGING
[ -d "\$S" ] || { echo "  staging ausente: \$S" >&2; exit 1; }
python3 - <<'PY'
import json
S = "$STAGING"
m = json.load(open(S + "/favorites_manifest.json"))
p = json.load(open(S + "/favorites_package.json"))
live = [e for e in m["entries"] if e["kind"] == "live"]
vod = [e for e in m["entries"] if e["kind"] == "vod"]
tot = sum(e.get("logical_size") or 0 for e in m["entries"])
print("  pacote: live=%d vod=%d soma=%.1f GiB volume=%.0f GiB" % (
    len(live), len(vod), tot / 1024**3, (p.get("volume_bytes") or 0) / 1024**3))
assert len(live) == 40, "esperado 40 canais, encontrado %d" % len(live)
assert (p.get("volume_bytes") or 0) == 274877906944, "volume do pacote nao e 256 GiB"
PY
[ -f "$EXPORT40" ] || { echo "  export de 40 ausente: $EXPORT40" >&2; exit 1; }
python3 -c "
import json
d=json.load(open('$EXPORT40'))
ap=[i for i in d['identities'] if i.get('status')=='approved']
h={1080:0,720:0}
for i in ap:
    x=i['chosen']['native_height']
    h[x]=h.get(x,0)+1
print('  export de 40: %d aprovados (%d em 1080p, %d em 720p)' % (len(ap), h[1080], h[720]))
assert len(ap)==40
"
echo "  binário do publisher: \$(stat -c %s $APP/scratch/mixed-staging/mixed-gen3-819e5f54/fuse_ntfs 2>/dev/null) bytes"
# O volume é lido da constante OPERATIONAL_VOLUME_BYTES (com override por
# MIXED_VOLUME_BYTES), então conferimos o VALOR EFETIVO, não um literal — o
# literal antigo já não existe e um grep por ele dá falso negativo.
python3 - <<'PY'
import sys
sys.path.insert(0, "$APP/scripts")
import mixed_generation_service as m
gib = m.OPERATIONAL_VOLUME_BYTES / 1024**3
print("  volume efetivo do publisher: %.0f GiB" % gib)
assert int(gib) == 256, "volume do publisher nao e 256 GiB"
PY
CHECK_EOF

# --------------------------------------------------- 2. janela de publicação
if [ "$CONFIRM" != 1 ]; then
    say "2/4 publicação  [DRY-RUN: nada foi alterado]"
    say "  iria: backup do export atual (39) em $EXPORT_PROD.bak-grade40"
    say "  iria: instalar o export de 40 (40 canais) em $EXPORT_PROD"
    say "  iria: pedir refresh à bridge, com --confirm-interrupt"
    say "3/4 verificação [DRY-RUN]"
    echo
    say "A TV continua com 39 canais e o VOD em curso não foi tocado."
    say "para publicar: CONFIRM_PUBLISH=1 INTERRUPT_TV=1 sh \$0"
    exit 0
fi

say "2/4 instalando o export de 40 e publicando"
if [ "$INTERRUPT" != 1 ]; then
    CONFIRM_FLAG=""
    say "  AVISO: sem INTERRUPT_TV=1 a bridge devolve 'blocked' se a TV estiver tocando"
else
    CONFIRM_FLAG="--confirm-interrupt"
fi

run <<PUBLISH_EOF
set -eu
E=$EXPORT_PROD
# 1) backup do export atual (29) antes de qualquer troca
if [ ! -f "\$E.bak-grade40" ]; then
    cp -a "\$E" "\$E.bak-grade40"
    echo "  backup: \$E.bak-grade40"
fi
# 2) escrita in-place preserva o inode do bind mount
cp -a $EXPORT40 "\$E"
chown --reference="\$E.bak-grade40" "\$E" 2>/dev/null || true
python3 -c "
import json
d=json.load(open('\$E'))
n=sum(1 for i in d['identities'] if i.get('status')=='approved')
print('  export em produção: %d canais aprovados' % n)
assert n==40, 'esperado 40 no export de produção'
"
# 3) publica pela bridge (é ela que injeta a trava de quiescência no filho e
#    roda o publisher como sam, com sudo -n -E no switch)
BR=\$(pgrep -f "mixed_bridge.py serve" | head -1)
[ -n "\$BR" ] || { echo "  bridge nao encontrada" >&2; exit 1; }
timeout 1100 sudo -u sam bash -c '
  set -a
  tr "\0" "\n" < /proc/'"\$BR"'/environ 2>/dev/null | grep -E "^(SWITCH_ROOT|ENV_FILE|EXPORT_CONFIG_FILE|APP_DIR)=" > /tmp/penv39
  . /tmp/penv39; set +a
  rm -f /tmp/penv39
  python3 $APP/scripts/mixed_bridge.py client \
    --spool $SPOOL --action refresh --timeout 1000 --request-owner-uid 1002 '"$CONFIRM_FLAG"' 2>&1 | tail -20
'
PUBLISH_EOF

# ------------------------------------------------------------- 3. verificação
say "3/4 verificando o estado"
run <<VERIFY_EOF
set -eu
python3 -c "
import json
d=json.load(open('$STATE'))
p=d.get('publication') or {}
print('  active_generation:', d.get('active_generation'))
print('  active_session   :', d.get('active_session'))
print('  phase            :', p.get('phase'))
print('  last_error       :', p.get('last_error'))
"
echo "  current -> \$(readlink -f $APP/switch/current 2>/dev/null | xargs basename 2>/dev/null)"
ss -ltn 2>/dev/null | grep -E ':18190|:18191' | awk '{print "  listener: "\$4}'
echo "  --- tablet:"
adb -s 127.0.0.1:25555 shell "su -c 'cat /data/local/tmp/favorites_supervisor.path 2>/dev/null'" 2>/dev/null | sed 's/^/    /' || echo "    (adb indisponível)"
VERIFY_EOF

say "4/4 conferindo a grade que a TV lê"
run <<GRID_EOF
set -eu
P=\$(adb -s 127.0.0.1:25555 shell "su -c 'cat /data/local/tmp/favorites_supervisor.path 2>/dev/null'" 2>/dev/null | tr -d '\r')
adb -s 127.0.0.1:25555 shell "su -c 'cat '\$P'/favorites_manifest.json 2>/dev/null'" 2>/dev/null > /tmp/m39.json
python3 -c "
import json
m=json.load(open('/tmp/m39.json'))
live=[e for e in m['entries'] if e['kind']=='live']
vod=[e for e in m['entries'] if e['kind']=='vod']
print('  NA TV: live=%d vod=%d' % (len(live), len(vod)))
"
rm -f /tmp/m39.json
GRID_EOF

echo
say "concluído."
say "Se phase=active: a grade de 40 canais está na TV — reconecte o pendrive no ConnectShare."
say "Rollback do export: ROLLBACK=1 sh \$0"
say "Rollback da geração: sh $APP/scripts/switch_mixed_generation_vps.sh stop <sessão>"
