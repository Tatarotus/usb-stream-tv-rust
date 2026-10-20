# TASK-002

## TASK ID

TASK-002. Status: AUDIT_PASSED after R1–R6 and [scope addendum](TASK-002_SCOPE_ADDENDUM.md). See [final audit](../architecture/TASK_002_AUDIT.md) for approval scope and historical formatting baseline. Original assignment contract follows. Specification dated2026-10-19. This document authorizes the implementing agent's scoped patch when assigned by the user; it does not authorize deployment, hardware access or automatic task chaining.

## TITLE

Stop NULL-only successful-progress updates and recover confirmed low-offset overwritten reads without fabricated adjacency/checkpoints.

## PRIORITY

P0.

## DEPENDENCIES

TASK-001 R1 AUDIT_PASSED. Read docs/architecture/TASK_001_AUDIT.md and this contract before implementation. No changes have been implemented by the architect for TASK-002. The temporary audit guard proved test sensitivity, not whole-engine correctness.

Record current HEAD, status, complete task-start diffs and SHA256s before editing. Audited baseline HEAD8581259bed94df3062e2c7bdef2d4707442ca106; dirty tracked diff excluding integration_seek_reanchor.rs hashes to ac2524f7aa72924890dd5f78ca449ca99636246516e28da0ce70365ec8a1f502. Approved TASK-001 test SHA256 d71e04fca90618e0abe4cc860bcd6742524b70cdd6d25e696322a3d6df69803f. If different, inspect and report differences; never overwrite unrelated work or assume hashes imply a clean tree.

## PROBLEM

CONFIRMED: LiveEngine::serve_live records last_fend and note_ckpt after copying zero producer bytes. Simply guarding those statements makes TASK-001 green but breaks washed-out recovery that currently manufactures adjacency from NULLs. One existing strict test additionally relies on a fresh checkpoint stamped with old media after7200s idle. Correct both behaviors while preserving exact media continuity in that bounded recovery scenario.

## EVIDENCE

Paths below resolve in the Rust repo unless explicitly prefixed C. Lines are pre-task working-tree references.

- crates/fuse_ntfs/src/engine/live_engine.rs:339-389 tracks to_copy and fills NULLs;420-434 unconditionally records checkpoint and successful cursor;69-86 and120-125 lifecycle resets.
- Same file:170-181 uses last_fend for seq_behind_ring;243-323 calibrates from the requested mapped position, then publishes new anchor. Thus NULL-generated last_fend currently drives recovery.
- crates/fuse_ntfs/src/engine/state_machine.rs:203-311 classifies probes/seek/resume;218-239 handles high-offset behind/future probes. Keep this existing policy unchanged in TASK-002.
- crates/fuse_ntfs/src/engine/ts_xform.rs:679-748: note_ckpt copies served with a new time; calibrate selects served only near old_pos, then unexpired checkpoints, then ring scan. CKPT_MAX_AGE_MS=30min; SERVED_FRESH_BYTES=256KiB.
- crates/fuse_ntfs/tests/integration_strict_contract.rs:689-837, washed_out_return_falls_back_to_live_edge: NULL read incorrectly advances last_fend and renews checkpoint, then second read recovers with Ckpt reference; exact PTS/PCR/CC asserts are valuable and must survive.
- crates/fuse_ntfs/tests/integration_seek_reanchor.rs:172-489: approved tests independently show3single-shot and1repeated RED; controls and existing test pass. Disposable guarded copy passed7/7. Keep this file unchanged.
- C read-only: /home/sam/Code/usb-stream-tv-prod/src/ntfs/fuse_ntfs.c:1438-1462 gates successful progress on real bytes; :1211 updates request-observation time independently. C is reference evidence, not an unquestionable oracle.

## ROOT CAUSE

CONFIRMED: successful-progress bookkeeping is outside to_copy>0. CONFIRMED coupled defect: fallback test/engine use that false progress to classify subsequent overwritten requests and to refresh stale references. The recovery design below is PROPOSED and must be demonstrated by falsifying host tests; its Samsung compatibility and relation to the reported USB loss remain UNKNOWN.

