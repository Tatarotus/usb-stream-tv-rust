#!/usr/bin/env bash
# Regression: the release packer must package the revision it was asked for.
#
# Historical defect: pack_rc_live_release.sh verified the requested revision but
# copied the binaries from a fixed path (target/<triple>/release). When HEAD was
# not the requested revision, the verifier built into target/verify/<short>/ and
# the packer copied whatever the last in-place build had left there, so a package
# could carry another revision's binaries.
#
# Scenario A: pack an OLD tag while HEAD points elsewhere; the package must hold
#             the requested tag's hashes.
# Scenario B: force the hash record to be resolved from the verifier's temporary
#             worktree (repo copy temporarily hidden), so the record path is gone
#             by the time the packer reads it; the packer must recover the record
#             from the revision tree and still package the right binaries.
#
# Host-only: builds and copies files into a temporary directory. It never
# deploys, never touches a device, /data, the USB gadget or any provider.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACK="${REPO_ROOT}/scripts/pack_rc_live_release.sh"
BASE_PKG="${BASE_PKG:-/home/sam/Code/rc001-release/rc-001-live-16d5991}"

FROZEN_TAG="rc-001-live-16d5991"
FROZEN_FUSE="909e2f066efb4ef2d64034546f11131c52216c920858e6bfec12b71ec3bae898"
ITER3_TAG="rc-001-live-iter3"
ITER3_FUSE="d5a973d6c9194d33c8654767d00e7b6073ca65b30107a717a6a9baa143399f55"

PASS=0
FAIL=0
ok()   { PASS=$((PASS + 1)); echo "ok      $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "FAIL    $1"; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/rc-pack-regress.XXXXXX")"
HIDDEN=""
hide_record()  { # <repo-relative record path>
    HIDDEN="${REPO_ROOT}/$1"
    mv "${HIDDEN}" "${WORK}/hidden-record"
}
unhide_record() {
    [ -n "${HIDDEN}" ] && mv "${WORK}/hidden-record" "${HIDDEN}" && HIDDEN=""
    return 0
}
cleanup() {
    unhide_record
    rm -rf "${WORK}"
}
trap cleanup EXIT

if [ ! -d "${BASE_PKG}" ]; then
    echo "SKIP: base package not found: ${BASE_PKG}" >&2
    exit 0
fi

head_rev="$(git -C "${REPO_ROOT}" rev-parse --short HEAD)"
echo "==> HEAD is ${head_rev}; base package ${BASE_PKG}"

# ---------------------------------------------------------------- scenario A
echo
echo "==> scenario A: pack ${FROZEN_TAG} while HEAD is ${head_rev}"
if [ "${head_rev}" = "$(git -C "${REPO_ROOT}" rev-parse --short "${FROZEN_TAG}^{commit}")" ]; then
    echo "    (skipped: HEAD is the tag itself, the defect needs HEAD != revision)"
else
    outA="${WORK}/pkgA"
    if "${PACK}" "${FROZEN_TAG}" "${outA}" "${BASE_PKG}" > "${WORK}/A.log" 2>&1; then
        got="$(sha256sum "${outA}/armv7/fuse_ntfs" | cut -d' ' -f1)"
        [ "${got}" = "${FROZEN_FUSE}" ] \
            && ok "A: package carries ${FROZEN_TAG}'s fuse_ntfs" \
            || bad "A: package fuse_ntfs ${got} != ${FROZEN_FUSE}"
        grep -qE '^ +binaries: .*target/verify/' "${WORK}/A.log" \
            && ok "A: packer consumed the verifier's isolated worktree binary directory" \
            || bad "A: packer did not consume the verifier's isolated worktree binary directory"
    else
        bad "A: packing ${FROZEN_TAG} failed (see ${WORK}/A.log)"
    fi
fi

# ---------------------------------------------------------------- scenario B
echo
echo "==> scenario B: pack ${ITER3_TAG} with its record hidden in the repo"
record="docs/release/RC_LIVE_ARM_SHA256_iter3.txt"
if [ ! -f "${REPO_ROOT}/${record}" ]; then
    echo "    (skipped: ${record} is not in the working tree)"
elif [ "${head_rev}" = "$(git -C "${REPO_ROOT}" rev-parse --short "${ITER3_TAG}^{commit}")" ]; then
    echo "    (skipped: HEAD is the tag itself, so the verifier builds in place and"
    echo "     no temporary worktree holds the record; run me from a later HEAD)"
else
    hide_record "${record}"
    outB="${WORK}/pkgB"
    if "${PACK}" "${ITER3_TAG}" "${outB}" "${BASE_PKG}" > "${WORK}/B.log" 2>&1; then
        got="$(sha256sum "${outB}/armv7/fuse_ntfs" | cut -d' ' -f1)"
        [ "${got}" = "${ITER3_FUSE}" ] \
            && ok "B: package carries ${ITER3_TAG}'s fuse_ntfs" \
            || bad "B: package fuse_ntfs ${got} != ${ITER3_FUSE}"
        grep -q "^==> record recovered from " "${WORK}/B.log" \
            && ok "B: packer recovered the record from the revision tree" \
            || bad "B: packer did not report recovering the record"
    else
        bad "B: packing ${ITER3_TAG} failed (see ${WORK}/B.log)"
    fi
    unhide_record
    [ -f "${REPO_ROOT}/${record}" ] \
        && ok "B: record restored in the working tree" \
        || bad "B: record was NOT restored"
fi

# ---------------------------------------------------------------- manifest
echo
echo "==> package manifest self-check"
if [ -d "${WORK}/pkgA" ]; then
    ( cd "${WORK}/pkgA" && sha256sum -c MANIFEST.sha256 >/dev/null ) \
        && ok "A: MANIFEST.sha256 verifies" || bad "A: MANIFEST.sha256 broken"
fi

echo
echo "passed: ${PASS}  failed: ${FAIL}"
[ "${FAIL}" -eq 0 ] || exit 1
echo "PACK REVISION ISOLATION: OK"