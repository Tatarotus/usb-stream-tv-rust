#!/usr/bin/env bash
# Structural validation of a mixed-media (Live + VOD) deployment package.
#
#   uso: scripts/validate_mixed_package.sh <PACKAGE_DIR>
#
# `sha256sum` alone proves nothing about the NTFS structure inside the
# template. This script therefore:
#
#   1. checks every SHA256SUMS entry;
#   2. expands favorites_template.sparse.gz into a real sparse image with the
#      host `sparse_unpack` binary;
#   3. runs the host `fuse_ntfs` in `test` target mode, which executes the same
#      startup verifiers used on the tablet: Layout::parse, Package::verify
#      (template archive + layout digests, package_id, path coverage),
#      Manifest::from_json/validate, build_runtime_files and
#      verify_template_image (reads every MFT record of the expanded image and
#      compares its $DATA runlist/size with the layout). It must report
#      "<N> arquivo(s)" and the expected "manifesto=<id>";
#   4. runs it again with --mixed-vod-address/--mixed-vod-allowlist so
#      parse_allowlist validates the real vod_allowlist.json against the
#      manifest sizes, and then reaches the mount stage.
#
# The only token written is a 48-byte dummy value in a private 0600 file inside
# the temporary directory; it is never printed. No real credential is used or
# read. The temporary directory and sparse image are removed on exit.
#
# What this still does NOT prove: that the Samsung MStar decoder enumerates,
# mounts and plays the volume over the physical USB/SCSI path.
set -euo pipefail

usage() {
    echo "uso: $0 <PACKAGE_DIR>" >&2
    exit 64
}

fail() {
    echo "[x] $*" >&2
    exit 1
}

ok() {
    echo "[ok] $*"
}

[ "$#" -eq 1 ] || usage
[ -d "$1" ] || {
    echo "[x] diretório inexistente: $1" >&2
    exit 2
}
PKG="$(cd "$1" && pwd)"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FUSE_BIN="$ROOT/target/release/fuse_ntfs"
UNPACK_BIN="$ROOT/target/release/sparse_unpack"

EXPECTED_FILES="${EXPECTED_FILES:-122}"
EXPECTED_LIVE="${EXPECTED_LIVE:-2}"
EXPECTED_VOD="${EXPECTED_VOD:-120}"
VOD_ADDRESS="${VOD_ADDRESS:-127.0.0.1:18191}"

REQUIRED=(
    favorites_manifest.json
    favorites_layout.json
    favorites_package.json
    vod_allowlist.json
    SHA256SUMS
    favorites_template.sparse.gz
)
for name in "${REQUIRED[@]}"; do
    [ -f "$PKG/$name" ] || fail "artefato obrigatório ausente: $name (em $PKG)"
done

for tool in sha256sum gzip python3 stat wc grep; do
    command -v "$tool" >/dev/null 2>&1 || fail "ferramenta ausente: $tool"
done

# ---- host binaries (build when missing or older than the sources) ----------
# A stale host binary would run stale verifiers, so the age check is part of
# the proof: the script must never validate a package with an outdated
# fuse_ntfs/sparse_unpack.
needs_build() {
    local bin="$1"
    shift
    if [ ! -x "$bin" ]; then
        return 0
    fi
    [ -n "$(find "$@" -newer "$bin" -print -quit 2>/dev/null)" ]
}

REBUILD=0
if needs_build "$FUSE_BIN" "$ROOT/crates/fuse_ntfs/src" \
    "$ROOT/crates/fuse_ntfs/Cargo.toml" "$ROOT/Cargo.toml"; then
    REBUILD=1
fi
if needs_build "$UNPACK_BIN" "$ROOT/crates/sparse_unpack/src" \
    "$ROOT/crates/sparse_unpack/Cargo.toml" "$ROOT/Cargo.toml"; then
    REBUILD=1
fi
if [ "$REBUILD" = 1 ]; then
    command -v cargo >/dev/null 2>&1 || fail "cargo ausente e binários host desatualizados"
    echo "[..] compilando binários host (release, offline)"
    ( cd "$ROOT" && cargo build -p fuse_ntfs --release --offline ) ||
        fail "falha ao compilar fuse_ntfs"
    ( cd "$ROOT" && cargo build -p sparse_unpack --release --offline ) ||
        fail "falha ao compilar sparse_unpack"
fi
[ -x "$FUSE_BIN" ] || fail "binário host ausente: $FUSE_BIN"
[ -x "$UNPACK_BIN" ] || fail "binário host ausente: $UNPACK_BIN"

