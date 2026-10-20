# RC-001 / RC-Live — ITER#4 evidence

Host-only record for the ITER#4 candidate (`rc-001-live-iter4`). Nothing in this
document was produced by touching a device, `/data`, the USB gadget or a
provider.

## Context

The independent review of ITER#3 returned **PLAN**: the Rust work was confirmed,
and two host-only corrections were required before any physical step:

- **P1** — `scripts/pack_rc_live_release.sh` verified the requested revision but
  copied its binaries from a fixed path, so it could package **another**
  revision's binaries.
- **P2** — `docs/release/RC_LIVE_SELECTIVE_UPDATE_AND_ROLLBACK.md` misdescribed
  `switch_tv_mode.sh live` as a restart without resetting the gadget, and
  replaced the executable without requiring the service to be stopped.

Both are corrected here. No Rust source changed in ITER#4.

## 1. P1 — the packer now packages the revision it was asked for

**Defect.** The packer called `verify_rc_live_release.sh <revision>` and then
copied from `${REPO_ROOT}/target/armv7-unknown-linux-gnueabihf/release`. When
`HEAD` was not the requested revision, the verifier built into
`target/verify/<short-sha>/`, so the copy came from whatever the last in-place
build had left in `target/` — for example, packing the frozen `16d5991` tag from
a newer `HEAD` could have shipped the newer `fuse_ntfs`.

**Fix — verifier** (`scripts/verify_rc_live_release.sh`,
sha256 `2952101a5e065cee93f56912da9ee23b2fbe91f4533d2b213efb64ae88ba96c6`):

- selects the hash record by its `# Revision:` line, searching the **current
  tree first** and the temporary worktree second, so a record cannot be left
  pointing at a path that is deleted when the worktree is cleaned up;
- prints the resolved inputs for callers:
  `==> record: <path>` and `==> binaries: <dir>`.

**Fix — packer** (`scripts/pack_rc_live_release.sh`,
sha256 `dbdfabbeca2e5aae543bc32ddd1576f346c1e6627bd39e0839022097f1d418a7`):

- consumes the verifier's reported binary directory instead of a fixed path, and
  copies from there;
- **proves every copied binary against the resolved record** and aborts on any
  mismatch, so a package cannot be assembled from an unverified artefact;
- if the record lived inside the verifier's temporary worktree (already removed
  when the verifier returned), recovers it from the revision tree by name
  (`docs/release/<basename>`) and fails loudly if it cannot.

**Regression** (`tests/test_pack_rc_live_release.sh`, host-only):

| scenario | what it forces | assertion |
|----------|----------------|-----------|
| A | pack the old tag `rc-001-live-16d5991` while `HEAD` points at another revision | the package's `armv7/fuse_ntfs` is `909e2f06…` (the tag's), the packer consumed the verifier's `target/verify/<short>` directory, and `MANIFEST.sha256` verifies |
| B | hide the record from the current tree so it resolves only from the verifier's temporary worktree, then pack that tag | the record path is gone by the time the packer reads it; the packer recovers it from the revision tree and the package still carries the tag's `d5a973d6…` |

Scenario B needs `HEAD` to differ from the tag under test (otherwise the
verifier builds in place and no temporary worktree holds the record); it
therefore runs from this candidate's `HEAD`, not from the tag it packs. The
regression log is attached to the ITER#4 handoff.

## 2. P2 — the update procedure now matches the script

**Defect.** The procedure claimed `switch_tv_mode.sh live` restarted Live
"without resetting the gadget". Reading `scripts/switch_tv_mode.sh`, the
`live|tv` branch calls `cleanup_stack()`, which writes `0` to
`/sys/class/android_usb/android0/enable` (disables the gadget), detaches
`f_mass_storage/lun0/file`, lazily unmounts `/data/local/tmp/ntfs_lab/mnt` and
`/data/local/tmp/vfat_mnt`, and `killall -9`s `stream_fetcher`, `fuse_ntfs` and
`fuse_direct`. It then recreates the FIFO, may re-run `patch_trp` on the live
template, starts `fuse_ntfs` plus exactly one `stream_fetcher`, waits for a
≥ 8 MB pre-buffer, flips the USB inquiry string between `LIVETV1`/`LIVETV2`,
detaches and re-attaches `lun0/file`, and re-enables the gadget.

That is a **planned, controlled re-enumeration** of the USB path — the TV
deliberately sees the device disappear and return — not a reset-free restart. It
is distinct from a **spontaneous** reset appearing with no mode switch in
progress, which is a defect signal.

**Correction.** `docs/release/RC_LIVE_SELECTIVE_UPDATE_AND_ROLLBACK.md` now:

- states what `switch_tv_mode.sh live` really does, and names the planned
  re-enumeration as such in the acceptance conditions;
- **stops the service before replacing anything** (the same `enable=0` / LUN
  detach / umount / `killall -9` primitives) and requires a printed `STOPPED`
  before continuing;
- records the installed state and hashes, and keeps a **byte copy** of the
  installed `fuse_ntfs` as the rollback source;
- stages and hashes the new binary on the device **before** remounting `/system`;
- replaces only `/system/xbin/fuse_ntfs` and lists the rollback as
  stop → restore → start, with explicit acceptance and stop conditions;
- leaves the server untouched (`server.py` is byte-identical,
  `53552fc9a000f6f2a9c9e2477d3429ed4906265a0d8fd62bbf6fb622302be517`).

## 3. Candidate identity

- Branch `rc-001-live`; candidate tag **`rc-001-live-iter4`**.
- **No previous tag was moved**: `rc-001-live-iter3` remains at `e23cf4d`,
  `rc-001-live-16d5991` remains at `16d5991`.
- No Rust source changed in ITER#4, so all five ARM binaries are byte-identical
  to ITER#3. New record: `docs/release/RC_LIVE_ARM_SHA256_iter4.txt`
  (`# Revision: rc-001-live-iter4`, same hashes; only `fuse_ntfs` differs from
  the frozen release: `d5a973d6…` vs `909e2f06…`).
- Runtime manifest regenerated because the two scripts are runtime inputs:

```text
files: 94
manifest_sha256: 0d0630859ccc0adae4488786f83e242e8ce5fb2c1ea3066884652363792b4113
```

## 4. Host test evidence

- Rust, full `fuse_ntfs` crate, serial: **221 passed / 0 failed / 3 ignored**
  (no source change vs ITER#3, where the same result was recorded).
- Python: **241 passed + 41 subtests**; watchdog shell: **56 passed / 0 failed**;
  `test_vod_lock_and_elf.sh`: PASS.
- `verify_rc_live_release.sh`: PASS for `rc-001-live-iter4` (in place, and
  against the assembled package) and for `rc-001-live-16d5991` (isolated
  worktree against the frozen record).
- `tests/test_pack_rc_live_release.sh`: scenario A and scenario B as above.
- The assembled package passes `sha256sum -c MANIFEST.sha256`; its manifest hash
  and `RC_MANIFEST.json` are attached to the ITER#4 handoff (the package is
  deliberately assembled outside the reviewed working tree).

## 5. What is still not done

- The candidate has **not** been deployed and no TV/tablet/server test has run.
- The remaining pre-physical item is proving backup and restore of the
  components actually installed on the tablet and the server; that requires
  device access and explicit operator approval.
- Deferred exactly as before: the audit's original items #1/#2/#3/#5 (per-PID
  PTS alignment remains contrary to the pinned splice contract).
- The TASK-006/B parser stays unintegrated and is excluded from the package.