## OBJECTIVE

Make all TASK-001 tests green and retain bounded, continuously restamped recovery for a confirmed chain of wholly overwritten reads below8MiB, using real delivered state rather than NULL-generated cursor/checkpoint state.

## ALLOWED FILES

Existing files:

- crates/fuse_ntfs/src/engine/live_engine.rs — zero-copy guard and integration of bounded confirmation/calibration.
- crates/fuse_ntfs/src/engine/state_machine.rs — separate request-only tracker, resets, pure unit tests; do not change existing probe/resume thresholds or their decisions.
- crates/fuse_ntfs/tests/integration_strict_contract.rs — only washed_out_return_falls_back_to_live_edge and its directly associated descriptions/header list entry; preserve all other tests and helpers.
- crates/fuse_ntfs/tests/integration_return_and_channel_switch.rs — only washed-out-recovery scenario descriptions/assertions, if necessary to state new3request confirmation truthfully. Preserve unrelated tests.

NEW explicitly authorized path (does not exist at specification time): crates/fuse_ntfs/tests/integration_no_data_progress.rs. Add focused integration cases and bounded fixtures here; do not include! the whole strict suite, as that would also execute device-marker tests.

Do not change a file merely because it appears in the allowed list. Keep changes to the smallest necessary subset.

## READ-ONLY REFERENCES

crates/fuse_ntfs/src/engine/{ring,snapper,ts_xform}.rs; contract.rs; main.rs; vfs/; approved integration_seek_reanchor.rs; integration_cont_splice.rs; Cargo files; scripts/; server.py; all architecture/audit documents. Original /home/sam/Code/usb-stream-tv-prod is entirely read-only.

## FORBIDDEN CHANGES

No production fixes outside allowed files, no reanchor-every-read workaround, no virtual metadata/size/name changes, no feeder throttling, no new dependencies/unsafe/network calls, no gadget/marker/watchdog policy changes, no VOD modifications, no source/slate/channel rewrite, no test ignores/deletions/threshold reductions, no global formatting cleanup. Do not copy the disposable audit patch as a complete solution. Do not execute full workspace tests or the strict probe_isolation_future_and_behind case: they can create /data/local/tmp/resync_usb. No /data/sysfs access, marker cleanup/recreation, mount, daemon launch, production services, device commands or deployment. Task commands below avoid those runtime paths.

## CURRENT BEHAVIOR

NULL-only established-reader replies advance successful cursor/checkpoint despite full NULL output. Request timestamps advance too, which is legitimate observation. Active adjacent overwritten reads already recover via seq_behind_ring; nonadjacent low-offset overwritten reads rely on false successful adjacency. High-offset behind probes reject and may request resync; that pre-existing behavior and two ignored known Return bugs are explicitly outside this task.

## REQUIRED BEHAVIOR

1. With to_copy==0, delivery bookkeeping must not advance last_fend or call note_ckpt. Keep appropriate tail invalidation and existing request-observation timestamp updates; do not freeze StateMachine.last_read_ms or AnchorState.last_read_ms as though they were success clocks.
2. Existing initial/restart/channel/adjacent-ring-overwrite anchor decisions retain their contracts. This requirement guards the post-delivery commit; legitimate separately authorized anchor initialization is not a successful-media claim.
3. Low-offset wholly overwritten, nonadjacent requests need separate bounded confirmation: first2qualifying blocks do not move active anchor, epoch, successful cursor or served reference; exactly the3rdconsecutive qualifying block may recover to a snapped live target if a valid real delivered reference is available. NULLs must never become SmCtx.prev_fend.
4. Fixed-offset retries, overlaps, out-of-order/gapped requests, future/edge starvation or high-offset probes cannot confirm this new recovery. Existing high-offset rejection/resync path remains unchanged; pure state-machine testing may inspect its enum without engine side effects.
5. Confirmed recovery retains last real PTS/PCR/CC continuity even if old ring bytes and authentic checkpoints have expired. Never refresh checkpoint age/limits, mark old bytes as newly served, or globally relax calibrate's freshness rules.
6. If no valid reference exists, do not commit this new warm-recovery anchor or invent timestamps. Return bounded phase-aware NULLs, leave successful cursor/epoch unchanged, reset the confirmation chain and report this explicit unsupported case in handoff. It remains a later recovery-policy issue, not evidence to trigger USB reset.

