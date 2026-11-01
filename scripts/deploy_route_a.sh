#!/bin/sh
# =============================================================================
# deploy_route_a.sh — Rota A: migrar para TV/<categoria>/ e publicar os aprovados
#
# Executa apenas o DEPLOY e a MIGRAÇÃO (nada de publicar geração). A publicação
# fica para o clique no painel, numa janela confirmada.
#
# Etapas (cada uma verificada; qualquer falha interrompe sem seguir adiante):
#   0. pré-requisitos locais: arquivos presentes + export produz exatamente os
#      canais aprovados (comparação por ID, não por nome)
#   1. backup datado no VPS (scripts + config)
#   2. envio in-place (preserva inode dos bind mounts) de:
#        scripts/publication_lock.py            (novo)
#        scripts/mixed_bridge.py                (lock SHARED)
#        scripts/mixed_generation_service.py    (guarda D9)
#        scripts/migrate_export_to_tv.sh        (janela + lock EXCLUSIVE)
#        config/live_favorites_export_tv.json   (8 canais aprovados)
#   3. reinício APENAS da bridge e conferência dos 5 leitores
#   4. migração: check -> apply (MIGRATE_CONFIRM_WINDOW=1)
#   5. relatório final do estado
#
# A TV não é tocada até o passo 4; o passo 3 reinicia um daemon (não mexe em
# tablet, USB nem no manager que serve a TV). Publicar é passo separado.
#
# Uso:
#   sh scripts/deploy_route_a.sh            # dry-run: só verifica, não muda nada
#   DEPLOY_CONFIRM=1 sh scripts/deploy_route_a.sh   # aplica
# =============================================================================
set -eu
cd "$(dirname "$0")/.." || exit 1

REMOTE=${REMOTE:-oracle}
APP=${APP:-/opt/usb-stream-tv}
CONTAINER=${CONTAINER:-usb-stream-tv}
EXPORT=config/live_favorites_export_tv.json
FILES="scripts/publication_lock.py scripts/mixed_bridge.py scripts/mixed_generation_service.py scripts/migrate_export_to_tv.sh"
CONFIRM=${DEPLOY_CONFIRM:-0}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

say() { echo "[deploy] $*"; }
die() { say "$*"; exit 1; }

# ---------------------------------------------------------------- 0. pre-flight
for f in $FILES "$EXPORT"; do [ -f "$f" ] || die "arquivo ausente: $f"; done
say "arquivos locais presentes"
python3 - "$EXPORT" <<'PY' || die "export invalido"
import json, sys
export = json.loads(open(sys.argv[1]).read())
approved = [i for i in export["identities"] if i.get("status") == "approved"]
if len(approved) != 8:
    print("esperado 8 canais aprovados, encontrado %d" % len(approved)); raise SystemExit(1)
ids = sorted((i.get("chosen") or {}).get("channel_id") for i in approved)
root = export.get("live_root")
if root != "TV":
    print("live_root != TV (%r)" % root); raise SystemExit(1)
print("export ok: live_root=TV, 8 aprovados, ids=%s" % ",".join(ids))
PY
say "versao local do migrador: $(sha256sum scripts/migrate_export_to_tv.sh | cut -c1-16)"
say "versao local da bridge : $(sha256sum scripts/mixed_bridge.py | cut -c1-16)"

say "--- estado atual do VPS (leitura) ---"
ssh -o BatchMode=yes "$REMOTE" "sudo grep -o -- '--export-config [^ ]*' /etc/systemd/system/usb-stream-tv-mixed-bridge.service | head -1; sudo test -d $APP/.deploy-route-a && echo 'backup anterior existe' || true"

if [ "$CONFIRM" != 1 ]; then
    say "DRY-RUN: nada foi alterado. Rode com DEPLOY_CONFIRM=1 para aplicar."
    exit 0
fi

