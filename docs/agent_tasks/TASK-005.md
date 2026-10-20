# TASK-005

## TASK ID

TASK-005

## TITLE

Isolate ordinary source failures from automatic gadget recycle

## PRIORITY

P0. Status: **AUTHORIZED — host-only Phase A+B**, 2026-10-20. No hardware,
deployment, USB/gadget, real `/data` marker, or C-reference action is
authorized by this contract.

## DEPENDENCIES

TASK-003 and TASK-004 are code-audit approved. This contract does not reopen
them; it deliberately supersedes only the destructive reader-triggered resync
policy described below.

## OBJECTIVE

Routine upstream/source failure or reader starvation must never request, or
cause, an automatic USB gadget recycle. The filesystem stays mounted, the
backing geometry stays stable, and the reader receives bounded NULL packets.

An explicit operator recovery remains available through the existing
`/data/local/tmp/resync_usb` marker and `tv_watchdog.sh` path. That path keeps
its existing live-mode-only, VOD/favorites suppression, 120 s cooldown and
3-per-15-minute breaker bounds.

## EVIDENCE

- `crates/fuse_ntfs/src/engine/state_machine.rs`: four consecutive
  behind-window probes return `SmDecision::ResyncRequest`.
- `crates/fuse_ntfs/src/engine/live_engine.rs`: `ResyncRequest` creates
  `/data/local/tmp/resync_usb` and aborts the read with NULLs.
- `scripts/tv_watchdog.sh`: `resync_cycle` consumes the marker and launches
  `switch_tv_mode.sh live`, which tears down/reconfigures the USB gadget.
- C reference `src/ntfs/fuse_ntfs.c` contains the same inherited T6 path; it is
  evidence of current behavior, not a requirement to preserve it. The C tree is
  read-only.
- `docs/architecture/TARGET_ARCHITECTURE.md` item 3: routine upstream failure
  must not request USB recycle; resync is an explicit operator policy.
- `docs/architecture/HARDWARE_VALIDATION_PLAN.md`: assert no source-triggered
  gadget operation; a mocked gadget sink proves only that no operation was
  attempted in scope.

## ROOT CAUSE

CONFIRMED executable path: behind-probe escalation -> marker creation ->
watchdog launch -> mode-script gadget reconfiguration. The incident trigger is
UNKNOWN. Source failure is not required for the path, but the path is reachable
during recovery from ordinary source/reader state and is an implicit,
destructive media recovery mechanism.

## REQUIRED BEHAVIOR

1. The live read path never creates `/data/local/tmp/resync_usb` and never
   invokes a gadget or mode script.
2. Future probes and behind-window probes remain bounded, non-destructive
   NULL replies. They must not advance the successful cursor, publish a new
   epoch, invalidate a newer transformer tail, or request a direct operator
   action.
3. Source starvation (writer not advancing) and source recovery remain
   in-band. No source-error state authorizes gadget reconfiguration.
4. An explicitly created operator marker is still honored by
   `resync_cycle`, subject to the existing mode/flag, executable, cooldown and
   breaker decisions. Marker consumption/preservation semantics are unchanged.
5. VOD and favorites modes continue to suppress operator resync and preserve
   the marker.
6. TASK-001/002/003 behavior, TASK-004 flush/generation invariants, playback
   policy, 128 MiB ring, feeder pacing and TS continuity remain unchanged.
7. The TASK-003 observation schema remains wire-compatible. The
   `resync_request_result` event and `resync_request` reason stay documented
   for explicit/future marker requests but are no longer emitted by the routine
   engine path.

## DESIGN

- Remove the destructive `ResyncRequest` decision and the
  `behind_null_runs` escalation counter. Behind probes return
  `ProbeReject`, exactly like other bounded probes.
- Remove the marker side effect from `serve_live` and the now-unused engine
  observation helper.
- Update operational comments/log wording to say the marker is an explicit
  operator request, not a FUSE/source request. Do not change the watchdog
  decision order, cooldown, breaker, flags or launch semantics.
- Replace the old state-machine test that asserted the destructive escalation
  with a test that asserts behind probes never escalate. Update comments in
  integration tests that described the old behavior.
- Add a focused host integration test covering behind probes and starvation:
  NULLs, frozen cursor/epoch, and no routine resync request.

## ALLOWED FILES

Production:

