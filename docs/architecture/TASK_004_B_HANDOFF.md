# TASK-004 — Phase B handoff (re-audit F1–F4, flush atomicity)

Status: **Phase B complete (host-only); F1–F4 plus the flush-transaction blocker
addressed; awaiting re-audit.**
No hardware action, deployment, or real `/data` marker operation was performed.

## 0. Auditor findings and disposition

| # | Finding | Disposition |
|---|---|---|
| F1a | TS transformer updated before the `start_gen` check. | **Fixed.** Reanchor is one atomic section under `xform`+`anchor`: the generation is re-checked before any transformer write; calibration/splice + publication happen under both locks. |
| F1b | `flush()` is not atomic: invalidate anchor, clear ring and reset xform use separate lock scopes. A new reanchor can start between invalidation and ring clear. | **Fixed.** Flush now runs as one transaction in the same order (`xform` → `anchor` → `ring` → `sm`): invalidate + `gen/epoch++`, clear ring, reset transformer, reset probe state, all under the locks. A reanchor cannot start inside it. |
| F1c | A read could capture the new generation after invalidation but observe the old ring before clear. | **Fixed.** `snapshot_gen` is captured **before** the cold check/ring observation; reanchor requires `anchor.gen == snapshot_gen`, and commit uses `delivery_gen = snapshot_gen` (or the read's own published gen after a reanchor). A flush therefore either happens before the snapshot (ring already clear) or after it (gen mismatch). |
| F2 | `obs_abort()` calls `abandon_overwritten()`, invalidating the current generation's transformer tail. | **Fixed.** `obs_abort_stale()` only NULL-fills, accounts and emits; it never locks or mutates the transformer. The stale commit path never calls `invalidate_tail`. Tests assert `splices` is unchanged. |
| F3 | Missing aligner reset on `poll()`/`fifo.read()` error-reopen paths. | **Fixed.** `aligner.reset()` on open error, `poll` error and `read` error, in addition to both flush paths and EOF. New partial-old-source test. |
| F4 | Validation in `commit_delivery()` before pacing; a post-commit flush leaves old bytes. | **Addressed by ordering + contract.** Pacing is before the commit; commit under `xform`+`anchor` is the single irrevocable linearization point. A flush before/during pacing is rejected (seam test). A flush after commit is ordered after the delivery. See §5. |

## 1. What changed

| File | Lines | SHA-256 |
|---|---:|---|
| `crates/fuse_ntfs/src/engine/feeder_phase.rs` (new) | 244 | `bf888b020c94…` |
| `crates/fuse_ntfs/src/engine/live_engine.rs` | 500 | `17cb8403bdcf…` |
| `crates/fuse_ntfs/src/engine/live_engine/anchor.rs` (new) | 37 | `a012936a92da…` |
| `crates/fuse_ntfs/src/engine/live_engine/commit.rs` (new) | 76 | `2aefc892bb8e…` |
| `crates/fuse_ntfs/src/engine/live_engine/observation.rs` | 191 | `5590735645ad…` |
| `crates/fuse_ntfs/src/engine/live_engine/test_seam.rs` (new, `cfg(test)`) | 76 | `2272fa0637b3…` |
| `crates/fuse_ntfs/src/engine/live_engine/phase_b_tests.rs` (new, `cfg(test)`) | 265 | `2841a8f55bd5…` |
| `crates/fuse_ntfs/src/main.rs` | 459 | `bf4e82d63660…` |
| `crates/fuse_ntfs/tests/integration_flush_phase.rs` | 294 | `9acfb52c3cd1…` |
| `crates/fuse_ntfs/tests/integration_epoch_flush.rs` | 223 | `d91711d195c6…` |

`anchor.rs`, `commit.rs` and `observation.rs` contain mechanical extractions
plus the generation gate; `test_seam.rs`/`phase_b_tests.rs` are `#[cfg(test)]`
only. No TASK-001/002/003 test, frozen area, playback policy or observability
schema was changed. `snapper.rs`, `ring.rs`, `ts_xform.rs` and
`state_machine.rs` were not touched by Phase B. `live_engine.rs` is exactly 500
lines; further production edits require freeing a line.

## 2. A1 — feeder phase alignment

`main.rs::feeder_loop` publishes only what `FeederPhase` releases:

- unsynchronized: retain at most `5*188-1` bytes; scan byte-by-byte for five
  consecutive `0x47` headers with TEI=0 at 188-byte spacing; discard only the
  prefix before the confirmed grid;
- aligned: emit only complete 188-byte packets, retain the trailing partial;
- a broken header drops the broken prefix and re-confirms the grid (bounded
  resync; no sleep, lock or backpressure added);
- `reset()` on every flush trigger, FIFO EOF, FIFO open error, `poll()` error
  and `read()` error. After any of these the next source is re-acquired from a
  clean state.

`main.rs` is the only live publication path: `aligner.push(&buf[..n])` then
`ring.write_chunk(&aligned)`; raw `buf[..n]` is never written. `snap188` and
`leadback_guard` retain their existing semantics.

Cost: one extra bounded copy per FIFO read (aligned buffer → ring). The feeder
still writes immediately; there is no added wait, lock, syscall or pacing.

### Tests

- Unit (6): all 188 phases with 64 KiB/16 KiB/128 KiB chunks; small chunks
  (`1/187/188/189`); reset to a new phase; reset after a partial old source
  (read-error analogue); three-point fake sync rejected; empty input.
- Integration: all 188 phases with a 700-packet stream, byte-exact suffix,
  real ring, then `snap_anchor` on the grid; 16/64/128 KiB fragment sizes are
  delivered as full chunks (700-packet window).
- The original raw-injection test remains `#[ignore]`d as the pre-fix
  reproduction (187/188 phases lost, `pos=1061072`, `(pos-phase)%188=187`).
  It documents the old oracle; ring entry is now aligned by the feeder.

## 3. A2 — generation transaction (F1/F2)

### Token and snapshot

`AnchorState.gen` is the anchor-generation token. It is incremented by
`flush()`, `on_open()` **and every reanchor publication**. `epoch_seq` remains
the mapping epoch.

At the start of `serve_live`, **before the cold check and before any ring
observation**, the reader captures:

```
(snapshot_gen, delivery_gen) = anchor.gen
```

- reanchor requires `anchor.gen == snapshot_gen` under `xform`+`anchor`;
- a read that does not reanchor commits with `delivery_gen == snapshot_gen`;
- a read that does reanchor sets `delivery_gen` to the generation it published,
  inside the atomic section.

Thus a flush cannot slip between the ring bytes a read observed and the
generation it commits against.

### Atomic flush (F1b/F1c)

`flush()` now holds `xform` → `anchor` → `ring` → `sm` for the whole
transaction:

1. invalidate the anchor, `last_fend = MAX`, `epoch_seq++`, `gen++`;
2. clear the ring and notify waiters;
3. `reset_epoch(seq)` on the transformer;
4. `reset_probe_state()` on the SM.

A reanchor that starts before the flush either finishes first (the flush then
invalidates it and resets the transformer) or blocks and observes the new,
inactive generation. There is no interval in which the new generation is
visible with the old ring or with an old transformer epoch.

### Atomic reanchor (F1a)

`serve_live` computes `seq`, releases the old anchor, then (optionally parks at
the test seam) acquires `xform` then `anchor` and:

1. re-checks `anchor.gen == snapshot_gen` **before touching the transformer**;
2. on mismatch: drops both locks, NULL-fills via `obs_abort_stale`, touches
   neither anchor, checkpoint nor transformer;
3. on match: cold `reset_epoch` or warm calibration + `begin_splice_epoch`,
   publishes `active/file_id/epoch/gen/base/last_fend`, records
   `delivery_gen = anchor.gen`, and emits the commit, all under both locks.

### Commit gate (F2)

`commit_delivery` holds `xform`+`anchor` and applies the continuity transform,
`note_ckpt` and `last_fend` only if `gen == delivery_gen && active &&
epoch_seq == epoch0`. Otherwise it NULL-fills the whole request, counts the
NULL bytes, emits `ReferenceInvalid`, and **does not call `invalidate_tail`**:
a stale read must not drop a newer generation's carry. `obs_abort_stale` is
the analogous non-mutating abort for the reanchor path.

### Tests

- `flush_is_atomic_with_a_concurrent_reanchor`: flush parks after invalidation
  with `xform+anchor` held; a reader must not complete inside the transaction;
  after release the ring is empty, the anchor stays inactive and
  `xform.epoch_id == anchor.epoch_seq`.
- `#1/#5` `stale_commit_after_flush_is_rejected`: public post-flush contract;
  a stale foff gets NULLs and cannot resurrect `last_fend`. The concurrent
  in-flight variant is the in-crate F4 test below.
- `#2` `flush_between_calibration_and_publication_is_rejected`: parked before
  the atomic section; no anchor/checkpoint; epoch and `splices` unchanged.
- `#4` `new_generation_after_reanchor_flush_is_served`: flush + new `0x5A`
  stream; stale read aborts without touching the transformer; next read serves
  only the new stream.
- `#3` flush during the ring-edge wait remains green.

## 4. F3 — reconnection resets

`main.rs` calls `aligner.reset()` on:

- the pre-open flush path and the streaming flush path;
- FIFO EOF (`Ok(0)`);
- FIFO open error;
- `poll()` error (non-`EINTR`);
- `fifo.read()` error (non-`EINTR`).

`EINTR` and poll timeouts deliberately preserve alignment (same producer).
`reset_after_partial_old_source_drops_old_bytes`: the old gate is aligned and
leaves a partial packet; `reset()`; a new phase-shifted source is fed in
11-byte chunks; output is a byte-exact suffix of the new source, so no old byte
survives.

## 5. F4 — pacing and the irrevocable point

Order is now:

```
deliver ring bytes → pacing sleep (unlocked, sliced) → commit gate
→ checkpoint/last_fend (under xform+anchor) → observation → return
```

- A flush during pacing is still before the commit and is rejected by the gate
  (`flush_after_pacing_before_commit_is_rejected`, seam immediately before the
  commit).
- The commit is the **irrevocable linearization point**. A flush that acquires
  the anchor after the commit is ordered after the delivery; the bytes already
  returned belong to a completed read. This is the defined contract.
- No second post-commit check was added: any check before `return` has the same
  unavoidable race (a flush can occur after the check and before the FUSE
  caller consumes the buffer), and holding an engine lock across the caller's
  return is not possible with the current API. Re-validating would only move
  the race, not close it.

## 6. RED → GREEN evidence

Full suite after all changes:

```
157 lib + 0 bin + 4 cont_splice + 4 epoch_flush + 6 flush_phase + 9 no_data
+ 9 observability + 3 seek + 7 return/channel + 13 strict + 0 doc
= 212 passed, 0 failed, 3 ignored, exit 0
```

The 3 ignored are the two known TASK-001 bugs plus the preserved raw A1
reproduction. New Phase B tests run green without `#[ignore]`.

F1a controlled experiment (reanchor generation guard temporarily disabled,
then restored byte-for-byte and verified with `sha256sum -c`):

```
flush_between_calibration_and_publication_is_rejected ... FAILED
  assertion `left == right` failed: stale read changed the splice state
  left: 3
 right: 2
new_generation_after_reanchor_flush_is_served ... FAILED
  non-NULL byte at offset 1
  left: 64
 right: 31
```

F1b controlled experiment (flush temporarily restored to the non-atomic
sequence with the seam between anchor invalidation and ring clear; restored and
verified):

```
flush_is_atomic_with_a_concurrent_reanchor ... FAILED
  reader completed inside the flush transaction
```

F4 controlled experiment (commit generation check forced true, then restored
and verified):

```
flush_after_pacing_before_commit_is_rejected ... FAILED
  non-NULL byte at offset 1
  left: 65
 right: 31
```

A1 raw reproduction (intentionally ignored):

```
187 of 188 phases lost after flush
phase 1: pos=1061072 (pos-phase)%188=187
```

GREEN after restore: all four phase-B unit tests pass; full suite above.

## 7. Lock order / deadlock review

- Commit and atomic reanchor: `xform` → `anchor` → `ring` (window reads,
  telemetry). Same order in both.
- Atomic flush: `xform` → `anchor` → `ring` → `sm`; invalidation, clear, reset
  and probe reset happen inside these locks. No reader can observe a new
  generation with an old ring.
- `on_open`: `anchor` → `sm`; consistent with flush's `anchor` → `sm` suffix.
- Reads capture `snapshot_gen` under `anchor`, then observe the ring; they do
  not hold `anchor` while acquiring `xform`. The atomic reanchor takes
  `xform` → `anchor`.
- `obs_abort_stale`: no `xform`; samples `anchor`, then `ring` separately;
  never nests them.
- The test seams park with **no lock held** except the flush seam, which
  intentionally holds the transaction locks (that is the property under test).
- The calibration windows (2 MiB read-back, 1 MiB raw) are read while
  `xform+anchor` are held; rare and bounded. A concurrent flush simply waits
  for the transaction.
- No path takes `ring` and then waits for `xform` while holding it. Unit tests
  complete without timeouts (serial execution).

## 8. Regression evidence

```
cargo test -p fuse_ntfs --offline -- --test-threads=1 \
  --skip probe_isolation_future_and_behind
=> 212 passed, 0 failed, 3 ignored, exit 0

sh tests/test_watchdog_observability.sh
=> passed: 56  failed: 0

cargo build -p fuse_ntfs --release \
  --target armv7-unknown-linux-gnueabihf --offline
=> Finished release profile
target/.../fuse_ntfs: ELF 32-bit LSB executable, ARM, EABI5,
statically linked; sha256 2e8c6e730b04…
```

## 9. Invariants preserved

- Feeder remains zero-throttle; no sleep/lock/backpressure added.
- Ring 128 MiB, `MIN_STREAM_START`, pacing, `LEADBACK`, `RESUME_MIN_LEAD`,
  probe isolation, NULL policy and retry policy unchanged.
- `main.rs` 459 and `live_engine.rs` 500, both ≤500; every new file ≤500.
- No test deleted, ignored to hide a failure, or weakened. The raw A1 test is
  retained as the documented pre-fix reproduction with the oracle change
  explicitly stated. The concurrent stale-commit integration scenario moved to
  the in-crate `flush_after_pacing_before_commit_is_rejected` test because the
  atomic flush now acquires `xform` first; the public integration test keeps
  the post-flush contract. Both are green, neither is ignored.
- No `/home/sam/Code/usb-stream-tv-prod` access; no hardware/TV action.

## 10. Notes

- `AnchorState` and the commit block are submodule extractions to respect the
  line budget; the reanchor/flush transactions remain in `live_engine.rs`.
- `gen` changes on reanchor too; this is what makes a concurrent reanchor
  invalidate an in-flight read.
- `obs_abort_stale` uses the existing `ReferenceInvalid` reason and event
  shape; no schema change.

## 11. Hashes

```
bf888b020c9488f70ffb387faa9fcc2385cf61b9111764f5874110008a668573  engine/feeder_phase.rs
17cb8403bdcf5035222b21159181f1d3fb0c650b224ccacd7f9d9b78d31cb5a9  engine/live_engine.rs
a012936a92daa305d06314bf9c41e8e398664cbd86797cdcbcbeafb9b40affae  engine/live_engine/anchor.rs
2aefc892bb8efde727d0769a9c5834d7980a1c5ca60faaec6e84412dc010622b  engine/live_engine/commit.rs
5590735645ad3142a7d632e829ef337235091ae72cb5887d16a67fcbf4f4c687  engine/live_engine/observation.rs
2272fa0637b32d97d9092481384a0a5ea1b065347227ce24cb4fae58f2e36681  engine/live_engine/test_seam.rs
2841a8f55bd50945d456551e71d09569369d79234c731ddd70d857eed808441d  engine/live_engine/phase_b_tests.rs
bf4e82d63660894e8355ee7458f4755432a28d290debbe77065230e5f1b50411  main.rs
9acfb52c3cd13da969416cdd169f38fc61ab1f117dfa068c2156e8a9ec0f9bd9  tests/integration_flush_phase.rs
d91711d195c6b224ac290ca59c311e15a3c17b2b54c17422dbfa514236b679ab  tests/integration_epoch_flush.rs
2e8c6e730b04dd4db1ea570944e2c361f0abffbe7a9a91f9e1df0c3928b5c6d2  target/armv7.../fuse_ntfs
```

## Stop point

Phase B ends here. Awaiting independent re-audit. No further production edit,
TASK-005, deployment, or hardware validation until explicit GO.