# ------------------------------------------------------------------- 1. backup
say "1. backup em $APP/.backup-route-a-$STAMP"
ssh -o BatchMode=yes "$REMOTE" "set -e
  sudo mkdir -p $APP/.backup-route-a-$STAMP/scripts $APP/.backup-route-a-$STAMP/config
  for f in publication_lock.py mixed_bridge.py mixed_generation_service.py migrate_export_to_tv.sh; do
      sudo test -f $APP/scripts/\$f && sudo cp -a $APP/scripts/\$f $APP/.backup-route-a-$STAMP/scripts/ || true
  done
  sudo cp -a $APP/config/live_favorites_export_tv.json $APP/.backup-route-a-$STAMP/config/ 2>/dev/null || true
  sudo cp -a /etc/systemd/system/usb-stream-tv-mixed-bridge.service $APP/.backup-route-a-$STAMP/ || true
  echo backup-ok"

# ------------------------------------------------------------ 2. envio in-place
say "2. enviando arquivos (in-place, preservando inode)"
for f in $FILES; do
    base=$(basename "$f")
    ssh -o BatchMode=yes "$REMOTE" "sudo tee $APP/$f > /dev/null" < "$f" || die "falhou o envio de $f"
    say "   $f -> $APP/$f"
done
ssh -o BatchMode=yes "$REMOTE" "sudo tee $APP/$EXPORT > /dev/null" < "$EXPORT" || die "falhou o envio do export"
say "   $EXPORT -> $APP/$EXPORT"

# a copia do service tambem precisa existir dentro do container (ele roda de la)
say "2b. sincronizando o service no container"
ssh -o BatchMode=yes "$REMOTE" "sudo docker cp $APP/scripts/mixed_generation_service.py $CONTAINER:/app/scripts/mixed_generation_service.py && echo container-ok" || say "   (aviso: copia para o container falhou)"

# ------------------------------------------ 3. ociosidade antes da migracao
say "3. conferindo ociosidade antes da migracao (o restart e feito POR ELA, sob o lock)"
# O reinicio que carrega o codigo novo acontece dentro do `apply` da migracao,
# que segura o lock EXCLUSIVO e drena o spool antes de parar a bridge. Reiniciar
# aqui, fora do lock, seria justamente a corrida que o protocolo elimina.
ssh -o BatchMode=yes "$REMOTE" "set -e
  spool=/opt/containers/apps/usb-stream-tv/vod_cache/.mixed_bridge
  in_flight=\$(sudo ls \$spool/requests \$spool/processing 2>/dev/null | wc -l)
  pubs=\$(ps -eo args --no-headers | grep -c '[m]ixed_generation_service.py' || true)
  echo \"   spool: \$in_flight item(ns) | publicadores vivos: \$pubs\"
  pid=\$(sudo systemctl show -p MainPID --value usb-stream-tv-mixed-bridge.service)
  echo \"   bridge atual pid=\$pid (codigo antigo; sera reiniciada pelo apply)\""

# ------------------------------------------------------------- 4. migracao
say "4. migracao do export (janela de quiescencia + lock EXCLUSIVO)"
ssh -o BatchMode=yes "$REMOTE" "set -e
  cd $APP
  sudo env MIGRATE_CONFIRM_WINDOW=1 sh scripts/migrate_export_to_tv.sh check || true
  sudo env MIGRATE_CONFIRM_WINDOW=1 sh scripts/migrate_export_to_tv.sh apply"

# ------------------------------------------------------------------ 5. relatorio
say "5. estado final"
ssh -o BatchMode=yes "$REMOTE" "set -e
  sudo grep -o -- '--export-config [^ ]*' /etc/systemd/system/usb-stream-tv-mixed-bridge.service | head -1
  sudo grep -o -- '--export-config [^ ]*' /etc/systemd/system/usb-stream-tv-mixed-prepare.service | head -1
  echo \"timer=\$(systemctl is-active usb-stream-tv-mixed-prepare.timer) bridge=\$(systemctl is-active usb-stream-tv-mixed-bridge.service)\""
say "DEPLOY CONCLUIDO. Publicar e um passo separado: clique em NA TV numa janela confirmada."