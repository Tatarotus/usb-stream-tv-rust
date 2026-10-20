#!/usr/bin/env bash
# Verify the RC-Live release: build the ARM binaries from the requested tagged
# revision and compare them against that revision's frozen SHA-256 values, then
# (optionally) verify an assembled package manifest.
#
# Usage:
#   scripts/verify_rc_live_release.sh [revision] [package_dir]
#
#   revision     git revision/tag to build (default: rc-001-live-16d5991)
#   package_dir  optional path to an assembled package (containing
#                MANIFEST.sha256); when given, `sha256sum -c` is run there.
#
# The build always happens on the *requested* revision, never on whatever the
# current working tree happens to contain:
#   * if HEAD already equals that revision, the current tree is used;
#   * otherwise a temporary detached `git worktree` of the revision is created,
#     built, and removed on exit. This prevents the failure mode where a tag is
#     named on the command line but the hashes are taken from an unrelated HEAD.
#
# This script only builds and hashes. It never deploys, never touches a
# device, /data, the USB gadget or any provider.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REVISION="${1:-rc-001-live-16d5991}"
PACKAGE_DIR="${2:-}"
TARGET="armv7-unknown-linux-gnueabihf"

command -v sha256sum >/dev/null || { echo "sha256sum not found" >&2; exit 1; }
command -v cargo >/dev/null || { echo "cargo not found" >&2; exit 1; }
command -v git >/dev/null || { echo "git not found" >&2; exit 1; }

BUILD_REV="$(git -C "${REPO_ROOT}" rev-parse --verify "${REVISION}^{commit}")"
HEAD_REV="$(git -C "${REPO_ROOT}" rev-parse --verify 'HEAD^{commit}')"
echo "==> revision: ${REVISION}"
echo "==> revision commit: ${BUILD_REV}"

BUILD_ROOT="${REPO_ROOT}"
WORKTREE=""
cleanup() {
    if [ -n "${WORKTREE}" ] && [ -e "${WORKTREE}" ]; then
        git -C "${REPO_ROOT}" worktree remove --force "${WORKTREE}" >/dev/null 2>&1 \
            || rm -rf "${WORKTREE}"
    fi
}
trap cleanup EXIT

if [ "${HEAD_REV}" != "${BUILD_REV}" ]; then
    echo "==> HEAD ${HEAD_REV} differs from the revision; building an isolated worktree"
    WORKTREE="$(mktemp -d "${TMPDIR:-/tmp}/rc-live-verify.XXXXXX")"
    rmdir "${WORKTREE}"
    git -C "${REPO_ROOT}" worktree add --detach "${WORKTREE}" "${BUILD_REV}" >/dev/null
    BUILD_ROOT="${WORKTREE}"
else
    echo "==> HEAD matches the revision; building the current tree"
fi

# Expected hashes are read from the revision under verification itself. Each
# record file carries a `# Revision:` line naming the tag and/or commit it
# describes, so a tree may legitimately hold several records (for example the
# frozen 16d5991 record plus a newer RC). Matching by commit, short commit, the
# revision as typed and any tag pointing at the commit keeps the record
# unambiguous without a hard-coded filename.
SHORT_REV="$(git -C "${REPO_ROOT}" rev-parse --short "${BUILD_REV}")"
REV_TAGS="$(git -C "${REPO_ROOT}" tag --points-at "${BUILD_REV}" | tr '\n' ' ')"
LABELS=" ${BUILD_REV} ${SHORT_REV} ${REVISION} ${REV_TAGS} "
EXPECTED=""
RECORD_COUNT=0
# Records may live in the revision's own tree or in the current tree (the
# frozen 16d5991 record was added after that tag was cut).
for record_root in "${BUILD_ROOT}" "${REPO_ROOT}"; do
    for candidate in "${record_root}"/docs/release/RC_LIVE_ARM_SHA256*.txt; do
        [ -f "${candidate}" ] || continue
        RECORD_COUNT=$((RECORD_COUNT + 1))
        revision_line="$(grep -m1 '^# *Revision' "${candidate}" || true)"
        for label in ${LABELS}; do
            case "${revision_line}" in
                *"${label}"*) EXPECTED="${candidate}" ;;
            esac
        done
    done
done
if [ -z "${EXPECTED}" ]; then
    if [ "${RECORD_COUNT}" -eq 1 ]; then
        EXPECTED="${BUILD_ROOT}/docs/release/RC_LIVE_ARM_SHA256.txt"
    else
        echo "no RC_LIVE_ARM_SHA256*.txt record for ${REVISION} in" \
             "${BUILD_ROOT}/docs/release (found ${RECORD_COUNT})" >&2
        exit 1
    fi
fi
BIN_DIR="${REPO_ROOT}/target/${TARGET}/release"

echo "==> building ${TARGET} (offline) from ${BUILD_ROOT}"
( cd "${BUILD_ROOT}" && CARGO_TARGET_DIR="${REPO_ROOT}/target" cargo build \
    --workspace --release --target "${TARGET}" --offline )

echo "==> checking ARM binary hashes against ${EXPECTED}"
fail=0
while read -r expected name; do
    case "${expected}" in ''|'#'*) continue ;; esac
    binary="${BIN_DIR}/${name}"
    if [ ! -f "${binary}" ]; then
        echo "MISSING ${name}"; fail=1; continue
    fi
    actual="$(sha256sum "${binary}" | cut -d' ' -f1)"
    if [ "${actual}" = "${expected}" ]; then
        echo "OK      ${name}  ${actual}"
    else
        echo "FAIL    ${name}"; echo "        expected ${expected}"; echo "        actual   ${actual}"; fail=1
    fi
done < "${EXPECTED}"

if [ -n "${PACKAGE_DIR}" ]; then
    echo "==> verifying package manifest in ${PACKAGE_DIR}"
    ( cd "${PACKAGE_DIR}" && sha256sum -c MANIFEST.sha256 >/dev/null ) \
        && echo "OK      package MANIFEST.sha256 (sha256sum -c)" \
        || { echo "FAIL    package MANIFEST.sha256"; fail=1; }
    echo "        manifest sha256: $(sha256sum "${PACKAGE_DIR}/MANIFEST.sha256" | cut -d' ' -f1)"
fi

if [ "${fail}" -ne 0 ]; then
    echo "==> RESULT: FAIL"
    exit 1
fi
echo "==> RESULT: PASS"