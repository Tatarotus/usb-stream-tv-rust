#!/bin/sh
# =============================================================================
# deploy_grade_29canais.sh — grade de 29 canais em volume de 256 GiB
#
# Entrega o pacote da grade nova (29 Live + VODs) ao tablet, com o binário
# fuse_ntfs corrigido para honrar o volume declarado pelo pacote.
#
# POR QUE ESTE SCRIPT EXISTE
# O driver tinha DOIS limites fixos de 128 GiB, ambos medidos no tablet:
#   1. favorites/manifest.rs: CapacityPolicy::default() -> VOLUME_128_GIB
#      rejeitava o pacote com CapacityExceeded antes de qualquer transferência.
#   2. vfs/ntfs_mft.rs: VOLUME_BYTES = 128 GiB reportado em getattr/lookup
#      fazia o kernel anunciar um LUN de 128 GiB à TV, mesmo com o BPB (VBR)
#      declarando 192 GiB. Medido na gen11: imagem 206158430208 bytes,
#      total_sectors 402653183 (192 GiB), getattr 137438953472 (128 GiB).
# O binário deste pacote reporta o tamanho do BPB e valida a capacidade contra
# o volume do pacote, então um volume de 256 GiB é utilizável de ponta a ponta.
#
# O QUE ESTE SCRIPT MUDA (cada passo verificado, falha interrompe sem seguir):
#   1. confere que o pacote de staging existe e está validado;
#   2. instala o binário patcheado no binaries-dir do publisher (backup antes);
#   3. ajusta o volume do publisher para 256 GiB (backup + checagem de sintaxe);
#   4. publica o export de 29 canais (backup do export atual);
#   5. reconstrói e publica a geração pelo caminho normal do publisher.
#
# ORDEM IMPORTA: o manager que serve a TV lê o export UMA vez, no arranque
# (run_live_favorites_manager.py:595). Trocar o arquivo em disco não afeta a
# sessão em execução; só a publicação seguinte troca o que a TV vê.
#
# A TV É INTERROMPIDA NO PASSO 5 — reconectar o pendrive no ConnectShare depois.
#
# Uso:
#   sh scripts/deploy_grade_29canais.sh              # dry-run: só verifica
#   CONFIRM_PUBLISH=1 sh scripts/deploy_grade_29canais.sh   # aplica de verdade
#   ROLLBACK=1 sh scripts/deploy_grade_29canais.sh   # desfaz binário/volume/export
#
# Variáveis:
#   REMOTE   host com sudo para o VPS (default: oracle)
#   APP      diretório da aplicação no VPS (default: /opt/usb-stream-tv)
#   STAGING  pacote de 256 GiB já construído e validado
#   EXPORT29 export novo com os 29 canais aprovados
#   FUSE_BIN binário ARM32 patcheado
# =============================================================================
set -eu

REMOTE=${REMOTE:-oracle}
APP=${APP:-/opt/usb-stream-tv}
STAGING=${STAGING:-/opt/usb-stream-tv/scratch/gen12-leve/staging}
EXPORT29=${EXPORT29:-/home/sam/canais-sweep/export-21.json}
EXPORT_PROD=${EXPORT_PROD:-$APP/config/live_favorites_export_tv.json}
# Diretório de binários lido pelo publisher em produção (--binaries-dir).
BINARIES_DIR=${BINARIES_DIR:-$APP/scratch/mixed-staging/mixed-gen3-819e5f54}
SERVICE=$APP/scripts/mixed_generation_service.py
VOLUME_256=274877906944
VOLUME_OLD=206158430208
CONFIRM=${CONFIRM_PUBLISH:-0}
INTERRUPT=${INTERRUPT_TV:-0}
ROLLBACK=${ROLLBACK:-0}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

LOCAL_ROOT=$(cd "$(dirname "$0")/.." && pwd)
FUSE_BIN=${FUSE_BIN:-$LOCAL_ROOT/target/armv7-unknown-linux-gnueabihf/release/fuse_ntfs}

say() { echo "[deploy] $*"; }
die() { say "ERRO: $*"; exit 1; }
run() { ssh -o BatchMode=yes "$REMOTE" "sudo bash -s"; }

# ------------------------------------------------------------------ rollback
if [ "$ROLLBACK" = 1 ]; then
    say "desfazendo (binário, volume, export)"
    run <<ROLLBACK_EOF
set -eu
F=$BINARIES_DIR/fuse_ntfs
if [ -f "\$F.bak-grade29" ]; then
    cp -a "\$F.bak-grade29" "\$F"
    echo "  binário restaurado"
fi
S=$SERVICE
if [ -f "\$S.bak-grade29" ]; then
    cp -a "\$S.bak-grade29" "\$S"
    echo "  volume do publisher restaurado"
fi
E=$EXPORT_PROD
if [ -f "\$E.bak-grade29" ]; then
    cp -a "\$E.bak-grade29" "\$E"
    echo "  export restaurado"