## INVARIANTS

Successful progress versus attempted-request sequence are distinct. Keep ring allocation bounded128MiB, feeder behavior unchanged, all individual pacing sleeps<=50ms with locks released. No change to188byte phase/old-tail/splice algorithms, PID mapping, or global timestamp math. Do not claim this fixes known arbitrary-phase seams, full network fallback, high bookmarks or USB stability.

Mixed real/NULL replies: this task preserves the existing C-compatible trigger to_copy>0 for successful reply-end bookkeeping; it does not redefine every mixed reply as entirely real media. Explicitly test real bytes plus NULL suffix, and disclose inherited full-reply-end semantics. The new recovery must still validate calibration freshness against genuine served state; if mixed suffix makes that reference invalid, use the noncommit behavior above, not fabricated freshness. Do not silently expand scope into partial-packet repair.

## PROPOSED DESIGN

### A. Delivery guard

In serve_live's final delivery block, gate note_ckpt and last_fend assignment on to_copy>0. Leave request-time observation and zero-copy invalidate_tail behavior separate. Do not feed post-reply full length back as successful progress for zero copy.

### B. Bounded request-only overwritten confirmation

Keep constant-size tracker state in StateMachine, with a small private helper/associated method; new names must be documented as NEW, not described as existing API. Inputs identify active file_id/anchor epoch, foff,len,now_ms and eligibility. Store only generation/file identity, expected next requested end, count and last attempt time. No queue/vector per request.

Eligible only when: active same-file anchor; no existing cold/restart/channel/adjacent-overwrite reanchor decision; ordinary SmDecision::Continue; nonadjacent to successful last_fend; len>0; checked virtual/stream end arithmetic; requested range entirely below8MiB AND entirely older than ring_old (mapped_start<ring_old and mapped_end<=ring_old). Reject overflow rather than saturating a bad request into eligibility. Empty reads clear the new pending tracker without changing successful progress.

A first eligible request begins count1. Advance only when foff exactly equals previous requested end and file/epoch match. Retry/overlap/gap starts a new count1, not count+1. Reset on ineligible/intervening reads, request gap exceeding existing configured resume gap, lifecycle reset (new/open/flush), file/epoch change and after consumption of confirmation. Saturate count at3. Real delivery and any other reanchor must invalidate pending confirmation. Reset via note_open/reset_probe_state and integration where needed; preserve their pre-existing semantics.

Evaluate/update under existing anchor->sm lock order. Do not acquire ring/anchor locks from inside the new pure tracker. Do not claim this solves existing flush/read generation races (TASK-004).

At count3 mark a local, explicit confirmed-overwritten-recovery reason. It must select the same snapped live target used for seq_behind_ring, not the position-preserving ResumeAfterGap target. It must remain a warm splice, not a cold identity reset. Do not overload SmDecision::ConfirmedSeek in a way that changes ordinary seek policy.

### C. Reference from actual delivered position

For this new recovery only, save the old anchor/epoch and successful last_fend before any reset. Compute its stream position with checked old-anchor mapping. Call existing TsXformState::calibrate with that mapped last-successful-end as old_pos and the unchanged successful last_fend as old_fend; the scan window, if used, must correspond to the same reference position. Do not use the current overwritten bookmark as the last-delivered reference.

In the strict fixture, the retained served reference is near the mapped last successful end, so existing Served tier is valid despite expired checkpoint wall-clock age; no new reference getter, checkpoint refresh or ts_xform production modification is necessary. Assert that this actually occurs. Do not apply this calibration-position change globally to other seek/resume paths, whose position-specific behavior is separately tested.

Validate this reference before advancing epoch/publishing anchor for the new recovery. If invalid, take required noncommit path. If valid, preserve existing raw-new-source sampling and begin_splice_epoch behavior, then publish new mapping once. Success cursor advances only after actual media delivery. Do not clear genuine reference state before consuming it.

