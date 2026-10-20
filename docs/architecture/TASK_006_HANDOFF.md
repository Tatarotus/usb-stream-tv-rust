# TASK-006 — host-only handoff (Phase A)

Status: **Phase A corrected after independent audit CHANGES_REQUESTED;
awaiting re-audit.** Phase B readiness parsing is **not integrated** and no
candidate-readiness gate is active. No deployment, production launch, live
provider, USB/gadget, hardware, `/data` marker or C-reference edit was
performed.

## 0. Objective and disposition

| Item | Disposition |
|---|---|
| Keepalive suppressed while `switch_channel()`/`switch_youtube()` acquires a candidate | **Fixed.** `switching` is no longer a keepalive suppression condition. |
| Old source stalls during candidate acquisition | **Covered.** Slate/NULL continues while the old source is unhealthy; a healthy source still receives no fallback output. |
| Stale slate/health after live commit | **Fixed.** Both commit paths call `_mark_live_commit_locked()` under `self.lock`. |
| Pre-commit slate reaches a subscriber after live commit | **Fixed.** `_broadcast()` updates prebuffer and enqueues under the same lock; keepalive rechecks gap/state under that lock. |
| **F1:** watchdog kills the old process during switching | **Fixed.** The watchdog checks `switching` and executes `kill()` under the same lock, so a switch cannot begin between check and signal; it still publishes slate/NULL. `switching` is set under lock before the switch thread starts and cleared under lock in `finally`. |
| **F2:** commit keeps an obsolete health timestamp when the first chunk is partial | **Fixed.** The commit helper refreshes `last_chunk_time`; a partial/empty restamper result cannot leave the old timestamp authoritative. |
| **F3:** failed acquisition mutates visible state | **Fixed.** `last_live_channel` moves to the successful commit; YouTube resets `temporary_retries` only at commit. Failure/timeout preserves the old channel, process and retry count. |
| Candidate accepted as first arbitrary chunk | **Deferred to Phase B**, as required by the independent scope review. |
| AC-3 PMT detection | **Deferred to Phase B.** Must accept `0x81` and private `0x06` with descriptors after inspecting the local FFmpeg fixture PMT. |
| TASK-001..005 / TASK-010 behavior | **Preserved.** No Rust, shell, gadget, watchdog-policy or frozen-file edit. |

## 1. Task-start and final hashes

Task-start for Phase A:

```
937bf965db296ad57a389d54c8235488f6ed7ddc172235265f02cb83b6f2ce3c  server.py
```

Final snapshot:

```
b1c6b14a8ffc4584ec8d584c27456b73bd785b1b314c33d41c3775f2998832cb  server.py
c2bc0fb0ab03d30ae79518fc23e76f8612572308d65e174db1782f360763a705  tests/test_server_slate_switch.py
046e98c71a9a87aea6f2edad379a55c0593a1959074077816231de19a2ea66b2  docs/agent_tasks/TASK-006.md
b9b5f5e0ee175f16dfb7c801ddc4d58cd3cfd0272d85801dd5e647d32bffb40f  tests/test_server_import_isolation.py
c57d55e802e7be71bb5a3ed05e7c951938b5d8fd7dc18706521f6a9fc6cb4a6c  tests/test_torrent_vod_stress.py
cf2fd3c47b0cbffa4f698f26a8df04a8557cb990216fc8d7c1f3170d8df53d50  tests/test_probe_sar.py
fc7da89f5907a2f553a88d95bb602e6b39c5bd9a20c33f1ae9c641627e175989  tests/test_server_control_plane.py
```

Line counts:

```
10826  server.py
  404  tests/test_server_slate_switch.py
  262  docs/agent_tasks/TASK-006.md
```

The handoff itself is not hashed into the implementation snapshot; compute it
after this file is final if needed.

## 2. Code changes

`server.py` only:

1. `StreamHub._keepalive_loop`:
   - removed `self.switching` from the skip condition;
   - rechecks `self.slate_mode` and `time.time() - self.last_chunk_time` under
     `self.lock` before publishing NULL or slate;
   - moves slate-entry state (`slate_mode`, `slate_start_time`,
     `stream_health`) under the lock;
   - keeps the 15 s stuck-upstream watchdog, but checks `switching` and
     executes `kill()` under the same `self.lock`, so a switch cannot begin
     between the check and the signal; the slate/NULL publication path still
     runs in the same tick.
2. `StreamHub._broadcast`:
   - prebuffer append and subscriber `put_nowait` now occur in one
     `self.lock` critical section;
   - queue-full handling remains non-blocking (`get_nowait` then retry);
   - prebuffer/queue order is preserved under concurrent fallback/live
     publication.
3. `StreamHub._mark_live_commit_locked`:
   - clears `slate_mode`, `slate_start_time`, sets `stream_health = "ok"`, and
     refreshes `last_chunk_time` as the authoritative commit timestamp.
