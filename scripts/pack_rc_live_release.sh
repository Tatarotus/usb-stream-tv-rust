#!/usr/bin/env bash
# Assemble an RC-Live release package from a tagged revision.
#
# Usage:
#   scripts/pack_rc_live_release.sh <revision> <output_dir> [base_package_dir]
#
#   revision          git revision/tag to package (e.g. rc-001-live-iter3)
#   output_dir        directory to create (must not already exist)
#   base_package_dir  an existing package that supplies the generated slate
#                     assets and the rollback set (the state to restore on a
#                     device). Default: rc-001-live-16d5991.
#
# The ARM binaries are built and verified first (via verify_rc_live_release.sh),
# so a package is never assembled from unverified artefacts. Everything else is
# copied from the pinned revision (source, host runtime, scripts, docs).
#
# Host-only: this builds and copies files. It never deploys, never touches a
# device, /data, the USB gadget or any provider.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REVISION="${1:-}"
OUT_DIR="${2:-}"
BASE_PKG="${3:-/home/sam/Code/rc001-release/rc-001-live-16d5991}"
if [ -z "${REVISION}" ] || [ -z "${OUT_DIR}" ]; then
    echo "usage: scripts/pack_rc_live_release.sh <revision> <output_dir> [base_package_dir]" >&2
    exit 2
fi
if [ -e "${OUT_DIR}" ]; then
    echo "output directory already exists: ${OUT_DIR}" >&2
    exit 1
fi
if [ ! -d "${BASE_PKG}" ]; then
    echo "base package not found: ${BASE_PKG}" >&2
    exit 1
fi

COMMIT="$(git -C "${REPO_ROOT}" rev-parse --verify "${REVISION}^{commit}")"
SHORT="$(git -C "${REPO_ROOT}" rev-parse --short "${COMMIT}")"
TARGET="armv7-unknown-linux-gnueabihf"

# Iteration number, when the revision follows the rc-001-live-iter<N> convention.
# Used only to point the generated release notes at the right evidence record.
ITERN="$(printf '%s' "${REVISION}" | sed -n 's/.*rc-001-live-iter\([0-9][0-9]*\).*/\1/p')"
EVID=""
[ -n "${ITERN}" ] && EVID="docs/release/RC_LIVE_ITER${ITERN}_EVIDENCE.md"

# Host runtime files (package path -> repo path).
HOST_FILES=(
    "Dockerfile:Dockerfile"
    "compose.yaml:compose.yaml"
    ".env.example:.env.example"
    "README.md:README.md"
    "gen_template.py:gen_template.py"
    "server.py:server.py"
    "torrent_downloader.py:torrent_downloader.py"
    "channels.json:channels.json"
    "generate_slate.sh:generate_slate.sh"
    "dashboard.html:dashboard.html"
    "manifest.json:manifest.json"
    "sw.js:sw.js"
    "app-icon.png:app-icon.png"
    "app-icon-512.png:app-icon-512.png"
    "screenshot-desktop.png:screenshot-desktop.png"
    "screenshot-mobile.png:screenshot-mobile.png"
    "src/tools/ts_check.py:src/tools/ts_check.py"
    "templates/ntfs_template.sparse.gz:templates/ntfs_template.sparse.gz"
)
# Operational scripts shipped for the tablet/server.
SHIP_SCRIPTS=(
    deploy_tablet.sh install-recovery-2.sh pack_sparse_template.py reconnect_usb.sh
    switch_live.sh switch_tv_mode.sh switch_vod.sh sync_youtube_cookies.sh tv_watchdog.sh
)
# Docs shipped with the package (package path -> repo path).
SHIP_DOCS=(
    "docs/RC-001.md:docs/agent_tasks/RC-001.md"
    "docs/HARDWARE_VALIDATION_PLAN.md:docs/architecture/HARDWARE_VALIDATION_PLAN.md"
    "docs/RC_001_BASELINE.md:docs/architecture/RC_001_BASELINE.md"
    "docs/RC_001_PLAN.md:docs/architecture/RC_001_PLAN.md"
)

copy_from_git() { # <repo-path> <dest>
    mkdir -p "$(dirname "$2")"
    git -C "${REPO_ROOT}" show "${COMMIT}:$1" > "$2"
}

echo "==> building and verifying ${REVISION} (${COMMIT})"
VERIFY_OUT="$("${REPO_ROOT}/scripts/verify_rc_live_release.sh" "${REVISION}")" || {
    printf '%s\n' "${VERIFY_OUT}"; exit 1; }
printf '%s\n' "${VERIFY_OUT}" | sed -n 's/^==> /    /p'
BIN_DIR="$(printf '%s\n' "${VERIFY_OUT}" | sed -n 's/^==> binaries: //p' | tail -1)"
EXPECTED="$(printf '%s\n' "${VERIFY_OUT}" | sed -n 's/^==> record: //p' | tail -1)"
if [ -z "${BIN_DIR}" ] || [ ! -d "${BIN_DIR}" ]; then
    echo "could not resolve the verified binary directory from verify_rc_live_release.sh" >&2
    exit 1
