# TASK-001

## TASK ID

TASK-001

## TITLE

Reproduce successful-chain advancement on an established live stream's NULL-only starvation reply.

## PRIORITY

P0. Status READY for test implementation after explicit user instruction. This is a red-regression task, not a production fix or deployment.

## DEPENDENCIES

None. Read docs/architecture/FORENSIC_AUDIT.md and inspect/preserve existing dirty worktree before changing anything. Record source/diff hashes in handoff; do not commit user work incidentally.

## PROBLEM

`LiveEngine::serve_live` claims successful file progress after copying zero producer bytes. This can fabricate adjacency and a fresh checkpoint from old served media. We need an executable falsifying test before choosing a repair, especially because an existing washed-out Return test depends on this incorrect behavior.

## EVIDENCE

- crates/fuse_ntfs/src/engine/live_engine.rs:339-389: edge wait and NULL fill; `to_copy` counts real producer bytes.
- Same file:420-434: zero copy invalidates transform tail, but `note_ckpt`, `last_fend` and `last_read_ms` update unconditionally afterward.
- crates/fuse_ntfs/src/engine/ts_xform.rs:682-692: valid old served state can become a newly dated checkpoint at a different fend.
- C read-only reference `/home/sam/Code/usb-stream-tv-prod/src/ntfs/fuse_ntfs.c:1438-1462`: successful progress gated on real delivery.
- crates/fuse_ntfs/tests/integration_strict_contract.rs:689-755 explicitly uses all-NULL progress/checkpoint to induce second-read recovery; this is a contradictory legacy expectation, not authority to weaken the supplied invariant.
- Current baseline workspace157 pass/2 ignored; explicitly ignored cases both fail. No hardware reproduction is established.

## ROOT CAUSE

CONFIRMED control-flow error: successful-progress bookkeeping is outside the `to_copy > 0` branch. Its causal role in reported TV/USB loss is unresolved. This task must prove the local contract violation only.

## OBJECTIVE

Add deterministic, unignored regression tests which fail on the current implementation because an established active reader's NULL-only reply changes its successful-media chain; add passing real-delivery/probe controls. Deliver a test-only patch and exact red evidence for the architect.

## ALLOWED FILES

Only `crates/fuse_ntfs/tests/integration_seek_reanchor.rs`.

## READ-ONLY REFERENCES