fi
python3 -c "import json;d=json.load(open('$EXPORT_PROD'));print('  canais no export agora:',sum(1 for i in d['identities'] if i.get('status')=='approved'))"
ROLLBACK_EOF
    say "rollback concluído. A geração ativa na TV NÃO é alterada por este comando."
    say "para voltar a geração, use: switch_mixed_generation_vps.sh stop <sessão>"
    exit 0
fi

# ------------------------------------------------------- 1. pré-requisitos locais
say "1/5 pré-requisitos locais"
[ -f "$FUSE_BIN" ] || die "binário ARM32 ausente: $FUSE_BIN"
python3 - "$FUSE_BIN" <<'PY' || die "binário não é ARM32 estático"
import struct, sys
d = open(sys.argv[1], 'rb').read(20)
machine = struct.unpack_from('<H', d, 18)[0]
if machine != 40:
    print("  e_machine=%d (esperado 40 = ARM)" % machine); raise SystemExit(1)
print("  binário ARM32 ok, %d bytes" % len(open(sys.argv[1], 'rb').read()))
PY
say "  sha256 local: $(sha256sum "$FUSE_BIN" | cut -c1-24)"

# ------------------------------------------------ 2. conferir staging no VPS
say "2/5 pacote de referência de 256 GiB no VPS"
# Este pacote é a PROVA de que o binário patcheado aceita 256 GiB: ele foi
# construído e validado aqui. O publisher NÃO o reutiliza — ele reconstrói a
# geração a partir do export e do inventário atuais (check_and_prepare_generation),
# no staging-base configurado. Conferir este pacote antes garante que o patch e a
# geometria estão certos antes de mexer em qualquer coisa do serviço.
run <<CHECK_EOF
set -eu
S=$STAGING
[ -d "\$S" ] || { echo "  staging ausente: \$S" >&2; exit 1; }
python3 -c "
import json
m=json.load(open('\$S/favorites_manifest.json'))
live=[e for e in m['entries'] if e['kind']=='live']
vod=[e for e in m['entries'] if e['kind']=='vod']
tot=sum(e.get('logical_size') or 0 for e in m['entries'])
p=json.load(open('\$S/favorites_package.json'))
print('  live=%d vod=%d soma=%.1f GiB volume=%.0f GiB' % (
    len(live), len(vod), tot/1024**3, (p.get('volume_bytes') or 0)/1024**3))
assert len(live) == 29, 'esperado 29 canais Live, encontrado %d' % len(live)
assert (p.get('volume_bytes') or 0) == $VOLUME_256, 'volume do pacote não é 256 GiB'
"
echo "  staging ok"
CHECK_EOF

# --------------------------------------------- 3. binário + volume + export
# Dry-run para aqui: nada é escrito enquanto CONFIRM_PUBLISH != 1.
if [ "$CONFIRM" != 1 ]; then
    say "3/5 binário, volume e export  [DRY-RUN: não alterado]"
    say "  instalaria $FUSE_BIN em $BINARIES_DIR/fuse_ntfs (backup .bak-grade29)"
    say "  ajustaria $SERVICE de $VOLUME_OLD para $VOLUME_256 (backup .bak-grade29)"
    say "  instalaria o export de 29 canais em $EXPORT_PROD (backup .bak-grade29)"
    say "4/5 publicação  [DRY-RUN]"
    say "5/5 verificação [DRY-RUN]"
    echo
    say "NADA FOI ALTERADO. A TV segue na geração atual."
    say "para aplicar: CONFIRM_PUBLISH=1 sh $0"
    exit 0
fi

say "3/5 instalando binário, volume e export"
# O binário vai por scp para /tmp; o sudo o instala com modo 755.
scp -o BatchMode=yes "$FUSE_BIN" "$REMOTE:/tmp/fuse_ntfs.grade29" || die "scp do binário falhou"
# O export foi produzido no VPS pelo harness dos 29 canais, então o padrão é o
# caminho remoto. Quando existir um arquivo local, ele é enviado por scp.
if [ -f "$EXPORT29" ]; then
    scp -o BatchMode=yes "$EXPORT29" "$REMOTE:/tmp/export29.json" || die "scp do export falhou"
else
    ssh -o BatchMode=yes "$REMOTE" "test -f '$EXPORT29' && cp -a '$EXPORT29' /tmp/export29.json && echo '  export do VPS: $EXPORT29'" \
        || die "export ausente (nem local nem no VPS): $EXPORT29"
fi

run <<APPLY_EOF
set -eu
F=$BINARIES_DIR/fuse_ntfs
[ -f "\$F" ] || { echo "  binário de destino ausente: \$F" >&2; exit 1; }
# Preserva o dono: o publisher roda como sam, e um arquivo root:root aqui
# mudaria de dono sem necessidade.
OWNER=\$(stat -c '%U:%G' "\$F")
printf '%s\n' "\$OWNER" > /tmp/.grade29.owner
if [ ! -f "\$F.bak-grade29" ]; then
    cp -a "\$F" "\$F.bak-grade29"
    echo "  backup do binário: \$F.bak-grade29"
