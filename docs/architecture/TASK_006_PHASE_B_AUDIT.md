# TASK-006 — Phase B parser audit record

Post-audit record; this file is not part of the audited parser handoff snapshot.

## Verdict

**TASK-006/B PARSER: APPROVED / GO** (iteration 2).

**TASK-006/B INTEGRATION: NO-GO** until a separate authorization.

Deploy, hardware, USB/gadget, live provider and physical validation remain out
of scope. The parser and its tests are now the frozen baseline for the next
integration review.

## Audited artifacts

```text
add39d88cace9aab51e3b4cc09452f21b4785840fc5a820a88d49f0406fb0f6b  ts_candidate_probe.py (500 lines)
3ab467dc86f6b623473901c69b10a6c673b0b4acdfd116156df8e0025be17b73  tests/test_ts_candidate_probe.py (466 lines)
5cf0fe2e8af9820c5f1c1d7b723072b95f85c885e33aee394ecd275ad19e116a  docs/architecture/TASK_006_PHASE_B_HANDOFF.md
b1c6b14a8ffc4584ec8d584c27456b73bd785b1b314c33d41c3775f2998832cb  server.py (frozen Phase A)
c2bc0fb0ab03d30ae79518fc23e76f8612572308d65e174db1782f360763a705  tests/test_server_slate_switch.py (frozen Phase A)
046e98c71a9a87aea6f2edad379a55c0593a1959074077816231de19a2ea66b2  docs/agent_tasks/TASK-006.md
```

The audit confirmed that `server.py` contains no import or call to
`ts_candidate_probe.py`.

## Closed findings

- **P1:** `audio_pid` now identifies the selected AC-3 PID; mixed AAC/AC-3
  PMTs are covered in both declaration orders.
- **P2:** `feed()` checks remaining capacity before `extend()`; a guarded
  buffer test proves an oversized chunk is not copied first.
- **P3:** READY exposes absolute `selected_start`/`selected_end` plus
  `trailing`; no bytes already read after the selected region are lost. The
  test covers PAT/PMT, IDR, the next PUSI and later packets in one feed, with
  and without leading garbage.
- The AC-3 registration descriptor is matched by its first four bytes.
- The handoff describes structural readiness, not a general proof of
  decodability; real fixtures are additionally checked with `ffprobe`.

## Evidence

```text
Parser suite: 28 passed, repeated 5 times
Full Python: 230 passed, 41 subtests passed
Rust: 215 passed, 0 failed, 3 ignored
Shell: 56 passed, 0 failed
```

RED baselines were temporary copies outside the repository. The naive
whole-buffer probe failed 19/24 parser tests at that stage; the P1-P3 baseline
failed the three new regressions.

## Conditions for future integration

The next authorization must preserve:

- existing 8 s ECO and 20 s transcode deadlines;
- publishing `selected` then `trailing` exactly once and in order, including a
  possible partial TS packet at the end of `trailing`;
- old process/fallback continuity during timeout, rejection or candidate
  failure;
- no orphan reader threads, unbounded buffers or state changes before commit.
