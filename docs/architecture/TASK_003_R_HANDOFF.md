# TASK-003 targeted corrections — handoff for re-audit

Task: `TASK-003` (tablet observability) — outcome was `CHANGES_REQUESTED` (TASK_003_AUDIT.md). This patch applies R1–R6 only, within the exact supplemental child-module scope there. No production behavior, hardware, deployment, marker execution, or next task. Original C remains read-only.

## Scope compliance

- No change to approved TASK-001/TASK-002 regression sources, tracker/TS/ring/snapper, server.py, stream_fetcher, switch scripts, or deployment scripts.
- `live_engine.rs` 500 (<=500); `dispatcher.rs` 1065 (<=1012+80); `main.rs` unchanged by this correction; `tv_watchdog.sh` 444 (<=500). New files <=500 (max 468).
- New files only: `observability/{gate,format,sinks,budget_tests,format_tests}.rs`, `vfs/dispatcher/observation.rs`, `tests/observability/{fixtures,parity}.rs`, plus the master-prompt doc requested separately.
- Frozen files unchanged by this correction; all R edits are in observability, live_engine/observation, dispatcher/observation, watchdog.

## Corrections applied

- R1 — full-width `u64` boot milliseconds in `Gate`; `refill` compares same width; unavailable clock refills nothing. Drops summaries share the global budget via `Worker::spawn`'s shared `global` gate. Slow-sink backlog drains after release; producers never wait. Unavailable-clock summary flood guarded. Default fixed-time 40-event scenario: <=16 total lines.
- R2 — formatter returns `0` for empty/undersized buffers (no panic), clamps caller slices to 512, whole-record overflow drops the record and counts `format_loss` (no silent field loss). `MemorySink` has line + byte caps with `full` counter and counted sink error.
- R3 — `live_read_progress` emitted once per read after delivery from actual values (`real_bytes`, `null_bytes`, `wait_ms`, `anchor_epoch`, `file_id`, `mapped_spos`); cold-starvation and noncommit paths emit their own records; invalid-reference noncommit preserved. Enabled/disabled byte/state parity covered by 5 integration tests.
- R4 — FUSE read result carries requested `len` vs `real_bytes`, offset, fh, request unique; reply-write failure preserves errno (captured before observer calls), expected/actual lengths, request correlation; unexpected device read reports `fuse_device_read_error` + `fuse_loop_exit` unclean. Marker requests truthfully labelled `marker_request_ok`/`marker_request_failed` with errno; no launch claim.
- R5 — watchdog normalizes upper/lower `CONFIGURED/CONNECTED/DISCONNECTED`; LUN presence from attribute content (empty/non-empty/read-error), never `-s`/st_size or raw path. Functions-only import runs no external command and no filesystem probes before the guard. Logging failure (directory as log target) leaves decisions unchanged on every UID.
- R6 — `event_seq` documented as unique per-process allocation id that may reorder across threads; concurrent test demonstrates `[2,1]` while asserting unique IDs. Summary uses sentinel `0`. RSS not measured (structural estimate only).

## Changed file hashes (sha256 prefix) and line counts (final)

```
7fbafc59   462  crates/fuse_ntfs/src/observability.rs
7ba898a7   114  crates/fuse_ntfs/src/observability/gate.rs
155fd16e   163  crates/fuse_ntfs/src/observability/format.rs
0b317573   132  crates/fuse_ntfs/src/observability/sinks.rs
f5c61167   361  crates/fuse_ntfs/src/observability/writer.rs
18cb532e   372  crates/fuse_ntfs/src/observability/tests.rs
fe1b607f   370  crates/fuse_ntfs/src/observability/budget_tests.rs
8db892ca   229  crates/fuse_ntfs/src/observability/format_tests.rs
82231a1c   500  crates/fuse_ntfs/src/engine/live_engine.rs
7d0b67e0   177  crates/fuse_ntfs/src/engine/live_engine/observation.rs
3df0db35  1065  crates/fuse_ntfs/src/vfs/dispatcher.rs
b25a6e8c    94  crates/fuse_ntfs/src/vfs/dispatcher/observation.rs
aaa3a58b   468  crates/fuse_ntfs/src/vfs/dispatcher/observation_tests.rs
c751fa58   302  crates/fuse_ntfs/tests/integration_observability.rs
565db47c   348  crates/fuse_ntfs/tests/observability/fixtures.rs
0d9b09a1   205  crates/fuse_ntfs/tests/observability/parity.rs
d0e48dae   444  scripts/tv_watchdog.sh
dd413a78   454  tests/test_watchdog_observability.sh
bc3e5b36   207  docs/architecture/OBSERVABILITY_EVENT_CONTRACT.md
```

## Tests

New/modified tests: `audit_*`-equivalent coverage in `budget_tests.rs` (9), `format_tests.rs` (12), `tests.rs` (R6 concurrency), `parity.rs` (5 R3), `dispatcher/observation_tests.rs` (6 R4), plus 56 shell assertions (including pending-transition test).

## Validation commands actually run

```
cargo test -p fuse_ntfs --offline -- --test-threads=1 --skip probe_isolation_future_and_behind
  exit 0; 192 passed, 0 failed, 2 ignored (known bugs), 1 filtered.
  targets: lib 147, integration_cont_splice 4, integration_no_data_progress 9,
           integration_observability 9, return_and_channel_switch 3, seek_reanchor 7,
           strict 13 (192 total).

sh tests/test_watchdog_observability.sh
  exit 0; 56 passed, 0 failed.

cargo fmt --check (new files only)
  exit 0 after rustfmt on the 9 new files; legacy files remain non-conforming by design.

cargo test --workspace --no-run --offline
  exit 0.

cargo build -p fuse_ntfs --release --target armv7-unknown-linux-gnueabihf --offline
  exit 0; 11.74 s.

git diff --check
  exit 0.

rustfmt --check on new files (isolated)
  exit 0.
```

