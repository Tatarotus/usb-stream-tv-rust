# TASK-004 — Phase A handoff

> **Phase B update (current tree):** this file is the historical Phase A record.
> The A1 test was strengthened to exercise integral 16/64/128 KiB chunks; the
> raw-injection reproduction is retained as an ignored legacy test. The
> calibration/publication and new-generation interleavings (#2/#4) are now
> covered by in-crate unit tests with a `#[cfg(test)]` seam. Current hashes and
> evidence are in `TASK_004_B_HANDOFF.md`.

Status: **Phase A complete — awaiting auditor review.** Phase B (production
correction) is **not authorized** and nothing under `crates/fuse_ntfs/src/` was
modified by this task.

## Scope delivered

| File | Lines | SHA-256 (prefix) | State |
|---|---:|---|---|
| `crates/fuse_ntfs/tests/integration_flush_phase.rs` | 249 | `158f407e` | new |
| `crates/fuse_ntfs/tests/integration_epoch_flush.rs` | 240 | `0ec432be` | new |
| `docs/agent_tasks/TASK-004.md` | 160 | `561cf8e3` | new |

Production tree: no diff introduced by TASK-004. Existing TASK-001..003 work
remains untouched.

## Test inventory

`integration_flush_phase.rs` — 5 green, 1 RED (ignored):

1. `apply_block_is_byte_exact_for_all_phases_and_fragment_sizes` — 188 phases ×
   7 fragment sizes (1, 187, 188, 189, 16 384, 65 536, 131 072), identity epoch,
   byte-exact output and packet headers preserved. Each of the 16 KiB, 64 KiB
   and 128 KiB sizes is delivered as a real chunk (700-packet, 131 600-byte
   window), not merely as a cap. Payload contains dense fake `0x47` bytes.
2. `d12_snap_finds_true_phase_before_leadback_guard` — `d12_snap` itself locates
   the shifted transport grid for phases 0, 1, 2, 47, 93, 186, 187 (tier
   `Transport`, phase-congruent position).
3. `aligned_phase_zero_anchor_is_unchanged` — negative control: a phase-0
   restart stays on the absolute grid.
4. `fake_sync_bytes_in_payload_do_not_steal_the_anchor` — PAT+SPS tier is
   selected and 8 following slots start with `0x47`.
5. `snap188_is_an_absolute_grid_helper` — pins the primitive.
6. `snap_anchor_preserves_fifo_phase_after_flush_all_phases` — **RED**, ignored.

`integration_epoch_flush.rs` — 3 green, 1 RED (ignored):

1. `flush_during_edge_wait_yields_nulls_with_new_epoch` — after `edge_waits`
   increments, flush wipes the generation; the reader returns a rotated NULL
   sequence and both anchor/xform epochs advance by one.
2. `adjacent_read_without_flush_commits_expected_end` — negative control.
3. `flush_increments_epoch_monotonically_and_clears_generation` — two flushes,
   monotonic epoch, empty ring.
4. `stale_commit_after_flush_is_rejected` — **RED**, ignored.

## A1 — reproduced

Command:

```
cargo test -p fuse_ntfs --offline --test integration_flush_phase \
  snap_anchor_preserves_fifo_phase_after_flush_all_phases -- --ignored --test-threads=1
```

Result:

```
187 of 188 phases lost after flush; first 5:
["phase 1: pos=1061072 (pos-phase)%188=187",
 "phase 2: pos=1061072 (pos-phase)%188=186",
 "phase 3: pos=1061072 (pos-phase)%188=185",
 "phase 4: pos=1061072 (pos-phase)%188=184",
 "phase 5: pos=1061072 (pos-phase)%188=183"]
```

Only phase 0 survives. `d12_snap` returns a correct phase-congruent position,
but `snap_anchor` then calls `leadback_guard`, which calls `snap188` and
projects the anchor onto the absolute 188-byte grid. `validate_scar` also
starts from `snap188(pos)`. After a flush the FIFO may resume at any phase, so
the published anchor is misaligned by `phase` bytes (and may not even start
with `0x47`). Not a test artifact: the phase is measured directly on the ring.

## A2 — reproduced and controlled

Command:

```
cargo test -p fuse_ntfs --offline --test integration_epoch_flush \
  stale_commit_after_flush_is_rejected -- --ignored --test-threads=1
```

Result:

```
left: 24064
right: 18446744073709551615
stale commit: in-flight read advanced last_fend after flush
```

Synchronization is explicit, not timing-based: the test holds `engine.xform`,
waits until the reader has copied old-generation bytes (`served_bytes`), starts
`flush()` in another thread, waits until `anchor.active == false`, then releases
the transformer. The in-flight read applies and commits `last_fend = 24064`
after the flush, leaving an inactive anchor that still carries a live commit.

Interleaving coverage:

| # | Interleaving | Result |
|---|---|---|
| 1 | Read using old anchor snapshot + flush | **REPRODUCED** (stale commit) |
| 2 | Flush between calibration and anchor publication | **BLOCKED_BY_TEST_SEAM** |
| 3 | Flush during ring edge wait | controlled, green |
| 4 | Flush during reanchor + new-generation data | not proven; same missing seam as #2 |
| 5 | Stale commit after flush completes | **REPRODUCED** (same test as #1) |

### BLOCKED_BY_TEST_SEAM

For #2/#4 the reader releases the anchor, enters `xform.lock()` and only then
publishes a new anchor. There is no observable state change between calibration
and publication that a host test can wait on without racing the flush. A
`#[cfg(test)]` hook is **not** visible to this external integration-test crate,
so the proposal is explicitly feature-gated (needs separate authorization,
Phase B):

```toml
# crates/fuse_ntfs/Cargo.toml
[features]
test-hooks = []
```

```rust
#[cfg(feature = "test-hooks")]
pub mod test_hooks {
    // Set by the test; the engine parks here after calibration, before
    // publishing the new anchor.
    pub static AFTER_CALIBRATION: std::sync::OnceLock<
        std::sync::Arc<(std::sync::Mutex<bool>, std::sync::Condvar)>,
    > = std::sync::OnceLock::new();
}
```

The engine calls a `#[cfg(feature = "test-hooks")]` wait immediately before
`anchor.active = true`; when the feature is off (production and normal suite)
the hook does not exist and scheduling is unchanged. The integration test runs
with `cargo test --features test-hooks`. This is a pure test hook: it does not
alter production scheduling. No production file was edited for Phase A.

## Full suite and harness

```
cargo test -p fuse_ntfs --offline -- --test-threads=1 \
  --skip probe_isolation_future_and_behind
=> 200 passed, 0 failed, 4 ignored, exit 0
```

The 4 ignored are the 2 pre-existing TASK-001 known bugs plus the 2 new RED
reproductions above. Shell harness:

```
sh tests/test_watchdog_observability.sh
=> passed: 56  failed: 0
```

ARMv7 target build was not rerun in this phase; no production file changed.

## Root-cause summary for Phase B review

1. **Phase loss** — `engine/snapper.rs::leadback_guard` (and the initial
   position in `validate_scar`) re-snaps to the absolute 188 grid instead of
   the stream's detected phase. The feeder must carry the phase across a flush
   (or the snapper must preserve the `d12_snap` congruence).
2. **Stale commit** — `engine/live_engine.rs` commits `last_fend` under
   `anchor.lock()` without checking that the read's captured epoch is still the
   published epoch. A flush between delivery and commit is accepted.
3. **Calibration/publication** — epoch `seq` is computed before the transform
   lock and published later without a generation check.

## Stop point

Phase A ends here. No production change, no hardware action, no deployment, no
real `/data` marker operation. Phase B awaits explicit auditor GO.