- `crates/fuse_ntfs/src/engine/state_machine.rs`
- `crates/fuse_ntfs/src/engine/live_engine.rs`
- `crates/fuse_ntfs/src/engine/live_engine/observation.rs` (remove the unused
  marker helper only)
- `scripts/tv_watchdog.sh` (comments/log wording only; no decision changes)

Tests:

- `crates/fuse_ntfs/src/engine/state_machine.rs` unit-test module
- `crates/fuse_ntfs/tests/integration_strict_contract.rs` (comments only)
- `crates/fuse_ntfs/tests/integration_seek_reanchor.rs` (comment only)
- NEW `crates/fuse_ntfs/tests/integration_source_recovery.rs`

Documentation:

- `docs/agent_tasks/TASK-005.md` (this file)
- `docs/architecture/TASK_005_HANDOFF.md`
- `docs/architecture/C_VS_RUST_BEHAVIOR_MATRIX.md` (USB recovery row only)

## READ-ONLY REFERENCES

All TASK-001..004 tests not explicitly named above, `engine/ring.rs`,
`engine/ts_xform.rs`, `engine/snapper.rs`, `engine/feeder_phase.rs`,
`contract.rs`, `main.rs`, observability implementation and contract docs, VOD,
server, fetcher, C reference. Preserve the dirty tree and all user work.

## FORBIDDEN CHANGES

No hardware/device commands, `/dev/fuse`, `/data` or sysfs access, marker
creation/deletion, mounts, daemon launch, deployment, network probes, or
production restart. No `server.py`, `stream_fetcher`, VOD, channel policy,
watchdog cooldown/breaker/flag/order changes. No second ring, feeder throttle,
pacing change, TS-transform change, observability schema removal, test
deletion/ignore/weakening, or global formatting. Do not modify the C reference.

## TESTS

- Unit: future probes and 8+ behind probes all return `ProbeReject`; no
  `ResyncRequest` variant remains.
- Unit/integration: four or more behind probes return full phase-aware NULLs,
  leave anchor/epoch/successful cursor unchanged, and keep subsequent
  adjacency intact.
- Integration: an established reader at the writer edge with no new source
  data is NULL-filled after the bounded wait, with no successful-cursor or
  epoch advance.
- Existing `probe_isolation_future_and_behind` remains green with updated
  comments; no real marker is touched.
- Shell harness: existing `tests/test_watchdog_observability.sh` remains green;
  no real script or marker is executed. The harness count is expected to stay
  56 unless a purely fake favorites-mode case is added.

## ACCEPTANCE CRITERIA

- No automatic marker creation or gadget/mode-script invocation remains in the
  live reader path.
- Behind/future probes and starvation are non-destructive and bounded.
- Explicit operator marker behavior, VOD/favorites suppression, cooldown and
  breaker are unchanged.
- TASK-001..004 suites and shell harness remain green; only the intentionally
  ignored legacy/A1 cases remain ignored.
- New files <= 500 physical lines; `main.rs` and `live_engine.rs` <= 500;
  `state_machine.rs` does not grow beyond its existing legacy debt; no
  minification.
- ARMv7 release build succeeds offline where already available.
- Handoff records exact hashes, commands, outputs, skipped/NOT_RUN scope and the
  fact that no hardware or deployment occurred.

## VALIDATION COMMANDS

```sh
cargo test -p fuse_ntfs --offline -- --test-threads=1 \
  --skip probe_isolation_future_and_behind
cargo test -p fuse_ntfs --test integration_source_recovery --offline -- --test-threads=1
cargo test -p fuse_ntfs --test integration_strict_contract --offline -- --test-threads=1 \
  probe_isolation_future_and_behind
cargo test -p fuse_ntfs --test integration_observability --offline -- --test-threads=1
bash -n tests/test_watchdog_observability.sh
sh -n scripts/tv_watchdog.sh
bash tests/test_watchdog_observability.sh
cargo build -p fuse_ntfs --release --target armv7-unknown-linux-gnueabihf --offline
git diff --check
```

The full-suite run must still avoid executing the real marker path. After this
change it is safe by construction; the handoff must state whether the filtered
or unfiltered run was used.

## STOP POINT

Phase A evidence and the Phase B host patch end with a handoff and independent
re-audit. No deployment, hardware experiment, device validation, marker
operation, or next task is authorized.