## API / DATA CONTRACTS

Existing public StreamBackend signatures, SmCtx fields, SmDecision meanings and AnchorState successful cursor semantics remain stable. New tracker fields stay private to engine/state_machine.rs; the caller-facing method may use pub(super) for sibling live_engine access, without exposing a new external public API. Documented inputs/outputs must make eligible versus confirmed distinct. Bool confirmation is sufficient if reason remains explicit in caller; no public transport API change needed. Reset and count are independent of last_fend. Additional pending-state bytes must remainO(1), below128bytes excluding existing state, no per-read allocation.

TsXformState::calibrate, apply_block and note_ckpt are existing APIs; do not change their production implementation or loosen their guards. Calibrate may update reference-selection counters; tests must account for those diagnostic effects separately from served/checkpoint state. Expected invalid reference must not panic, silently reset identity, mutate old successful cursor, or create any file.

## EDGE CASES

- TASK-001 aligned/packet-multiple/odd-phase starvation and fixed-offset repetition remain unchanged and turn green.
- Exactly1/2overwritten requests versus3; same offset repeated; overlap; offset gap; unrelated in-window media/future probe between blocks; boundary at8MiB and mixed range crossing ring_old; length/offset overflow; zero-length request.
- File/epoch switch, open and flush reset; next run starts at1. Unit-test lifecycle logic without default binaries/mounts.
- Fully real delivery, real+NULL suffix, wholly overwritten NULL prefix, NULL-only starvation, producer resumes at unchanged edge offset, and no valid delivered reference.
- Expired7200s genuine checkpoints, successful cursor unchanged through first2NULLs, Served-tier reference at recovery, exact PTS/PCR/CC after recovery and continued playback.
- No change to future-probe1/2block isolation or high-offset enum decisions. Do not execute high-offset engine-resync paths to test their filesystem side effect.

## TESTS

1. Preserve approved integration_seek_reanchor.rs byte-for-byte: all7tests green after implementation.
2. Add pure tracker tests (state_machine.rs test module): exact3confirmation, resets/disqualifiers, checked arithmetic, no fixed-retry accumulation, lifecycle and file/epoch isolation. No IO.
3. Update only washed_out_return_falls_back_to_live_edge and its descriptions: first2fully overwritten reads must remainNULL with original successful cursor/epoch;3rdsequential request recovers once. Expect Served calibration, no increment of checkpoint selection from a forged post-gap record. Retain the7200s gap and exact firstPTS=last-realPTS+3000, PCR offset, per-packet transformed bytes, videoCC and subsequent same-epoch playback assertions. Adjust offsets/index labels consistently; do not drop or loosen their arithmetic.
4. New integration_no_data_progress.rs covers producer restart, mixed replies, invalid reference noncommit, and at least one broken overwritten chain. Fixture<=12MiB normally; wrap fixture may stream bounded chunks past128MiB rather than allocate multiple full-ring captures. Use valid packet/PES fields as parser fixtures; label them host-only rather than claiming playable program.
5. Prove checkpoint nonfabrication independently of cursor guard. Public calibrate with empty scan window can select checkpoint if served is deliberately outside its256KiB proximity and within4MiB checkpoint-distance limit. Arrange authentic checkpoints expired, NULL-only read to a different checkpoint slot/fend, then ask calibration about that attempted end (not just successful last_fend). It must remain invalid, not a freshCkpt result. Keep setup outside recovery eligibility, with mapped start at writer edge; snapshot now_ms and public references/counters as needed. Show that temporarily restoring unconditional note_ckpt in an isolated copy makes this test fail. Do not expose private internals or add artificial checkpoints in production to make an assertion pass.
6. Optional test-description correction: TASK-001 repeated case's comment about probe_pos is known imprecise; that file is frozen for this task, so do not edit it incidentally.

## ACCEPTANCE CRITERIA

