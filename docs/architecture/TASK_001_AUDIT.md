# TASK-001 independent audit — 2026-10-18

Current outcome after R1: APPROVED. Initial review: CHANGES_REQUESTED (history below). Scope: test-only expected-red reproduction artifact; no production implementation reviewed or authorized.

## Independently verified

CONFIRMED: tracked diff excluding integration_seek_reanchor.rs hashes to ac2524f7aa72924890dd5f78ca449ca99636246516e28da0ce70365ec8a1f502, matching handoff. Production engine hashes match reported prefixes: live_engine ec8b7312, ts_xform49774bf5, ringf9a372ab. Existing dirty changes remain. Hashes verify content at audit, not historical absence of transient writes.

CONFIRMED: ran `cargo test -p fuse_ntfs --test integration_seek_reanchor --offline -- --test-threads=1`: exit101;3passed/3failed,2.27s test execution. Failures are precise successful-cursor assertions:

| Case | Actual / expected last_fend |
|---|---|
| aligned |8404944 /8388560 |
| packet multiple |8400592 /8388560 |
| odd phase |8405038 /8388654 |

Controls future_probe_preserves_successful_cursor and real_media_delivery_advances_successful_cursor pass; seek_reanchor_flow passes. Public identity mapping/edge setup is adequate for this local cursor reproduction. It does not prove end-to-end playback reachability, fresh served-reference positions after the manually parked cursor, or hardware behavior.

CONFIRMED: rustfmt check exits1 for pre-existing lines108/115 area, not newly added test block. Separately `git diff --check` passes. Did not rerun workspace/strict suites because handoff reports hardcoded /data marker side effects; targeted suite provides reproduction without intentionally exercising resync. Workspace159pass/3fail/2ignored remains implementer-reported for this delivery, not newly independently rerun.

## Blocking correction R1

TASK-001 EDGE CASES requires repeated NULL-only requests without accidental reanchor. integration_seek_reanchor.rs:302-344 creates an engine, issues one starvation request at320 and drops it. Three separate phase tests do not test repeated requests on shared mutable state.

Add a bounded scenario using the same established engine and at least3 starvation requests before dropping it. Keep requested offsets derived from a fixed pre-starvation mapping, not from the possibly corrupted post-read cursor. A retry of the same writer-edge offset is a valid narrow scenario; document how the state machine classifies subsequent requests. Collect replies/snapshots for all attempts before asserting cursor equality, so the first expected-red assertion does not prevent later attempts executing. Assert full phase-correct NULL output, unchanged anchor/file/epoch and unchanged successful cursor for each observation. Retain current RED cases and controls unchanged. Do not add ignores or production fixes. Continue to respect<=500physical lines in the same allowed file; revised handoff must show actual result and diff.

## Additional observations

CONFIRMED: has_media_packet (:261-265) accepts PAT PID0 as media. Current fixture does contain video/audio, so present reproduction remains valid. Prefer explicitly checking fixture video0x100/audio0x101 (and PES where appropriate) to reject a PAT-only false positive. No new production API is needed.

UNKNOWN runtime served-reference/checkpoint preservation: current assertions inspect anchor/ring only. Checkpoint privacy limitation is correctly disclosed and optional in TASK-001. Do not claim a cursor-only fix establishes checkpoint correctness or safe washed-out recovery. The suggested successful-progress guard has not been executed independently in an isolated copy during this review; it remains a candidate, not an audited fix.

CONFIRMED reported scope deviation: handoff says an existing /data/local/tmp/resync_usb marker was removed. TASK-001 forbids /data actions and requires memory/ordinary-Cargo-output-only side effects. Age alone cannot prove a control marker safe to delete. This audit neither accessed nor recreated that path. Future validation must avoid suites with real device-control paths until sandboxed or explicitly authorized; report potential side effects instead of cleaning them. Do not recreate the marker as rollback without an evidence-based safety review.

## Revalidation / limits

Run revised test target serially offline; retain precise expected-red outcomes and passing controls. Use a narrow Cargo filter for new repeated case and report all collected attempts. Scope-only hash and git diff --check must match. No need to rerun unrelated full suites, hardware, deployment or tool installation. Record missing hardware evidence explicitly. Independent source review plus this targeted execution justifies the cursor regression; TASK-001 remains incomplete until R1 is delivered. No production readiness conclusion.

## R1 re-audit — final disposition

APPROVED, limited to the expected-red test artifact. No production fix, hardware or deployment approval.

CONFIRMED: SHA256 of revised test file d71e04fca90618e0abe4cc860bcd6742524b70cdd6d25e696322a3d6df69803f;489physical lines. Tracked diff excluding the permitted test file retains ac2524f7aa72924890dd5f78ca449ca99636246516e28da0ce70365ec8a1f502 before/after validation. Code review confirms three same-engine attempts at a fixed pre-starvation offset; all observations collected before aggregate assertions. Revised fixture helper requires expected video/audio PID and PES signature, rejecting PAT-only output.

CONFIRMED independent execution: `cargo test -p fuse_ntfs --test integration_seek_reanchor --offline -- --test-threads=1` exits101,3passed/4failed,3.95s. Repeated case reports all three cursor mismatches:8388560->8404944;8388560->8400592;8388560->8404944. Original single-shot failures unchanged; both controls and pre-existing test pass.

CONFIRMED red/green falsification: copied Cargo.toml,Cargo.lock and crates into `/tmp/task001-r1-audit-iv_kcmf8`, guarded only note_ckpt and last_fend updates with to_copy>0 in that disposable copy, leaving last_read_ms observation unchanged. Same seven-test target passes7/7,exit0,3.90s. Main workspace production source unchanged. This validates test sensitivity and acceptance criterion5; it does not validate washed-out recovery or broader correctness of the candidate guard. No full/strict suites executed and no /data path accessed in this re-audit.

CONFIRMED: rustfmt still exits1 only for pre-existing lines108/115 area; git diff --check passes. Independent static reviewer found no blocking R1 issue.

Nonblocking precision note: repeated-test offset8388560 lies48bytes below PROBE_ISOLATION_FOFF8388608. Therefore probe_pos is None, rather than a Some value inside a resident window as the comment suggests. Continue classification and the reproduction remain valid. Correct that prose when this file is next edited; no test behavior change is necessary.

R1 is satisfied; TASK-001 test artifact is AUDIT_PASSED. All production P0 defects and hardware evidence remain open. TASK-002 requires its own precise implementation contract and authorization; do not integrate the disposable audit guard as an approved fix.
