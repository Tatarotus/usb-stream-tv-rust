# TASK-004

## TASK ID

TASK-004

## TITLE

Flush Phase Reacquisition & Atomic Epoch Publication

## PRIORITY

P0. Status: **AUTHORIZED — Phase A only**. Phase B (production correction) is
**NO-GO** until the Phase A reproductions are reviewed.

## DEPENDENCIES

TASK-001, TASK-002 and TASK-003 are code-audit approved. This contract does not
reopen them.

## OBJECTIVE

Eliminate two classes of potential failures in the Live TV pipeline:

1. **MPEG-TS phase loss after flush.** A FIFO read may begin at any position of
   a 188-byte packet. The feeder must not publish misaligned bytes as a valid
   TS stream.
2. **Concurrent epoch publication.** A flush may occur while a FUSE thread is
   computing a new anchor, calibrating the transformer, or preparing delivery.
   An operation started in the old generation must not overwrite or corrupt the
   new generation's state.

Playback continuity, the unthrottled feeder and the behaviour approved in
TASK-001..003 must be preserved.

## EVIDENCE

- `crates/fuse_ntfs/src/main.rs` (feeder_loop): observed flush drains the FIFO
  and calls `engine.flush()`; subsequent FIFO bytes are written directly to the
  ring with `write_chunk`.
- `crates/fuse_ntfs/src/engine/live_engine.rs` (`flush`): anchor, ring and
  transformer are reset in separate critical sections.
- `crates/fuse_ntfs/src/engine/live_engine.rs` (reanchor): the next epoch is
  computed before calibration and the anchor is published in a later lock.
- `crates/fuse_ntfs/src/engine/ring.rs`: 128 MiB circular buffer, unthrottled
  writer, cooperative reader wait.
- `crates/fuse_ntfs/src/engine/snapper.rs`: `d12_snap` returns the packet grid
  found in the stream; `leadback_guard` re-snaps to the absolute 188 grid.
- `crates/fuse_ntfs/src/engine/ts_xform.rs`: PCR/PTS/DTS/CC continuity, partial
  packet carry and epoch state.

These are risk points; not every interleaving is proven.

## PHASE A — REPRODUCTION AND FALSIFICATION

Authorized now. **No production changes.**

Host-only tests must exercise:

### A1 — All flush alignments

- Test all 188 possible phases (0..187).
- Simulate partial FIFO reads and restart after flush.
- Fragment sizes: 1, 187, 188, 189, 16_384, 65_536 and 131_072 bytes.
- Insert fake `0x47` bytes inside payloads so a naive sync-byte search is not
  accepted as proof of alignment.
- Verify consistent TS header sequence, buffer boundaries and preservation of
  valid packet bytes.
- Demonstrate whether the current implementation can publish data from a
  misaligned origin. If not reproducible, record `NOT_REPRODUCED`; do not
  invent a defect.

### A2 — Generation concurrency

- Use `Barrier`, `Condvar` or equivalent explicit synchronization; `sleep()` is
  not a synchronization mechanism.
- Minimum interleavings:
  1. Flush while a read is using an old anchor snapshot.
  2. Flush between transformer calibration and publication of the new anchor.
  3. Flush while a read waits at the ring edge.
  4. Flush during reanchor followed by new-generation data.
  5. Stale commit attempted after flush completes.
- Verify `epoch_seq`, mapping, transformer state and bytes actually delivered.

### A3 — Negative controls

- Without a flush, a valid stream is unchanged.
- A correctly aligned flush must not create a false corruption finding.
- An old generation must not be accepted as new.
- If a failure is not reproduced, record `NOT_REPRODUCED`.

## PHASE A FILES

New test files:

- `crates/fuse_ntfs/tests/integration_flush_phase.rs`
- `crates/fuse_ntfs/tests/integration_epoch_flush.rs`

New specification / handoff:

- `docs/agent_tasks/TASK-004.md` (this file)
- `docs/architecture/TASK_004_A_HANDOFF.md`

Tests must use synthetic data, memory and controlled synchronization. They must
not trigger the real flush/resync marker.

Production files may be inspected but not edited. If a real path cannot be
tested without a small injection seam, record `BLOCKED_BY_TEST_SEAM` and
propose the smallest modification for separate authorization.

## PHASE B — PROPOSED PRODUCTION CORRECTION (NOT AUTHORIZED)

Only after Phase A review may these files be considered, restricted to the
reproduced defects:

| File | Potential change |
|---|---|
| `crates/fuse_ntfs/src/main.rs` | Phase recovery on feeder publication after flush |
| `crates/fuse_ntfs/src/engine/ring.rs` | Generation coherence / residency validation, if needed |
| `crates/fuse_ntfs/src/engine/live_engine.rs` | Protection against stale commits and deliveries |
| `crates/fuse_ntfs/src/engine/ts_xform.rs` | Coherence between published epoch and transform state |
| `crates/fuse_ntfs/src/engine/mod.rs` | Auxiliary module declaration, if justified |

A new pure module (e.g. `engine/feeder_phase.rs`) may be proposed only with
Phase B approval. Never modify all files automatically; use the smallest subset
that fixes proven failures.

## PHASE B ACCEPTANCE CRITERIA

- Correct alignment for all 188 phases.
- No publication of old content as the new generation.
- No stale anchor/transformer commit after flush.
- Bounded recovery buffers and processing time.
- PCR/PTS/DTS and CC continuity preserved in tested conditions.
- No deadlock in reproduced adversarial scenarios.
- No artificial feeder throttling.
- Ring remains 128 MiB; no full second copy or unbounded allocation.
- Cooperative waits and pacing with the mutex released; 50 ms pause bound.
- No regression in TASK-001, TASK-002, TASK-003.
- `main.rs` and `live_engine.rs` stay at <= 500 lines. `ring.rs` and
  `ts_xform.rs` already exceed the limit: treat as explicit legacy debt; do not
  minify, delete tests or silently raise the budget. New files <= 500 lines.
- If a limit cannot be met without a coherent extraction, return a structural
  proposal instead of changing the rule unilaterally.

## FROZEN FILES AND BEHAVIOURS

- TASK-001, TASK-002 and TASK-003 tests and corrections.
- `engine/state_machine.rs` and `engine/snapper.rs` (unless separately
  authorized).
- TASK-003 observability and event contract.
- `vfs/dispatcher.rs`, NTFS, FUSE ABI and virtual disk geometry.
- `server.py`, stream fetcher, VOD pipeline and channel-switch policy.
- Watchdog, gadget, deploy, reconnect and mode-switch scripts.
- The original C reference implementation.

## STOP POINT

Phase A ends with red/green evidence and a handoff. No production change, no
hardware action, no deployment.