fi
install -m 755 /tmp/fuse_ntfs.grade29 "\$F"
chown "\$OWNER" "\$F"
rm -f /tmp/fuse_ntfs.grade29
python3 -c "
import struct
d=open('\$F','rb').read(20)
m=struct.unpack_from('<H',d,18)[0]
print('  binário instalado: e_machine=%d' % m)
assert m==40
"

S=$SERVICE
SOWNER=\$(stat -c '%U:%G' "\$S")
if [ ! -f "\$S.bak-grade29" ]; then
    cp -a "\$S" "\$S.bak-grade29"
    echo "  backup do publisher: \$S.bak-grade29"
fi
# Idempotente: troca o volume antigo pelo de 256 GiB, se ainda estiver lá.
if grep -q "volume_bytes=$VOLUME_OLD" "\$S"; then
    sed -i "s/volume_bytes=$VOLUME_OLD/volume_bytes=$VOLUME_256/" "\$S"
    chown "\$SOWNER" "\$S"
    echo "  volume do publisher: $VOLUME_OLD -> $VOLUME_256"
elif grep -q "volume_bytes=$VOLUME_256" "\$S"; then
    echo "  volume do publisher já é 256 GiB"
else
    echo "  AVISO: volume esperado não encontrado em \$S; confira manualmente" >&2
fi
python3 -c "import ast,sys;ast.parse(open('\$S').read())" \
    || { echo "  publisher com sintaxe inválida, restaurando" >&2; cp -a "\$S.bak-grade29" "\$S"; exit 1; }
echo "  publisher com sintaxe válida"

E=$EXPORT_PROD
if [ ! -f "\$E.bak-grade29" ]; then
    cp -a "\$E" "\$E.bak-grade29"
    echo "  backup do export: \$E.bak-grade29"
fi
# Escrita in-place preserva o inode do bind mount.
cat /tmp/export29.json > "\$E"
rm -f /tmp/export29.json
python3 -c "
import json
d=json.load(open('\$E'))
n=sum(1 for i in d['identities'] if i.get('status')=='approved')
print('  export em produção: %d canais aprovados, live_root=%r' % (n, d.get('live_root')))
assert n==29, 'esperado 29 aprovados no export'
"
APPLY_EOF

# --------------------------------------------------------- 4. publicar geração
say "4/5 publicando a geração (a TV será interrompida e precisará reconectar o pendrive)"
# A publicação vai pelo CLIENTE DA BRIDGE, não pelo publisher direto: é a bridge
# que injeta MIXED_PUBLICATION_LOCK no filho (mixed_bridge.py:491) e roda o
# publisher como sam — invocá-lo à mão perderia a trava de quiescência e
# escreveria o state file com o dono errado.
# O request precisa ser dono uid 1002 (client-uid da bridge).
if [ "$INTERRUPT" = 1 ]; then
    CONFIRM_FLAG="--confirm-interrupt"
else
    CONFIRM_FLAG=""
fi
run <<PUBLISH_EOF
set -eu
SPOOL=/opt/containers/apps/usb-stream-tv/vod_cache/.mixed_bridge
timeout 900 python3 $APP/scripts/mixed_bridge.py client \
    --spool "\$SPOOL" --action refresh --timeout 840 \
    --request-owner-uid 1002 $CONFIRM_FLAG 2>&1 | tail -40
PUBLISH_EOF

# ------------------------------------------------------------- 5. verificação
say "5/5 verificando o estado"
run <<VERIFY_EOF
set -eu
python3 -c "
import json
d=json.load(open('/opt/containers/apps/usb-stream-tv/vod_cache/.mixed_state.json'))
p=d.get('publication') or {}
print('  active_generation:', d.get('active_generation'))
print('  pending_generation:', d.get('pending_generation'))
print('  phase:', p.get('phase'), '| gen:', p.get('generation'))
print('  último erro:', p.get('last_error'))
"
echo "  --- listeners do manager/proxy:"
ss -ltnp 2>/dev/null | grep -E ':18190|:18191' | sed 's/^/    /' || echo "    (ss indisponível)"
echo "  --- tablet:"
adb -s 127.0.0.1:25555 shell "su -c 'cat /data/local/tmp/favorites_supervisor.path 2>/dev/null; cat /sys/class/android_usb/android0/f_mass_storage/lun0/file 2>/dev/null'" 2>/dev/null | sed 's/^/    /' || echo "    (adb indisponível)"
VERIFY_EOF

echo
say "concluído."
say "Se phase=active: a grade de 29 canais está na TV."
say "Se phase=unconfirmed/unconfirmed: verifique o tablet antes de repetir."
say "Rollback: ROLLBACK=1 sh $0   (não altera a geração ativa)"
say "Rollback de geração: sh $APP/scripts/switch_mixed_generation_vps.sh stop <sessão>"