fi
if [ -z "${EXPECTED}" ]; then
    echo "could not resolve the hash record from verify_rc_live_release.sh" >&2
    exit 1
fi
if [ ! -f "${EXPECTED}" ]; then
    # The verifier matched a record that lived inside its temporary worktree,
    # which it removes on exit. That record came from the revision's own tree, so
    # its path under docs/release/ is exact: read it back from the commit instead
    # of trusting a path that no longer exists.
    record_rel="docs/release/$(basename "${EXPECTED}")"
    if ! git -C "${REPO_ROOT}" cat-file -e "${COMMIT}:${record_rel}" 2>/dev/null; then
        echo "hash record ${EXPECTED} is gone and absent from ${COMMIT}:${record_rel}" >&2
        exit 1
    fi
    EXPECTED_TMP="$(mktemp)"
    trap 'rm -f "${EXPECTED_TMP}"' EXIT
    git -C "${REPO_ROOT}" show "${COMMIT}:${record_rel}" > "${EXPECTED_TMP}"
    EXPECTED="${EXPECTED_TMP}"
    echo "==> record recovered from ${COMMIT}:${record_rel} (it lived in the temporary worktree)"
fi

echo "==> staging ${OUT_DIR}"
mkdir -p "${OUT_DIR}"/{armv7,host,scripts,source,docs,rollback}

# Copy from the directory the verification actually built into, then prove the
# copied binaries are the ones the record names. Never trust a fixed path: the
# verified tree may be an isolated worktree with its own target directory.
for b in fuse_ntfs fuse_direct stream_fetcher patch_trp sparse_unpack; do
    install -m 0755 "${BIN_DIR}/${b}" "${OUT_DIR}/armv7/${b}"
    expected="$(awk -v n="${b}" '$2 == n { print $1 }' "${EXPECTED}")"
    actual="$(sha256sum "${OUT_DIR}/armv7/${b}" | cut -d' ' -f1)"
    if [ -z "${expected}" ] || [ "${expected}" != "${actual}" ]; then
        echo "binary does not match the record: ${b} (expected ${expected:-<missing>}, got ${actual})" >&2
        exit 1
    fi
    echo "    verified ${b}  ${actual}"
done

echo "==> source/ (pinned Rust workspace inputs)"
copy_from_git "Cargo.toml" "${OUT_DIR}/source/Cargo.toml"
copy_from_git "Cargo.lock" "${OUT_DIR}/source/Cargo.lock"
copy_from_git ".gitignore" "${OUT_DIR}/source/gitignore.txt"
copy_from_git ".cargo/config.toml" "${OUT_DIR}/source/cargo-config.toml"
while IFS= read -r path; do
    copy_from_git "${path}" "${OUT_DIR}/source/${path}"
done < <(git -C "${REPO_ROOT}" ls-tree -r --name-only "${COMMIT}" -- crates \
         | grep -E '(Cargo\.toml|\.rs)$')

echo "==> host/"
for entry in "${HOST_FILES[@]}"; do
    copy_from_git "${entry##*:}" "${OUT_DIR}/host/${entry%%:*}"
done
# Generated slate assets are not tracked; take them from the base package.
for asset in slate.ts slate_1080p.ts; do
    if [ -f "${BASE_PKG}/host/${asset}" ]; then
        install -m 0644 "${BASE_PKG}/host/${asset}" "${OUT_DIR}/host/${asset}"
    else
        echo "warning: base package has no host/${asset}" >&2
    fi
done

echo "==> scripts/"
for s in "${SHIP_SCRIPTS[@]}"; do
    copy_from_git "scripts/${s}" "${OUT_DIR}/scripts/${s}"
    chmod 0755 "${OUT_DIR}/scripts/${s}"
done

echo "==> docs/"
for entry in "${SHIP_DOCS[@]}"; do
    copy_from_git "${entry##*:}" "${OUT_DIR}/${entry%%:*}"
done
while IFS= read -r path; do
    copy_from_git "${path}" "${OUT_DIR}/${path}"
done < <(git -C "${REPO_ROOT}" ls-tree -r --name-only "${COMMIT}" -- docs/release)

echo "==> rollback/ (from ${BASE_PKG})"
cp -a "${BASE_PKG}/rollback/." "${OUT_DIR}/rollback/"

echo "==> metadata"
CARGO_V="$(cargo --version 2>/dev/null || echo unknown)"
RUSTC_V="$(rustc --version 2>/dev/null || echo unknown)"
PY_V="$(python3 --version 2>/dev/null || echo unknown)"
PYTEST_V="$(${REPO_ROOT}/.venv/bin/pytest --version 2>/dev/null || pytest --version 2>/dev/null || echo unknown)"
FFMPEG_V="$(ffmpeg -version 2>/dev/null | head -1 || echo unknown)"

cat > "${OUT_DIR}/BUILD.md" <<EOF
# RC-Live build instructions

