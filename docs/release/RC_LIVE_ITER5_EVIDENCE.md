# RC-001 / RC-Live — ITER#5 evidence

Host-only record for the ITER#5 candidate (`rc-001-live-iter5`). Documentation
only: no Rust source, script or host file changed, and nothing was deployed.

## Context

The independent review of ITER#4 returned **DONE** (RC-001 ITER4 host approved)
with two operational observations to apply before the physical step:

- the update procedure placed the final check of the **installed** hashes after
  the command that disables the gadget and kills the processes, so an operator
  following it literally would interrupt the live stream before deciding whether
  the device even matched the expected state;
- `pgrep -f "fuse_ntfs|stream_fetcher"` can match the shell that runs the command
  itself, so the "STOPPED" proof could never be satisfied.

Both are fixed in `docs/release/RC_LIVE_SELECTIVE_UPDATE_AND_ROLLBACK.md`.

## What changed

- The procedure now opens with a **read-only preflight** (section 0): device
  model/ABI, current mode and USB state via `switch_tv_mode.sh status`, the
  installed tablet hashes, the server `server.py` identity, and a byte copy of
  the installed `fuse_ntfs` as the rollback source. None of it stops processes,
  remounts `/system`, detaches the USB or touches the TV.
- Section 0 ends with an explicit **GATE**: the installed `fuse_ntfs`
  (`909e2f06…`), the other three binaries and `server.py` (`53552fc9…`) must
  match, and the status must be the expected one, **before** anything is
  interrupted. If the gate fails, the procedure stops with the stream untouched.
- Stopping became its own section (1) and runs only after the gate passes. Its
  proof of stoppage is name-based — `pidof fuse_ntfs stream_fetcher`, the same
  idiom `switch_tv_mode.sh status` uses — instead of a self-matching `pgrep -f`.
- Everything else from ITER#4 is retained: `switch_tv_mode.sh live` is described
  as a planned controlled re-enumeration (gadget `enable=0`, LUN detach, umounts
  and `killall -9`, then FIFO rebuild, optional `patch_trp`, `fuse_ntfs` +
  one `stream_fetcher`, ≥ 8 MB pre-buffer and a re-attach), distinct from a
  spontaneous reset; stage-and-hash the new binary before remounting `/system`;
  replace only `/system/xbin/fuse_ntfs`; rollback is stop → restore → start; the
  server is untouched.

## Candidate identity

- Branch `rc-001-live`; candidate tag **`rc-001-live-iter5`**.
- **No previous tag was moved**: `rc-001-live-iter4` remains at `b15cb67`,
  `rc-001-live-iter3` at `e23cf4d`, `rc-001-live-16d5991` at `16d5991`.
- No runtime input changed, so the ARM binaries and the runtime manifest are
  identical to ITER#4: 94 files, manifest_sha256
  `0d0630859ccc0adae4488786f83e242e8ce5fb2c1ea3066884652363792b4113`.
  New record: `docs/release/RC_LIVE_ARM_SHA256_iter5.txt` (same hashes as
  ITER#4; only `fuse_ntfs` differs from the frozen release).

## Host test evidence

Unchanged from ITER#4 and re-confirmed at this commit: Rust `fuse_ntfs` crate
serial 221 passed / 0 failed / 3 ignored; Python 241 passed + 41 subtests;
watchdog 56 / 0; `test_vod_lock_and_elf.sh` PASS;
`tests/test_pack_rc_live_release.sh` 6 ok / 0 fail;
`verify_rc_live_release.sh` PASS for `rc-001-live-iter5` and for the frozen
`rc-001-live-16d5991`.

## What is still not done

- Nothing was deployed and no TV/tablet/server test has run.
- The next step is the read-only preflight of section 0, which requires explicit
  operator approval to access the device. Only if its gate passes is a single
  authorization requested for the supervised update window and the physical
  Live TV (`Canais TV`) test.
- Deferred exactly as before: audit items #1/#2/#3/#5. The TASK-006/B parser
  stays unintegrated and excluded from the package.