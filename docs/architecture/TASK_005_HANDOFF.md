# TASK-005 — host-only handoff

Status: **Phase A+B implemented on the host; awaiting independent audit.**
No hardware action, deployment, `/data` access, marker creation/deletion,
`/dev/fuse` access, sysfs write, mount or real watchdog/mode-script run was
performed.

## 0. Objective and disposition

| Item | Disposition |
|---|---|
| Routine behind-probe escalation created `resync_usb` | **Removed.** `SmDecision::ResyncRequest` and the `behind_null_runs` counter are gone. Behind probes are bounded `ProbeReject` NULL replies. |
| Source starvation / writer stop could be conflated with gadget recovery | **Removed from the reader path.** No code in `crates/fuse_ntfs/src` creates the marker. A new host test exercises a stopped writer with no cursor/epoch advance. |
| Explicit operator recovery | **Retained.** `scripts/tv_watchdog.sh::resync_cycle` still consumes an operator-created `resync_usb` marker and keeps live-only, VOD/favorites suppression, 120 s cooldown and 3/15-minute breaker. |
| TASK-001..004 behavior | Preserved; full suite and shell harness green. |

The C reference still contains its inherited automatic T6 marker; the C tree
was read-only and is not modified. `fuse_ntfs_consolidado.rs` is an untracked,
non-compiled consolidated reference and was not edited.

## 1. Task-start and final hashes

Task-start (before TASK-005 edits):

```
4b6847264a4d8ce975dee5932e2b816e4943994fceabec013b6ef0149738082e  state_machine.rs
17cb8403bdcf5035222b21159181f1d3fb0c650b224ccacd7f9d9b78d31cb5a9  live_engine.rs
5590735645ad3142a7d632e829ef337235091ae72cb5887d16a67fcbf4f4c687  live_engine/observation.rs
d0e48dae2b7a2097481ead2bca898c97c70a252755cfe3d10e7f6c90f29a6201  scripts/tv_watchdog.sh
4f27319ace326e48fe5be8f5b5bb9987b9c8ae9148cb8f2d0ede32dc0149a6ac  integration_strict_contract.rs
d71e04fca90618e0abe4cc860bcd6742524b70cdd6d25e696322a3d6df69803f  integration_seek_reanchor.rs
```

Final:

```
f00e9c1fed3ef56ac4d4c041a67204631b8a032ad231d53bfcd02a0589837b7a  crates/fuse_ntfs/src/engine/state_machine.rs
8ca1f2e84d36a3b99c3cbee582960b49f29e9d1e38ba742bdc73a9f227ecefd3  crates/fuse_ntfs/src/engine/live_engine.rs
88cab6545642796848f308ae83b9a841044c38f561af8b32cb7df9e114eec2bd  crates/fuse_ntfs/src/engine/live_engine/observation.rs
483081b9ffe7df2a7925b8611145a6f09f617389a88cd2dd336bb03fee06ab0e  scripts/tv_watchdog.sh
0047c390e34054d4edeaf92ad19264408598b0eda40100b2ed75a280f8d5a535  crates/fuse_ntfs/tests/integration_strict_contract.rs
76ba78d973eded78315a666a0c521b8f05afb41642d6153d6593e92037462c47  crates/fuse_ntfs/tests/integration_seek_reanchor.rs
8fd153ae27cc9226793987276d098e4f302103fdbce8637c80bab379ba850b65  crates/fuse_ntfs/tests/integration_source_recovery.rs
31ce50aab709e33ca58ac66afdebb51c23fd218bd58bbc48763fbd2da0a97b78  docs/agent_tasks/TASK-005.md
f3f72c3a05bd7a40d37ee49f99e2c108985694c3e5006676f9957946d178d602  docs/architecture/C_VS_RUST_BEHAVIOR_MATRIX.md
909e2f066efb4ef2d64034546f11131c52216c920858e6bfec12b71ec3bae898  target/armv7-unknown-linux-gnueabihf/release/fuse_ntfs
```

The ARM artifact is `ELF 32-bit LSB executable, ARM, EABI5 … statically
linked, stripped`.

## 2. Code changes (task-only scope)

`crates/fuse_ntfs/src/engine/state_machine.rs`

- Removed `SmDecision::ResyncRequest`.
- Removed the `behind_null_runs` field, initialization and resets.
- Behind-window probes now increment `probe_rejects` and return
  `ProbeReject`; no counter threshold and no gadget authority.
- Replaced the old escalation unit test with
  `behind_probes_never_escalate_to_gadget_action` (12 consecutive rejects).

`crates/fuse_ntfs/src/engine/live_engine.rs`

- Removed the `SmDecision::ResyncRequest` arm and the
  `std::fs::File::create("/data/local/tmp/resync_usb")` side effect.
- `ProbeReject` is now the only probe abort; it remains the existing
  non-destructive `obs_abort` path.

`crates/fuse_ntfs/src/engine/live_engine/observation.rs`

- Removed the now-unused `obs_marker_request` helper. The stable
  `resync_request_result` event and `resync_request` reason remain in the
  observation schema for explicit/future marker requests; the routine engine
  path no longer emits them.

`scripts/tv_watchdog.sh`

- Comments now state that the marker is an explicit operator request and that
  source failure/reader starvation never authorizes gadget reconfiguration.
