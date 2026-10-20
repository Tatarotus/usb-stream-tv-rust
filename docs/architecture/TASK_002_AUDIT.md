# TASK-002 independent adversarial audit — 2026-10-19

Current outcome: APPROVED for the scoped host patch after R1–R6 and structural addendum. Earlier CHANGES_REQUESTED findings below are retained as review history. Production code was inspected but not edited by the architect. No device, service, /data, gadget or deployment action was performed. The snapper performance WIP and in-window timeshift policy remain outside scope and unchanged.

## Reproduced baseline and provenance

CONFIRMED source/test hashes match handoff: live_engine5ad32ca5; state_machineba244b1a; strict4f27319a; no_data5ceeafb8; frozen TASK-001d71e04fc (full hashes retained in user handoff). Dirty repository remains on the existing uncommitted worktree. The previous /tmp TASK-001 snapshot is unavailable; complete task-only historical diffs cannot be reconstructed from HEAD alone. Do not claim every unrelated WIP hunk was independently verified against an unavailable snapshot.

CONFIRMED independently ran:

`cargo test -p fuse_ntfs --offline -- --test-threads=1 --skip probe_isolation_future_and_behind`

Exit0;110library+4cont_splice+5no_data+3return/channel+7seek+13strict=142passed;2known ignored;1explicitly filtered real-marker case. Full unfiltered workspace and ignored cases NOT_RUN. ARM build not rerun during this review; implementer reports PASS, functional counterexamples already justify changes.

CONFIRMED: git diff --check passes. rustfmt --check --edition2021 exits1 on all4changed files, with11/25/60/14diff regions respectively (live_engine/state_machine/strict/no_data). Some regions are historical WIP; newly authored no_data file and tracker code also contain formatting differences. The handoff's zero-findings claim is not reproducible. Do not globally format unrelated legacy hunks to conceal the difference.

## Independent falsification, isolated build artifacts

Copied Cargo.toml,Cargo.lock and crates into `/tmp/task002-independent-coblqh5k`. Explicit CARGO_TARGET_DIR points only to its isolated-target directory; the original target/ is never shared. Added audit-only tests in the copy; no submitted source/test files were modified. The six tests below all fail against the delivered implementation. Audit orchestrator captures individual Cargo exits101; shell/Python wrapper completion does not mean tests passed.

| Audit counterexample | Actual failure |
|---|---|
| audit_low_virtual_offset_with_high_stream_position_confirms | Third eligible low virtual-offset request fails to confirm when mapped position is64MiB |
| audit_virtual_range_crossing_floor_cannot_confirm | Third request crossing virtual8MiB floor incorrectly confirms in pure tracker |
| audit_empty_request_breaks_confirmation_at_engine_boundary | Epoch becomes2 rather than staying1 after2blocks->empty read->next block |
| audit_fixture_really_has_parseable_pes_timestamps | Existing stream() fixture has no PES timestamps recognized by production parser |
| audit_invalid_reference_observes_request_clock | Invalid-reference noncommit does not observe request time advanced5s |
| audit_reference_mapping_when_successful_end_precedes_base_foff | Equivalent old-anchor parameterization rejects genuine served reference; epoch stays1 instead of recovering to2 |

Commands in isolated copy: cargo test -p fuse_ntfs --offline --lib -- audit_ --test-threads=1; cargo test -p fuse_ntfs --offline --test integration_no_data_progress -- audit_ --test-threads=1; final reference-mapping case added/run with filter audit_reference_mapping. Results2/2failed,3/3failed,1/1failed respectively. Audit-only source remains in the isolated copy for review; copy tests into an authorized task path only as part of the correction patch, not wholesale (copy exceeds line budget).

## Required targeted corrections

### R1 — Separate virtual offset from stream sequence; validate both ends

HIGH. state_machine.rs:385-389 checks mapped_start+len against both ring_old and8MiB. The8MiB boundary belongs to virtual foff, not stream sequence. Real low-offset reads can map arbitrarily far into a long-running stream; the current condition disables their recovery after mapped positions exceed8MiB. Conversely the tracker accepts a virtual range crossing the floor when its stream mapping is low. At407 virtual foff+len uses saturation, not required checked arithmetic.

Compute virtual_end=foff.checked_add(len), stream_end=mapped_start.checked_add(len). Eligibility requires both arithmetic operations valid, virtual_end<=PROBE_ISOLATION_FOFF, mapped_start<ring_old, stream_end<=ring_old and existing other gates. Do not impose8MiB on stream_end. Use validated virtual_end as next requested end. Preserve high-offset engine probe/resync decisions. Add nonidentity mapping, exact boundary/crossing and virtual-overflow tests.

### R2 — Reset on empty engine reads

