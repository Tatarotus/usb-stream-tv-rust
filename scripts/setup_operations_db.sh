#!/usr/bin/env bash
# =============================================================================
# setup_operations_db.sh — cria o banco de operações COMPARTILHADO
#
# POR QUE ESTE SCRIPT EXISTE
#   O servidor roda dentro do container (`/app/vod_cache/...`) e o publisher roda
#   no host (`/opt/containers/apps/usb-stream-tv/vod_cache/...`). São DOIS
#   caminhos para o MESMO arquivo. Se cada lado apontar para um arquivo próprio,
#   o publisher nunca encontra a operação criada pelo endpoint e o painel passa a
#   mostrar publicações órfãs — o defeito que este script existe para impedir.
#
# O QUE FAZ
#   1. cria o diretório e o banco na raiz compartilhada (fora de bind-mount de
#      arquivo individual, para sobreviver a recriação do container);
#   2. aplica dono e modo 0600;
#   3. roda a migração do módulo (cria o schema), sem inventar schema à mão;
#   4. PROVA que host e container veem o mesmo inode, comparando um marcador
#      gravado no arquivo — não basta o caminho parecer certo;
#   5. imprime as duas variáveis que precisam ser definidas, uma por lado.
#
# O QUE NÃO FAZ
#   Não reinicia nada, não publica, não toca no tablet nem no USB. Padrão é
#   dry-run; escrever exige CONFIRM_SETUP=1.
#
# Uso:
#   sh scripts/setup_operations_db.sh                 # dry-run
#   CONFIRM_SETUP=1 sh scripts/setup_operations_db.sh # cria
# =============================================================================
set -eu

APP_HOST=${APP_HOST:-/opt/containers/apps/usb-stream-tv}
APP_CONTAINER=${APP_CONTAINER:-/app}
CONTAINER=${CONTAINER:-usb-stream-tv}
REMOTE=${REMOTE:-oracle}
# Raiz compartilhada: diretório, nunca um arquivo isolado — um bind de arquivo
# individual sobrevive mal a recriação do container.
REL_DIR=${REL_DIR:-vod_cache/.operations}
DB_NAME=${DB_NAME:-operations.db}
CONFIRM=${CONFIRM_SETUP:-0}

HOST_DIR="$APP_HOST/$REL_DIR"
HOST_DB="$HOST_DIR/$DB_NAME"
CONTAINER_DB="$APP_CONTAINER/$REL_DIR/$DB_NAME"
MARKER_FILE="$HOST_DIR/.shared-marker"

say() { echo "[opsdb] $*"; }
die() { say "ERRO: $*"; exit 1; }
run() { ssh -o BatchMode=yes "$REMOTE" "sudo bash -s"; }

say "1/5 caminhos que precisam apontar para o MESMO arquivo"
say "  host      : $HOST_DB"
say "  container : $CONTAINER_DB"
say "  variável (host/publisher)      : MIXED_OPERATIONS_DB=$HOST_DB"
say "  variável (container/servidor)  : MIXED_OPERATIONS_DB=$CONTAINER_DB"

if [ "$CONFIRM" != 1 ]; then
    say "2/5 criar diretório e banco   [DRY-RUN]"
    say "3/5 dono e modo 0600          [DRY-RUN]"
    say "4/5 migração do schema        [DRY-RUN]"
    say "5/5 prova host == container   [DRY-RUN]"
    echo
    say "NADA FOI ALTERADO."
    say "para criar: CONFIRM_SETUP=1 sh $0"
    exit 0
fi

say "2/5 criando diretório e banco no host"
run <<EOF
set -eu
mkdir -p "$HOST_DIR"
chmod 700 "$HOST_DIR"
[ -f "$HOST_DB" ] || : > "$HOST_DB"
echo "  banco: \$(stat -c '%U:%G %a' "$HOST_DB")"
EOF

say "3/5 aplicando dono e modo 0600"
run <<EOF
set -eu
# Mesmo dono do state file do publisher (o serviço roda como esse usuário).
OWNER=\$(stat -c '%U:%G' /opt/containers/apps/usb-stream-tv/vod_cache/.mixed_state.json 2>/dev/null || echo sam:sam)
chown "\$OWNER" "$HOST_DIR" "$HOST_DB"
chmod 700 "$HOST_DIR"
chmod 600 "$HOST_DB"
echo "  dono/modo: \$(stat -c '%U:%G %a' "$HOST_DB")"
EOF

say "4/5 rodando a migração do módulo (schema real, não inventado)"
run <<EOF
set -eu
cd /opt/usb-stream-tv 2>/dev/null || cd /opt/containers/apps/usb-stream-tv
MIXED_OPERATIONS_DB="$HOST_DB" python3 - <<'PY'
import os, sys
sys.path.insert(0, "scripts")
import mixed_operations as ops
store = ops.OperationStore(os.environ["MIXED_OPERATIONS_DB"])
print("  schema_version:", ops.SCHEMA_VERSION)
print("  operacoes hoje :", len(store.unresolved()), "pendentes")
PY
EOF

say "5/5 provando que host e container veem o MESMO inode"
# Um marcador gravado no host e lido pelo container: se os caminhos apontassem
# para arquivos diferentes, o marcador não apareceria.
run <<EOF
set -eu
STAMP="opsdb-\$(date +%s)-\$\$"
python3 - "\$STAMP" <<'PY'
import os, sqlite3, sys
db = os.environ.get("DB") or "$HOST_DB"
conn = sqlite3.connect(db)
conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
conn.execute("INSERT INTO meta (key, value) VALUES ('shared_marker', ?) "
             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (sys.argv[1],))
conn.commit(); conn.close()
PY
echo "  marcador gravado no host: \$STAMP"
if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    GOT=\$(docker exec "$CONTAINER" python3 -c "
import sqlite3
c = sqlite3.connect('$CONTAINER_DB')
print(c.execute(\"SELECT value FROM meta WHERE key='shared_marker'\").fetchone()[0])
" 2>/dev/null || echo unreadable)
    echo "  marcador lido no container: \$GOT"
    if [ "\$GOT" = "\$STAMP" ]; then
        echo "  => MESMO ARQUIVO (host e container)"
    else
        echo "  => CAMINHOS DIFERENTES: NÃO configure os dois lados assim" >&2
        exit 1
    fi
else
    echo "  container '$CONTAINER' ausente: prova adiada, não presumida"
fi
EOF

echo
say "concluído. Defina as duas variáveis (uma por lado) e recrie o container"
say "na janela autorizada. Este script NÃO reiniciou nada."