- Launch log wording changed from “pedido pelo FUSE” to “pedido explicito do
  operador”. Decision order, marker consumption/preservation, VOD/favorites
  guards, cooldown, breaker and launch semantics are unchanged.

`crates/fuse_ntfs/tests/integration_strict_contract.rs`,
`crates/fuse_ntfs/tests/integration_seek_reanchor.rs`

- Comments updated to remove the obsolete claim that the 4th behind probe
  escalates to `ResyncRequest`. No assertion was weakened or removed.

`docs/architecture/C_VS_RUST_BEHAVIOR_MATRIX.md`

- USB recovery row now distinguishes the read-only C automatic route from the
  post-TASK-005 Rust explicit-operator policy.

## 3. New host coverage

`crates/fuse_ntfs/tests/integration_source_recovery.rs`:

1. `behind_probes_are_non_destructive_and_never_request_resync`
   - Starts a real in-memory stream, establishes playback, advances the ring
     past the old mapping, then issues 8 behind probes.
   - Every reply is phase-aware NULL; anchor base, successful `last_fend` and
     epoch stay frozen.
   - An enabled in-memory observer records no `resync_request_result` and no
     `reason=resync_request`.
2. `source_starvation_is_in_band_and_does_not_move_cursor`
   - Stops the writer at the live edge; the reader waits the existing bounded
     edge budget and receives NULLs.
   - The successful cursor and epoch do not advance; `near_starve` is
     accounted.

No test creates or reads the real `/data` marker. The full suite may now run
without the previous `--skip probe_isolation_future_and_behind` safety filter.

## 4. Validation results

Full suite (unfiltered; no real marker path exists in the compiled crate):

```
cargo test -p fuse_ntfs --offline -- --test-threads=1
=> 157 lib + 4 cont_splice + 4 epoch_flush + 6 flush_phase
 + 9 no_data + 9 observability + 3 return/channel + 7 seek
 + 2 source_recovery + 14 strict
 = 215 passed, 0 failed, 3 ignored
```

Ignored remains exactly: two legacy TASK-001 known bugs and the TASK-004/A1 raw
pre-fix reproduction.

Targeted:

```
cargo test -p fuse_ntfs --test integration_source_recovery --offline -- --test-threads=1
=> 2 passed, 0 failed

cargo test -p fuse_ntfs --lib \
  engine::state_machine::tests::behind_probes_never_escalate_to_gadget_action \
  --offline -- --test-threads=1
=> 1 passed, 0 failed
```

Shell:

```
bash -n tests/test_watchdog_observability.sh
sh -n scripts/tv_watchdog.sh
bash tests/test_watchdog_observability.sh
=> passed: 56  failed: 0
```

Static marker check:

```
grep -RniE 'resync_usb|File::create\("/data/local/tmp|ResyncRequest|behind_null_runs' \
  crates/fuse_ntfs/src --include='*.rs'
=> only the stable observability enum tokens:
   observability.rs:93  ResyncRequestResult = 13
   observability.rs:164 ResyncRequest = 39
```

`git diff --check` is clean.

## 5. Invariants and limits

- No feeder throttle, ring resize, pacing change, TS-transform change, VOD or
  server/fetcher change.
- TASK-004 generation/flush transactions untouched.
- TASK-003 schema remains wire-compatible; only the routine engine emitter was
  removed.
- Watchdog control order remains: marker observation -> VOD/favorites guard ->
  consume -> executable check -> cooldown -> breaker -> launch.
- Real operator marker behavior is covered by the existing fake-path shell
  harness, not by touching `/data`.
- No hardware claim: the tablet, gadget, real TV reopen, source attribution and
  T7 remain unvalidated. No deployment or next task is authorized.
- `live_engine.rs` is 496 lines; `state_machine.rs` is 708 lines (legacy debt,
  reduced from 716); new test is 212 lines; `tv_watchdog.sh` is 445 lines.
- `rustfmt --check` passes on the new test. Existing legacy files retain their
  known baseline formatting differences; no global reformat was performed.

## 6. Explicit operator path

An operator can still request the bounded recovery explicitly:

```sh
# On the tablet, only when live mode is intended and operationally approved:
touch /data/local/tmp/resync_usb
```

`tv_watchdog.sh` then observes the marker, suppresses it in VOD/favorites,
enforces the 120 s cooldown and 3-per-15-minute breaker, and asks
`switch_tv_mode.sh live` to perform the gadget reconfiguration. This command
was **not** executed during TASK-005.

## 7. Stop point

TASK-005 ends here for independent audit. No deployment, hardware experiment,
device validation, marker operation, commit or push was requested or performed.

## 8. Post-audit addendum (2026-10-20)

Independent ChatGPT/MCP audit verdict: **TASK-005 APPROVED / GO** for the
host-only scope. The auditor confirmed 9/9 TASK-005 hashes, 8/8 TASK-004/B
frozen-file hashes, and 0 new blockers. Deploy and physical validation remain
**NO-GO**. The MCP did not provide independent execution logs, so the test and
build counts remain builder-declared.

This addendum is documentation-only; the audited code and test hashes above are
unchanged. The next roadmap action is to prepare the TASK-006 scope; no
implementation starts automatically.