MEDIUM. live_engine.rs:140-143 returns before updating the new request tracker. Direct unit testing len0 on the tracker is insufficient: the public engine path leaves pending state untouched. Reset only the pending overwritten confirmation before empty-read return; successful cursor and media reference stay unchanged. Add engine-level2eligible->empty->next eligible test, then verify a fresh3request run is needed. Retain TASK-001 hash.

### R3 — Correct PES fixture and prove the tests reject bad inputs

MEDIUM, test reliability. integration_no_data_progress.rs:83 sets p[3]|=0x40. PUSI belongs in p[1]; p[3] bits instead change scrambling_control. Production ts_pusi reads p[1]&0x40 (ts_xform.rs:110-111). Thus the claimed valid PES/PTS fixture is malformed, and tests operate with reference state lacking parsed PES timestamps.

Fix PUSI in byte1, keep scrambling_control clear and assert actual ts_pes_ts parses fixture video/audio packets and matches intended PTS values. Re-run new integration tests and their negative controls on the corrected fixture, using separate source AND target directories. No claim of a playable program is required.

Strengthen state_machine.rs:764-779 disqualifier tests: starting from count0, false is also returned when a bad request is erroneously accepted as count1. Seed2valid pending requests and inject the disqualifier at the exact expected next foff, then verify no confirmation and fresh chain length3. Cover zero, range crossing, virtual/stream overflow, high virtual offset versus high mapped position and configured gap/lifecycle resets. This is a targeted strengthening, not permission to remove existing tests.

### R4 — Symmetric checked reference mapping

MEDIUM. live_engine.rs:267-268 uses base_spos.checked_add(old_fend.saturating_sub(base_foff)). For old_fend<base_foff, this maps to base_spos instead of subtracting the backward distance. Earlier in-window real reads can put the successful cursor below a nonzero anchor offset. The existing normal read mapping already distinguishes both directions.

Use checked_add for forward direction and checked_sub for backward direction; reject underflow/overflow through the defined noncommit path. Add a reference-preserving test using an equivalent reparameterization: base_foff4MiB,base_spos=old_base+4MiB,successful fend2MiB. Mapping for every ordinary read is unchanged; confirmed recovery must retain genuine Served continuity after expired checkpoints. Audit copy reproduces the existing failure without touching source timestamps/PTS rules.

### R5 — Preserve observation-clock semantics on new noncommit paths

MEDIUM. live_engine.rs:272-275 and331-335 return before the ordinary last_read_ms update at477 and zero-copy tail invalidation at467. Request time is observation, not successful progress: refusing a new anchor must not make an active reader appear idle. The invalid-reference test currently inspects cursor/epoch/mapping only.

On the NEW overwritten-recovery noncommit paths, record the current request observation time and invalidate partial delivery tail as appropriate while preserving successful cursor, epoch, mapping, served reference and checkpoint freshness. Do not alter inherited high-offset probe behavior incidentally. Add a deterministic clock-shift test:2pending requests,advance virtual clock5s (below configured gap),third request invalid reference; observation time must advance but successful progress must not. Tracker must be consumed/reset. No artificial sleeps or /data operations.

### R6 — Accurate quality handoff

Correct reported formatting status with actual command stdout/stderr/exits and tool version/config. Format only task-authored code, preserving unrelated baseline WIP. Report unresolved baseline diffs as baseline FAIL, not zero findings. Keep line budgets through cohesive code organization; do not minify/remove substantive tests to fit a number. If constraints cannot be satisfied within approved files, report BLOCKED for a scoped contract adjustment rather than editing unapproved files or silently raising limits.

## Nonblocking scope cautions

Global epoch allocation changed from incrementing under anchor lock to local epoch+1 then delayed publication (live_engine.rs:305,362). Simultaneous reanchors could now choose equal epochs. Actual FUSE serving appears serial and existing feeder-flush races are deferred; no runtime failure was established. Explain why this global ordinary-path change is necessary, or limit delayed commit behavior to the new overwritten-recovery branch. Do not claim concurrency correctness from these sequential tests.

The new abandon helper uses virtual phase. Existing normal anchors snap both origins to188byte multiples, so a phase mismatch for valid normal anchor state has not been established; this review does not raise an additional unsupported blocker from arbitrary inconsistent test state.

Actual pending tracker state is several fields inside StateMachine; size_of::<OvwCtx>() measures the input context, not that persistent-state delta. This is a measurement-label limitation, not evidence of unbounded memory. It remains O(1).

## Re-audit gate

Preserve approved TASK-001, exact strict PTS/PCR/CC assertions and current source-loss progress guard. Deliver narrowly corrected R1-R6 with falsifying before/after evidence and safe filtered suite counts. Keep2known ignored cases and1safety filter visible. Retain no-data clock/checkpoint distinctions and invalid-reference noncommit. No automatic source/USB reset, hardware validation or deployment authorized. snapper WIP and in-window policy remain untouched. Independent review is required again before approval.