json_field() {
    python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2], ""))
except Exception:
    print("")' "$1" "$2"
}

json_task_count() {
    python3 -c 'import json,sys
print(len(json.load(open(sys.argv[1], encoding="utf-8")).get("tasks", [])))' "$1"
}

# ---- 1. hashes -------------------------------------------------------------
( cd "$PKG" && sha256sum -c --quiet SHA256SUMS ) || fail "SHA256SUMS não confere"
hash_count="$(grep -c -E '^[0-9a-f]{64}  ' "$PKG/SHA256SUMS" || true)"
[ "$hash_count" -ge "${#REQUIRED[@]}" ] ||
    fail "SHA256SUMS tem apenas $hash_count entradas"
ok "SHA256SUMS: $hash_count hashes conferem"

EXPECTED_MANIFEST="${MIXED_EXPECT_MANIFEST:-$(json_field "$PKG/favorites_manifest.json" manifest_id)}"
[ -n "$EXPECTED_MANIFEST" ] || fail "favorites_manifest.json sem manifest_id"
if [ -f "$PKG/package_summary.json" ]; then
    summary_id="$(json_field "$PKG/package_summary.json" manifest_id)"
    if [ -n "$summary_id" ] && [ "$summary_id" != "$EXPECTED_MANIFEST" ]; then
        fail "package_summary.json declara manifesto=$summary_id, manifesto real=$EXPECTED_MANIFEST"
    fi
fi
VOLUME_BYTES="$(json_field "$PKG/favorites_package.json" volume_bytes)"
[ -n "$VOLUME_BYTES" ] && [ "$VOLUME_BYTES" != "0" ] ||
    fail "favorites_package.json sem volume_bytes"

allow_tasks="$(json_task_count "$PKG/vod_allowlist.json")" ||
    fail "vod_allowlist.json ilegível"
[ "$allow_tasks" -eq "$EXPECTED_VOD" ] ||
    fail "allowlist tem $allow_tasks tasks, esperado $EXPECTED_VOD"
ok "allowlist: $allow_tasks tasks; manifesto=$EXPECTED_MANIFEST"