## Before / after falsifiers (isolated, separate CARGO_TARGET_DIR)

Before (auditor artifact, TASK_003_AUDIT.md table): 5/5 failed on the prior submission with exit 101 (gate 49.7-day wrap; shared global burst; empty-buffer panic; silent field loss; concurrent inversion `[2,1]`).

After: isolated copy `/tmp/opencode/task003-r-verify`, `CARGO_TARGET_DIR=/tmp/opencode/task003-r-target`, `cargo test -p fuse_ntfs --lib --offline -- audit_ --test-threads=1`: exit 0, 6/6 passed (including a MemorySink bound case).

Mutation check: reintroducing each audited defect (truncated gate timestamp, removed empty-buffer guard, ignored overflow, bypassed global summary budget, unbounded MemorySink) into a second copy makes 5/5 corresponding `audit_*` tests fail (exit 101); the R6 allocation-id test still passes. Restored code passes again. This demonstrates the tests falsify the defects rather than rubber-stamp them.

## NOT_RUN / limitations

- RSS/process footprint: NOT_RUN (structural estimate only: 128-entry queue + 256 KiB worker stack; MemorySink is now bounded, so the bound is defensible, but no measured resident memory).
- Runtime real marker path on `/data/local/tmp/resync_usb`: NOT_RUN (pure event tests + source review only; marker creation semantics documented).
- Hardware / TV / USB / slate / Return / channel-switch latency: NOT_RUN (no device access).
- Mutation score and coverage: NOT_RUN (no tool run; quality-gate design doc exists).
- Clippy whole-workspace: FAILS on pre-existing warnings in frozen/out-of-scope files (e.g. dispatcher tests, ntfs_mft tests). New-code warnings are limited to a handful of casts in observability-adjacent code; no `-D warnings` claim is made.
- Rustfmt whole-workspace: FAILS on pre-existing legacy files by design; only new files are formatted.
- `/home/sam/Code/usb-stream-tv-prod` untouched.

## Known deviations

- `fuse_read_result` labels a short reply `short_write` (documented in OBSERVABILITY_EVENT_CONTRACT.md) because the reply itself is a `write(2)` to `/dev/fuse`.
- Per-class `summary` gate exists but worker summaries use only the shared global gate, as required.
- `live_engine.rs` emits `obs_underrun` while holding the ring mutex; emit is bounded and non-blocking, but this is noted for the auditor.

## Rollback

Revert only the R-owned hunks; delete only newly authored files. No `git reset`/checkout of dirty files. No device action occurred.

## F1–F4 + N1–N5 correction pass

- F1: `obs_delivered` moved after Caminho C transform, checkpoint/`last_fend` update and pacing sleep.
- N1: read epoch is snapshotted at read start (`epoch0`) and passed to `obs_delivered`/`emit_read`, so a flush during pacing cannot corrupt the recorded epoch.
- F2/N2: `GADGET_LAST` no longer advanced on gate refusal; `GADGET_PENDING` preserves and retries. Summary accounting only advances `reported` after a successful write; idle worker retries pending summaries. Summary payload is interval (`dropped - reported`).
- F3: `reply()` records errno only for negative `write(2)` returns.
- N3: `live_engine.rs` reduced to 500 lines.
- N4: contract clarified (interval semantics, gadget_enable/gadget_state/lun authorized, admission vs output rate).
- N5: shell harness extended with uppercase CONNECTED/DISCONNECTED, newline-only LUN, directory LUN read_error.

## Final correction pass

- A1: epoch and mapped_spos now captured under the same lock after reanchor.
- A2: reset_obs clears LOGF, GADGET_LAST, GADGET_PENDING.
- B1: live_engine.rs reduced to 500 lines (with trailing newline).
- Suite: 192 pass, 0 fail, 2 ignored, 1 filtered. Shell: 56 pass, 0 fail.

## C1–C4 verification pass

- C1: `summary_recovers_after_sink_failure` in `budget_tests.rs` (370 lines, 9 tests) implements explicit `Condvar` synchronization for worker steps. Verifies `rate_suppressed` per batch (24 after batch 1, 40 after batch 2), parses `dropped` counts numerically (exact 24 interval on recovery, distinct 16 interval on subsequent summary without duplication), and checks `sink_errors >= 1` only after `worker.shutdown(2_000)`, eliminating the notify-before-accounting race.
- C2: `positive_short_write_preserves_zero_errno_and_expected_len` in `observation_tests.rs` (468 lines, 6 tests). Uses non-blocking Unix stream socketpair with bounded buffer to force a positive partial write (`0 < real_bytes < expected_len`). Verifies `errno=0`, `reason=short_write`, `transport_request_unique`, and expected/actual lengths (auditor approved).
- C3: `test_watchdog_observability.sh` (454 lines, 56 assertions) uses a controlled harness clock to eliminate `/proc/uptime` second-transition races. Verifies zero lines emitted during gate refusal, `GADGET_LAST` preserved, `GADGET_PENDING="connected/1/empty"` preserved, and retry emission containing `gadget_state=connected` with pending cleared.
- C4: Handoff reconciled with final hashes, test targets, and line counts (192 Rust tests, 56 shell assertions, `budget_tests.rs` 370 lines / 9 tests, `observation_tests.rs` 468 lines / 6 tests).

## Stop point

No deployment, hardware capture, marker cleanup, or TASK-004 started. Patch is returned for independent adversarial audit.