4. `switch_channel()`:
   - sets `switching = True` under `self.lock` before creating the worker;
   - clears it under `self.lock` in `finally`;
   - rolls back `switching`/`switch_lock` if thread creation fails;
   - moves `last_live_channel = target_id` from pre-acquisition into the
     successful commit;
   - calls `_mark_live_commit_locked()` before publishing candidate bytes.
5. `switch_youtube()`:
   - same synchronous `switching` lifecycle and rollback;
   - removes the pre-acquisition `temporary_retries = 0`;
   - resets `temporary_retries` only inside the successful commit;
   - calls `_mark_live_commit_locked()` before publishing candidate bytes.

No endpoint semantics, channel policy, VOD behavior, responder policy or
watchdog policy changed.

## 3. RED -> GREEN evidence

A disposable copy at `/tmp/opencode/task006-red` was used only to run the new
tests against the pre-fix behavior. It is outside the repository and is not a
deliverable. The repository itself was not reverted.

Observed RED failures against the pre-fix copy:

- old `switching` skip: `test_keepalive_publishes_slate_while_switching` and
  `test_keepalive_publishes_null_while_switching_without_slate` failed with
  fallback output suppressed;
- old `_broadcast` enqueue outside the lock:
  `test_broadcast_atomic_ordering_with_slow_subscriber` failed with subscriber
  order `[B, A]` while prebuffer order was `[A, B]`;
- watchdog without the `switching` guard:
  `test_watchdog_does_not_kill_old_proc_while_switching` failed because the
  fake old process was killed during acquisition;
- watchdog that checks under lock but calls `kill()` after releasing it:
  `test_watchdog_kill_is_atomic_with_switch_start` observed `[True]` (a switch
  began in the gap) instead of `[False]`;
- commit without timestamp refresh:
  `test_switch_channel_commit_refreshes_timestamp_and_last_live` failed on the
  stale `last_chunk_time`;
- early `last_live_channel`/`temporary_retries` mutation:
  the failure-path tests observed the target/zeroed state instead of the old
  channel and retry count.

GREEN on the final repository: `14 passed` in
`tests/test_server_slate_switch.py`, repeated five times. The final test file
covers slate during switching, NULL during switching, healthy-source silence,
stale pre-commit slate, commit state clearing, atomic broadcast ordering, full
queue non-blocking behavior, watchdog suppression with continued fallback
output, atomic check/kill against a concurrent switch start, successful commit,
failure, and timeout preservation.

## 4. Test results

Targeted:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  tests/test_server_slate_switch.py
=> 14 passed

PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  tests/test_server_import_isolation.py tests/test_server_control_plane.py \
  tests/test_probe_sar.py tests/test_server_slate_switch.py
=> 40 passed
```

Full Python suite:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests
=> 202 passed, 41 subtests passed
```

Rust regression (no Rust files changed):

```sh
cargo test -p fuse_ntfs --offline -- --test-threads=1
=> 215 passed, 0 failed, 3 ignored
```

Shell regression (no shell files changed):

```sh
bash -n tests/test_watchdog_observability.sh
sh -n scripts/tv_watchdog.sh
bash tests/test_watchdog_observability.sh
=> passed: 56  failed: 0
```

The Python suite uses only synthetic local media and a loopback fixture
server. No live provider, external network probe or production server launch
occurred. The ARMv7 `fuse_ntfs` artifact remains
`909e2f066efb4ef2d64034546f11131c52216c920858e6bfec12b71ec3bae898`; no Rust
source was changed by TASK-006 Phase A.

## 5. Constraints and limits

- `switch_channel()` still accepts the first non-empty candidate chunk; Phase B
  will add the PAT/PMT/H.264 readiness gate.
- The 8 s ECO and 20 s transcode deadlines are unchanged.
- `_broadcast()` performs bounded, non-blocking queue operations while holding
  `self.lock`; the full-queue test verifies no blocking wait.
- No hardware/decoder claim is made. Host tests prove Python state and queue
  ordering only.
- TASK-006 Phase A remains uncommitted, like TASK-004/B, TASK-005 and
  TASK-010.

## 6. Audit request

Please inspect through MCP and verify:

1. F1: the watchdog checks `switching` and calls `kill()` under the same lock,
   so no switch can start in the gap; slate/NULL still keeps being published,
   and switching is set before the worker starts and cleared on all paths.
2. F2: a successful commit refreshes `last_chunk_time` even when the restamper
   returns no bytes for a partial first chunk.
3. F3: failure and timeout leave `last_live_channel`, `current_channel_id`,
   `temporary_retries` and the old process unchanged.
4. Keepalive still publishes slate/NULL for a stale old source during
   switching and stays silent for a healthy old source.
5. `_broadcast()` serializes prebuffer and subscriber enqueue without blocking
   or lock inversion.
6. Hashes and test counts above; no TASK-001..005 / TASK-010 regression.

Phase B remains **NO-GO for integration** until a separate parser handoff and
audit. Deploy and physical validation remain **NO-GO**.