- Diff restricted to allowed exact paths; TASK-001 test hash unchanged.
- All7approved tests green; new tracker and no-data cases green, each could fail under its corresponding deliberate defect.
- Washed-out recovery remains functional with exact clock/counter continuity and genuine reference; first1/2speculative blocks never reanchor.
- No forged checkpoint: checkpoint-specific regression rejects unconditional note_ckpt independently from successful-cursor assertions.
- Ordinary request timestamps retain semantics; no new ignore attributes and no unrelated test changes. Two existing ignored high-bookmark regressions remain openly documented as outside scope, not solved.
- Safe commands below pass except documented pre-existing fmt/Clippy findings. No run writes /data/resync marker. Full suite is NOT_RUN, never described as PASS.
- ARM compile checked offline where existing toolchain permits; static DNS/runtime warnings disclosed. No hardware/TV/USB compatibility claim.

## VALIDATION COMMANDS

Run serially for integration tests to bound aggregate live-ring memory. Inspect new tests for IO before running.

```sh
cargo test -p fuse_ntfs --test integration_seek_reanchor --offline -- --test-threads=1
cargo test -p fuse_ntfs --lib --offline -- --test-threads=1
cargo test -p fuse_ntfs --test integration_no_data_progress --offline -- --test-threads=1
cargo test -p fuse_ntfs --test integration_strict_contract --offline -- --test-threads=1 --skip probe_isolation_future_and_behind
cargo test -p fuse_ntfs --test integration_return_and_channel_switch --offline -- --test-threads=1
cargo test -p fuse_ntfs --test integration_cont_splice --offline -- --test-threads=1
cargo test --workspace --no-run --offline
cargo build --workspace --release --target armv7-unknown-linux-gnueabihf --offline
git diff --check
git status --short
```

The strict skip is explicit safety isolation of a known real-path side effect, not an assertion that test passed: report it as1filtered plus2existing ignored; current strict target has14normal tests, so normally13executed when skipping that case, unless test inventory is explicitly changed by this task. No `--ignored` or unfiltered workspace execution. For formatting, use rustfmt --check on actually changed files and report pre-existing versus introduced hunks; do not format whole legacy files. Clippy can be run offline without executing tests; preserve baseline findings and report new warnings separately rather than blanket allow. If any safe listed command fails, report actual failure and do not widen scope or run unsafe substitutes.

## RESOURCE BUDGET

O(1) confirmation state<128extra bytes, no new threads or queues. Keep live_engine.rs<=500physical lines (baseline457); factor the small pure tracker into existing state-machine module rather than crush readable code. New test file<=500lines. Explicit scoped legacy exception: state_machine.rs baseline633 and strict_contract.rs baseline1674 are already oversized; cohesive tracker/unit tests and the single recovery-test revision may grow each by at most150physical lines in this patch, with before/after counts disclosed. No unrelated splitting, comments removal or minification for numeric compliance. integration_return_and_channel_switch.rs must remain<=500. Existing feeder/pacing/edge budgets unchanged; synthetic-clock idle, not7200s real sleep. Use serial Cargo integration tests and one ring per test where feasible.

## ROLLBACK

Revert only TASK-002-owned hunks using exact task-start snapshots/diff; delete only the newly authored test file if reverting it. Never git reset/checkout whole dirty files. Do not modify/delete user artifacts or reference-repository files. No production/hardware action occurred, so no device rollback is authorized or necessary.

## HANDOFF FORMAT

Provide task-start commit, status and allowed/read-only file hashes; exact task-owned diff; modified/new paths; proposed tracker API/state/reset behavior; changed strict-test assertions and why they correct the known bad oracle; actual command stdout/stderr/exit codes; test counts and explicitly filtered/ignored/NOT_RUN checks; checkpoint-specific isolated negative-control result; before/after line counts and resource observations; known limitations, deviations and risks. No raw source credentials. Explicitly state no hardware/deploy/marker access and no claim that P0USB, full Return or visible channel switching is solved. Return the patch for independent adversarial audit; do not start TASK-003 or deploy automatically.

## R6 scope amendment

The exact additional test-module paths and structural-pass permissions in [TASK-002_SCOPE_ADDENDUM.md](TASK-002_SCOPE_ADDENDUM.md) amend ALLOWED FILES for R6 only. No numeric threshold increase or test removal is authorized.
