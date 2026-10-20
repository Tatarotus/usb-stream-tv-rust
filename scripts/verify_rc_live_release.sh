#!/usr/bin/env bash
# Verify the RC-Live release: rebuild the ARM binaries from a tagged revision
# and compare them against the frozen SHA-256 values, then (optionally) verify
# an assembled package manifest.
#
# Usage:
#   scripts/verify_rc_live_release.sh [revision] [package_dir]
#
#   revision     git revision/tag to build (default: rc-001-live-16d5991)
#   package_dir  optional path to an assembled package (containing
#                MANIFEST.sha256); when given, `sha256sum -c` is run there.
#
# This script only builds and hashes. It never deploys, never touches a
# device, /data, the USB gadget or any provider.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REVISION="${1:-rc-001-live-16d5991}"
PACKAGE_DIR="${2:-}"
EXPECTED="${REPO_ROOT}/docs/release/RC_LIVE_ARM_SHA256.txt"
TARGET="armv7-unknown-linux-gnueabihf"
BIN_DIR="${REPO_ROOT}/target/${TARGET}/release"

command -v sha256sum >/dev/null || { echo "sha256sum not found" >&2; exit 1; }
command -v cargo >/dev/null || { echo "cargo not found" >&2; exit 1; }

echo "==> revision: ${REVISION}"
echo "==> building ${TARGET} (offline)"
( cd "${REPO_ROOT}" && git rev-parse --verify "${REVISION}^{commit}" >/dev/null )
BUILD_REV="$(cd "${REPO_ROOT}" && git rev-parse "${REVISION}^{commit}")"
echo "==> revision commit: ${BUILD_REV}"

cargo build --manifest-path "${REPO_ROOT}/Cargo.toml" \
    --workspace --release --target "${TARGET}" --offline

echo "==> checking ARM binary hashes"
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