## R1–R6 follow-up and R6 scope decision

CONFIRMED current corrections match submitted hashes: live_engine1495ebca,state_machine8e541639,no_data56fac6bb,frozen TASK-001d71e04fc. Independent static review of R1–R5 found no additional blocking functional defect: separate checked coordinate ends, empty reset, symmetric reference mapping, and both new noncommit observation/tail paths are present. Reported negative controls were not independently re-executed in this follow-up and remain implementer-reported evidence.

CONFIRMED independently re-ran the safe filtered suite: exit0;110library+4cont_splice+9no_data+3return/channel+7seek+13strict=146passed;2known ignored;1explicit safety filter. No unfiltered marker test or /data access. These results do not approve hardware compatibility or deferred concurrency.

Current outcome remains CHANGES_REQUESTED for R6 only: source/test size and task-authored formatting need resolution. Scope decision: authorize [TASK-002_SCOPE_ADDENDUM.md](../agent_tasks/TASK-002_SCOPE_ADDENDUM.md). New child test modules preserve helpers once in the integration root and private state-machine test access. Do not raise825/620ceilings or dispense with tests. Legacy state-machine cap remains783; every new file<=500; live engine<=500. Parent files retain unrelated historical formatting differences with explicit reporting. Final structural diff and safe tests require re-audit.

## Final structural R6 audit — disposition

APPROVED for TASK-002's bounded host implementation. This is neither a production-readiness verdict for the whole system nor hardware/deployment permission. No implementation changes were made by the architect in this review.

CONFIRMED independently:

- All8submitted hashes match the structural handoff, including frozen live_engine1495ebca, strict4f27319a and TASK-001d71e04fc.
- Sizes: live_engine500,state_machine716,overwritten_tests153,integration root236,delivery207,recovery289. All current approved ceilings are met; no new825/620allowance.
- New child files and wholly authored integration root pass rustfmt --check --edition2021,exit0. Helpers remain once in the integration root,9scenarios are4delivery+5recovery,3tracker tests live under a cfg(test) child with private access. Cargo integration target unchanged; no duplication or extra ignored tests found.
- Safe filtered suite exits0:110library+4cont_splice+9no_data+3return/channel+7seek+13strict=146passed,2known ignored,1explicitly filtered real-marker case. No unfiltered resync test or /data/sysfs/marker access.
- Offline ARMv7 release build for fuse_ntfs exits0 (warm build,0.04s, no warnings emitted). Build success does not prove execution/decoder compatibility on Android/TV.
- git diff --check passes. Independent static reviewer found no new structural blocker. Negative controls reported after moves remain implementer-reported; they were not rerun independently in this final pure-move review.

### Resolution of reported live_engine formatting BLOCKED

Accept unchanged historical formatting as baseline FAIL under the existing addendum; no further formatting or line-limit increase is required for TASK-002. Differing from HEAD does not establish TASK-002 authorship because this repository was already dirty before the task.

All10current rustfmt regions were inspected: imports8/15; cold wait149; SmCtx214; resume selection222; calibration window319; raw window341; collect_raw348; apply_block462; trailing blank499. The corresponding old long imports/wait/context/selection/window/collect/apply statements are present in the retained pre-TASK-002 inspection evidence (original live_engine around8/15,137,200-210,283-310,420-423). In particular apply_block was already a long line before TASK-001. Existing global fmt failure was documented in FORENSIC_AUDIT and TASK_001_AUDIT before this implementation. The boundary whitespace is also not a functional correction requirement.

Thus these regions are not newly introduced task formatting debt requiring a530line file. Current task-authored helpers/branches and extracted files have been inspected; no unresolved authored-format blocker is established. state_machine parent and strict legacy formatting differences remain reported baseline FAIL, not PASS. This decision does not make cargo fmt --all globally green, remove future CI requirements or waive formatting for new code.

### Remaining limits / stop point

TASK-002 is AUDIT_PASSED. Its guards distinguish zero-real-byte progress, its bounded low-offset confirmation uses separate checked coordinates, and genuine delivered-reference/noncommit contracts have host regression evidence. Existing mixed real/NULL reply-end bookkeeping, high-bookmark ignored defects, arbitrary-phase splice risks, concurrency/flush generation races, offline slate/USB incident causality and TV timing remain outside this approval. Resource/coverage/mutation/full-quality/hardware acceptance has not been newly established.

Keep the snapper performance WIP, in-window timeshift policy and consolidated source untouched. No automatic commit, deployment or next-task execution. A later task requires its own user-directed contract; this audit does not initiate it.