# ---- private workspace, always cleaned -------------------------------------
TMP="$(mktemp -d "${TMPDIR:-/tmp}/mixed-package-validate.XXXXXXXX")"
cleanup() {
    rm -rf -- "$TMP"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

TOKEN_FILE="$TMP/transport.token"
(
    umask 077
    # 48-byte dummy: obviously not a credential, only used to satisfy the
    # startup length/charset check. Never echoed.
    printf '%s' '000000000000000000000000000000000000000000000000' >"$TOKEN_FILE"
)
chmod 600 "$TOKEN_FILE"
[ "$(wc -c <"$TOKEN_FILE")" -eq 48 ] || fail "token dummy com tamanho inesperado"
ok "token dummy de validação: 48 bytes, arquivo 0600 (nunca impresso)"

# ---- 2. expand the SPARSE01 template into a real sparse image --------------
gzip -dc -- "$PKG/favorites_template.sparse.gz" | "$UNPACK_BIN" "$TMP/template.bin" \
    >"$TMP/unpack.log" 2>&1 ||
    fail "falha ao expandir favorites_template.sparse.gz ($(tail -n 2 "$TMP/unpack.log" | tr '\n' ' '))"
actual_bytes="$(stat -c %s "$TMP/template.bin")"
[ "$actual_bytes" = "$VOLUME_BYTES" ] ||
    fail "imagem expandida tem $actual_bytes bytes, layout declara $VOLUME_BYTES"
ok "imagem NTFS expandida: $VOLUME_BYTES bytes virtuais ($(du -h "$TMP/template.bin" | cut -f1) físicos)"

# ---- 3/4. host startup verifiers ------------------------------------------
timeout_cmd=()
if command -v timeout >/dev/null 2>&1; then
    timeout_cmd=(timeout 60)
fi

RUN_STATUS=0
run_fuse() {
    local tag="$1"
    shift
    set +e
    ${timeout_cmd[@]+"${timeout_cmd[@]}"} "$FUSE_BIN" "$TMP/mnt" test "$TMP/template.bin" \
        --favorites-layout="$PKG/favorites_layout.json" \
        --favorites-package="$PKG/favorites_package.json" \
        --favorites-manifest="$PKG/favorites_manifest.json" \
        --favorites-template-archive="$PKG/favorites_template.sparse.gz" \
        --favorites-template-image="$TMP/template.bin" \
        --favorites-session=validate \
        --favorites-media=127.0.0.1:9 \
        --favorites-token-file="$TOKEN_FILE" \
        "$@" >"$TMP/$tag.out" 2>"$TMP/$tag.err"
    RUN_STATUS=$?
    set -e
}

# Rejects any startup refusal or unexpected error while accepting only the
# documented terminal states (see the case statements below).
check_output() {
    local tag="$1"
    local err="$TMP/$tag.err"
    grep -Fq -- "Favoritos: $EXPECTED_FILES arquivo(s)" "$err" ||
        fail "$tag: não reportou '$EXPECTED_FILES arquivo(s)': $(tail -n 3 "$err" | tr '\n' ' ')"
    grep -Fq -- "manifesto=$EXPECTED_MANIFEST," "$err" ||
        fail "$tag: manifesto divergente (esperado $EXPECTED_MANIFEST)"
    if grep -Fq -- '[x] Favoritos' "$err"; then
        fail "$tag: startup rejeitou o pacote: $(grep -F -- '[x] Favoritos' "$err" | head -n 1)"
    fi
    grep -E -- '^\[x\]' "$err" >"$TMP/$tag.refusals" || true
    if grep -v -F -- 'VOD misto exige --mixed-vod-address' "$TMP/$tag.refusals" \
        >"$TMP/$tag.unexpected" && [ -s "$TMP/$tag.unexpected" ]; then
        fail "$tag: erro inesperado: $(head -n 1 "$TMP/$tag.unexpected")"
    fi
    grep -E -- '^Error:' "$err" >"$TMP/$tag.errors" || true
    if grep -v -F -- 'mount failed' "$TMP/$tag.errors" \
        >"$TMP/$tag.errors.bad" && [ -s "$TMP/$tag.errors.bad" ]; then
        fail "$tag: erro inesperado: $(head -n 1 "$TMP/$tag.errors.bad")"
    fi
}

# Run 1: package verification only. With 120 VOD entries the binary refuses to
# wire the mixed backend without --mixed-vod-address (exit 2) *after* the whole
# package was validated; that refusal is the deterministic terminal state here.
run_fuse run1
check_output run1
case "$RUN_STATUS" in
    0 | 124)
        ok "run1: pacote validado; montagem alcançada"
        ;;
    2)
        grep -Fq -- '[x] VOD misto exige --mixed-vod-address' "$TMP/run1.err" ||
            fail "run1: exit 2 sem a recusa esperada de --mixed-vod-address: $(tail -n 2 "$TMP/run1.err" | tr '\n' ' ')"
        ok "run1: layout+pacote+manifesto+template+imagem validados (VOD não configurado)"
        ;;
    1)
        grep -Fq -- 'mount failed' "$TMP/run1.err" ||
            fail "run1: exit 1 sem 'mount failed'"
        ok "run1: pacote validado; montagem falhou sem privilégio FUSE"
        ;;
    *)
        fail "run1: exit inesperado $RUN_STATUS"
        ;;
esac

# Run 2: same, plus the real allowlist. Reaching the mount stage proves
# parse_allowlist accepted all $EXPECTED_VOD tasks with manifest-equal sizes.
run_fuse run2 \
    --mixed-vod-address="$VOD_ADDRESS" \
    --mixed-vod-allowlist="$PKG/vod_allowlist.json"
check_output run2
case "$RUN_STATUS" in
    0 | 124)
        ok "run2: allowlist aceita; validação concluída e montagem alcançada"
        ;;
    1)
        grep -Fq -- 'mount failed' "$TMP/run2.err" ||
            fail "run2: exit 1 sem 'mount failed'"
        grep -Eq -- 'Operation not permitted|Permission denied' "$TMP/run2.err" ||
            fail "run2: exit 1 sem erro de permissão esperado: $(tail -n 2 "$TMP/run2.err" | tr '\n' ' ')"
        ok "run2: allowlist aceita; montagem recusada sem privilégio FUSE (esperado)"
        ;;
    2)
        fail "run2: exit 2 — verificador recusou pacote/allowlist: $(grep -F -- '[x]' "$TMP/run2.err" | head -n 1)"
        ;;
    *)
        fail "run2: exit inesperado $RUN_STATUS"
        ;;
esac

ok "$EXPECTED_FILES arquivos (live=$EXPECTED_LIVE vod=$EXPECTED_VOD), manifesto=$EXPECTED_MANIFEST"
echo "RESULTADO: PACOTE VALIDADO (hashes + layout + manifesto + allowlist + estrutura NTFS da imagem)"
echo "NÃO PROVADO: montagem/enumeração física pelo decodificador Samsung (validação de hardware)."