Source revision: \`${COMMIT}\` (${REVISION})

## Toolchain used

\`\`\`text
cargo/rustc  ${CARGO_V#cargo } / ${RUSTC_V#rustc }
target       ${TARGET} (static)
python       ${PY_V#Python }
ffmpeg       ${FFMPEG_V}
\`\`\`

\`source/\` contains the pinned workspace inputs: \`Cargo.toml\`, \`Cargo.lock\`,
the \`.cargo\` linker config and the tracked \`crates/**\` sources. Tests, the
parser and the dashboard are not part of the Rust build.

## ARM build

\`\`\`sh
cargo build --workspace --release --target ${TARGET} --offline
\`\`\`

Expected binaries: \`fuse_ntfs\`, \`fuse_direct\`, \`stream_fetcher\`, \`patch_trp\`,
\`sparse_unpack\`; see \`RELEASE_NOTES.md\` for their SHA-256 values.

## Verify the package

\`\`\`sh
sha256sum -c MANIFEST.sha256
\`\`\`

Rebuild-and-compare from the tagged revision:

\`\`\`sh
scripts/verify_rc_live_release.sh ${REVISION} <this-package-dir>
\`\`\`
EOF

{
    echo "# RC-001 / RC-Live — release notes"
    echo
    echo "Commit: \`${COMMIT}\` (${REVISION})"
    echo
    echo "## Scope"
    echo
    if [ -n "${ITERN}" ]; then
        echo "Iteration-${ITERN} candidate for RC-001 / RC-Live, cut from the"
        echo "audited RC-Live review loop. Full evidence record: \`${EVID}\`."
    else
        echo "RC-001 / RC-Live candidate. See \`docs/release/\` for the evidence"
        echo "record that accompanies this revision."
    fi
    echo
    echo "## Artifact hashes (ARM32 static EABI5)"
    echo
    echo '```text'
    for b in fuse_ntfs fuse_direct stream_fetcher patch_trp sparse_unpack; do
        printf '%s  %s\n' "$(sha256sum "${OUT_DIR}/armv7/${b}" | cut -d' ' -f1)" "${b}"
    done
    echo '```'
    echo
    echo "## Test evidence"
    echo
    if [ -n "${ITERN}" ]; then
        echo "See \`${EVID}\` and \`RC_MANIFEST.json\`."
    else
        echo "See \`docs/release/\` and \`RC_MANIFEST.json\`."
    fi
    echo
    echo "## Known remaining risks"
    echo
    echo "- Docker base image and \`apt\`/\`pip\` packages are not pinned by digest."
    echo "- The ARM linker path in \`.cargo/config.toml\` is absolute and host-specific."
    echo "- \`channels.json\` contains public provider URLs; review before deployment."
} > "${OUT_DIR}/RELEASE_NOTES.md"

python3 - "${OUT_DIR}" "${COMMIT}" "${SHORT}" "${REVISION}" <<'PY'
import hashlib, json, os, sys, datetime
out, commit, short, revision = sys.argv[1:5]

def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

artifacts = {}
for name in ("fuse_ntfs", "fuse_direct", "stream_fetcher", "patch_trp", "sparse_unpack"):
    artifacts[f"armv7/{name}"] = sha(os.path.join(out, "armv7", name))
for name in ("fuse_ntfs", "fuse_direct", "stream_fetcher", "patch_trp", "sparse_unpack"):
    pre = os.path.join(out, "rollback", "armv7-pre-rc", name)
    if os.path.exists(pre):
        artifacts[f"rollback/armv7-pre-rc/{name}"] = sha(pre)

manifest = {
    "name": "RC-001 / RC-Live",
    "branch": "rc-001-live",
    "revision": revision,
    "commit": commit,
    "short": short,
    "generated_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "artifacts": artifacts,
    "server_py_sha256": sha(os.path.join(out, "host", "server.py")),
    "excluded": [".env", "parser integration", "catalog_cache", "favorites.json",
                 "series_favorites.json", "server_events.log", "target",
                 "__pycache__", ".pytest_cache", "fuse_ntfs_consolidado.rs"],
    "parser_included": False,
}
with open(os.path.join(out, "RC_MANIFEST.json"), "w") as f:
    json.dump(manifest, f, indent=2, sort_keys=True)
    f.write("\n")
print("RC_MANIFEST.json written")
PY

echo "==> MANIFEST.sha256"
( cd "${OUT_DIR}" && find . -type f ! -name MANIFEST.sha256 -print0 \
    | sort -z | xargs -0 sha256sum > MANIFEST.sha256 )
( cd "${OUT_DIR}" && sha256sum -c MANIFEST.sha256 >/dev/null )

FILES="$(find "${OUT_DIR}" -type f | wc -l)"
echo "==> package ready: ${OUT_DIR}"
echo "    files:    ${FILES}"
echo "    manifest: $(sha256sum "${OUT_DIR}/MANIFEST.sha256" | cut -d' ' -f1)"