`crates/fuse_ntfs/src/contract.rs`; `engine/live_engine.rs`, `ring.rs`, `state_machine.rs`, `ts_xform.rs`, `snapper.rs` under crates/fuse_ntfs/src; all existing integration tests; docs/architecture/*.md; original C source and tests under `/home/sam/Code/usb-stream-tv-prod`.

## FORBIDDEN CHANGES

No production code, scripts, Cargo settings/dependencies, quality thresholds, ignore attributes, existing test deletion/weakening, generated consolidated file, hardware access, default daemon launch, /data paths or USB reset. Do not fix the bug, change the old strict test, or make a new assertion expect the broken cursor value. Do not install packages.

## CURRENT BEHAVIOR

Established reader reaches mapped writer edge; no producer data arrives. Engine returns a full phase-aware NULL buffer after bounded wait, then records a new fend and potentially checkpoint even though no real bytes were copied. Request-observation time also advances, which is not itself established as a defect. Cold-start early return and isolated-probe paths behave differently and must not substitute for this scenario.

## REQUIRED BEHAVIOR

The new regression's oracle is the supplied invariant: NULL-only established-reader response leaves successful-media `last_fend` unchanged, keeps active anchor/epoch unchanged, leaves previously served media reference intact, and does not fabricate a newly delivered checkpoint. Full byte reply remains required; the test is expected to FAIL on audited code. This task does not decide recovery/adjacency policy for a later real request.

## INVARIANTS

No source modifications; no side effects outside test memory/ordinary Cargo target outputs. Bounded waits/memory. Preserve all pre-existing assertions and existing known-bug inventory. Do not trigger `ResyncRequest` (avoid behind probes). Do not claim a NULL packet carries PCR, keeps the decoder playing, or proves USB attachment.

## PROPOSED DESIGN

Use the file's existing TS fixture machinery where suitable; inspect it before reuse. Construct one LiveEngine, feed packet-aligned synthetic data beyond MIN_STREAM_START, establish an actual media-bearing read, then arrange an adjacent request whose mapped start equals s_write. Read public anchor/ring state to establish exact setup; do not assume leadback or special offsets from test names. Advancing through the existing buffered region or carefully preparing public test state is allowed, but handoff must explain setup and assert the intended active mapping. Avoid long paced loops by using bounded fixture/state setup; no production test seam needed.

Before starving, snapshot active flag/file_id/base_foff/base_spos/epoch_seq/last_fend/last_read_ms and, where public, served-reference values. Fill destination with a poison byte. Stop producer entirely, call serve_live and verify requested length and exact phase-aware NULL content. Place successful-cursor equality assertion so baseline failure is precise; do not let incidental timing or fixture parse failure mask it. Do not require last_read_ms immutability: it is used for request/idle observation and original C updates g_last_file_read_ms before delivery (fuse_ntfs.c:1211). Any future separate successful-media clock needs a distinct contract. RingData::shift_time_back may establish request-time differences without wall-clock sleep, but those differences are diagnostic, not a failure oracle. Checkpoint internals are private: do not expose them merely for this task. If a public calibration scenario can distinguish stale-reference freshness safely, document it as optional extra evidence; otherwise report checkpoint claim as static evidence, not runtime-tested.

Controls: nonempty real delivery advances successful cursor; isolated future probe returns full NULLs while preserving it. Empty-dst behavior is not the starvation case. Reuse one bounded engine per case and release it promptly.

## API / DATA CONTRACTS

Use existing `StreamBackend::serve_live(file_id:u32,foff:u64,dst:&mut[u8],deadline_ms:u64)->usize`, `LiveEngine::new`, public anchor mutex and ring helpers as implemented. Deadline argument is currently ignored by engine; do not assert a1ms passed deadline is enforced. Current500ms edge budget plus pacing can exceed500ms total; choose a generous host upper bound (e.g.3s) only as a runaway guard, not a hardware latency guarantee. Assert logical outcome independently from timing. Acquire/release inspection locks before invoking serve_live; never call it while holding anchor/ring/xform locks.

## EDGE CASES

Cover TS-aligned and odd-phase requests (at least16384 and188-multiple lengths), established-session starvation rather than cold-start, repeated NULL-only requests without accidental reanchor, and successful media after ordinary real delivery in the control. If repeated requests change classification, explain exact branch; do not assert a broader unproved recovery policy. No producer writer runs concurrently in the primary red case, so timeout is deterministic. Future-probe control must be outside+2MiB margin and >=8MiB, nonadjacent, and cannot create a behind-resync marker.

## TESTS

Add distinctly named starvation regressions and real-delivery/probe controls in the allowed existing file. Assertions derive expected foff->spos/NULL phase from actual established mapping, not engine result bytes. Verify actual producer data contains a non-NULL media PID before inducing outage. Current code must fail the new successful-cursor assertion; controls must pass. Run existing integration tests unchanged and preserve their pass/ignored baseline. Do not add an `#[ignore]` to make default Cargo green.

## ACCEPTANCE CRITERIA

1. Full diff changes only allowed test file.
2. Deterministic before-fix regression failure at successful-cursor equality; no compiler failure, panic from setup, timeout/hang or hardware-path access masquerades as reproduction.
3. New controls pass; existing non-new test behavior unchanged.
4. Full workspace output explicitly reports the deliberate new failures and existing2 ignored known failures. Expected red is accepted for a reproduction artifact, never called a passing implementation/release.
5. Test is capable of becoming green under a targeted successful-progress guard; architect evaluates that later in an isolated copy, with recovery contradictions retained visibly.

## VALIDATION COMMANDS

```sh
cargo test -p fuse_ntfs --test integration_seek_reanchor --offline -- --list
cargo test -p fuse_ntfs --test integration_seek_reanchor --offline
cargo test -p fuse_ntfs --test integration_strict_contract --offline
cargo test --workspace --offline
rustfmt --check --edition 2021 crates/fuse_ntfs/tests/integration_seek_reanchor.rs
git diff --check
git status --short
```

Use discovered new test names as Cargo filters to record regression and control results separately. Existing global fmt/clippy failures remain baseline findings; do not fix unrelated files. Do not rerun ignored known failures unless evidence changed.

## RESOURCE BUDGET

Production128MiB ring allocation per engine; no unbounded filler/queues/threads. Keep fixture bounded (prefer <=12MiB unless existing snapper behavior demonstrably requires more). One edge-wait per primary case; no60s real sleeps or network. New test source plus existing file should remain<=500physical lines; if impossible, stop and ask architect for revised test-file scope rather than editing an unauthorized path.

## ROLLBACK

Remove only the newly added hunks from the allowed file using the task diff. Never git reset/checkout the whole dirty file: it already had user modifications before audit. Keep original C read-only. No device rollback is needed because no device changes are permitted.

## HANDOFF FORMAT

Provide starting commit and dirty-file fingerprint; summary; changed files/full diff; test names/fixture/mapping proof; command stdout/stderr and exit codes (regressions versus controls separated); existing-test comparison; memory/time observed; known limitations; deviations; remaining risks. State explicitly EXPECTED_RED_REPRODUCTION and that no fix/hardware validation occurred. Architect returns one audit outcome with scope; a test-artifact approval does not resolve P0 or permit production deployment.
