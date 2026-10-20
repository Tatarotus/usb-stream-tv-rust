# TASK-003-R — Independent Adversarial Re-Audit

**Date**: 2026-10-19  
**Auditor**: ChatGPT Web (Principal Systems Architect & Adversarial Auditor)  
**Session**: `https://chatgpt.com/c/6ad6631b-aae8-83e8-a10c-61b675d55e3b`  
**Workspace**: `usb-stream-tv-rust` via MCP bridge (`/home/sam/Code`)  
**Scope**: R1–R6 targeted corrections, read-only  
**Outcome**: `CHANGES_REQUESTED`

---

## 1. Summary & Assessment by Requirement

The new submission resolves several substantial problems identified in the first audit (timestamp width, formatter overflow, bounded memory, unique allocation IDs). However, the observability contract cannot be approved as fully satisfied yet.

| Requirement | Assessment | Findings |
|---|---|---|
| **R1 — Global budget and uptime** | Partial | Full-width timestamps corrected; drop-summary semantics and output-rate guarantee need clarification |
| **R2 — Formatter and bounded memory** | Pass by inspection | Whole-record overflow, buffer bounds and MemorySink caps implemented |
| **R3 — Actual engine outcomes** | Partial | Five parity scenarios added, but the result event precedes final processing |
| **R4 — FUSE error evidence** | Partial | Negative write errors and device-read errors covered; positive short writes remain unverified |
| **R5 — Gadget observation** | Partial | Content-based LUN detection corrected; state-transition regression and coverage gaps remain |
| **R6 — Sequence semantics** | Pass by inspection | Unique allocation IDs correctly documented as potentially reordered |

---

## 2. Findings Requiring Corrections

### F1 — Engine result emitted before processing completes
- **Severity**: MEDIUM · R3
- **Location**: `crates/fuse_ntfs/src/engine/live_engine.rs:436–492`
- **Issue**: The engine invokes `obs_delivered()` before:
  1. Applying the Caminho C transformation.
  2. Updating the checkpoint and `last_fend`.
  3. Completing pacing sleep.
  The copied-byte and synthetic-NULL counts have already been determined, so those quantities are meaningful. Nevertheless, the event is not strictly a completed-read event as promised by the contract.
- **Required**: Move the final result emission after processing completes, or explicitly document it as a pre-completion observation. Add a test that verifies the event's ordering relative to the final state update.

### F2 — Gadget transitions can be permanently skipped
- **Severity**: MEDIUM · R5
- **Location**: `scripts/tv_watchdog.sh:105–108`
- **Issue**: `GADGET_LAST` is updated before checking the observation rate gate.
  Deterministic counterexample: two different gadget states are sampled in the same gate interval. The second observation is suppressed, but its state becomes `GADGET_LAST`. A subsequent sample of that unchanged state will never report it.
  The normal one-second polling loop makes this unlikely in regular operation, but the helper's change-detection contract is still incorrect.
- **Required**: Preserve a pending state when the rate gate refuses an observation, and verify that it is emitted on the next eligible sample.

### F3 — Positive short writes can report stale errno
- **Severity**: MEDIUM · R4
- **Location**: `crates/fuse_ntfs/src/vfs/dispatcher.rs:355–365`
- **Issue**: The reply handler obtains `last_os_error()` for both negative and positive-but-short `write(2)` results.
  A successful partial write does not establish a meaningful new `errno`. This may attach an unrelated previous error to the event.
- **Required**: Record `errno` only for negative returns, define the non-error short-write representation, and test a genuinely positive short return using a safe synthetic descriptor.

### F4 — Drop-summary payload semantics are inconsistent
- **Severity**: LOW–MEDIUM · R1
- **Location**: `crates/fuse_ntfs/src/observability/writer.rs:227–250`
- **Issue**: The writer maintains `reported`, but passes the all-time cumulative drop count into `summary_event()`.
  The contract describes losses observed since the previous summary. Consumers interpreting `dropped` as an interval count would double-count previous losses.
- **Required**: Emit `dropped - reported`, or formally define the field as a lifetime cumulative total. Test two successive summaries with different loss increments.
  The admission-rate and actual sink-write-rate semantics should also be distinguished: queued records admitted across several seconds may be written in a rapid burst after a blocked sink is released.

---

## 3. Remaining Verification Gaps

1. **R5 Harness Gaps**: The R5 harness does not test uppercase/lowercase `CONNECTED` and `DISCONNECTED`, newline-only LUN contents, or the original nonzero-`st_size`/empty-read counterexample. The implementation looks substantially better, but the required regression coverage is incomplete.
2. **Contract Categorical Fields**: The watchdog emits categorical fields such as `gadget_state` and `lun` that are not listed in the contract's optional-key schema (`OBSERVABILITY_EVENT_CONTRACT.md`). The schema should explicitly authorize and enumerate them.
3. **Test Count Accounting**: The handoff reports 189 passing tests, but its displayed per-target breakdown adds up to 193 (144 + 4 + 9 + 9 + 3 + 7 + 13 = 189 vs target list). The aggregate may be correct; its attribution requires reconciliation.
4. **Independent Test Execution**: The MCP connection exposes no recorded test executions and identifies the parent workspace as non-Git. Therefore, the auditor could inspect the submitted files but could not independently verify the command exit codes, frozen-file hashes, full diff or claimed mutation outcomes.

---

## 4. Evidence Accepted Provisionally

- The submitted source provides credible evidence for the full-width rate-gate correction, bounded formatter and collector, expanded parity tests, actual FUSE error reporting, and corrected concurrency semantics.
- The builder-reported ARM build, 189 tests, 50 shell assertions, six falsifiers and five mutation controls remain builder-reported, not independently rerun in this audit.
- RSS, runtime marker behavior, hardware, mutation score and coverage remain appropriately marked `NOT_RUN`.

---

## 5. Decision & Next Steps

- **STATE**: `CHANGES_REQUESTED`
- **BLOCKERS**: F1–F4, remaining R4/R5 regression tests, contract clarification, and corrected test-count evidence.
- **Safety**: No production deployment, device operation, marker execution, source modification or file write was performed.
- **Recommendation**: One more narrowly scoped correction pass (`TASK-003-R2`), preserving TASK-001/TASK-002 frozen sources and all current playback behavior.
