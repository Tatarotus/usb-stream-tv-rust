# TASK-006

## TASK ID

TASK-006

## TITLE

Repair retained Python slate during candidate acquisition and validate candidate readiness

## PRIORITY

P0. Status: **SCOPE APPROVED IN TWO PHASES — Phase A authorized, Phase B
pending a separate implementation review**. No deployment, hardware,
USB/gadget, live-provider or real TV action is authorized by this document.

## DEPENDENCIES

- TASK-003 observation contract (read-only).
- TASK-010 safe Python import boundary: **APPROVED / GO, host-only**.
- TASK-001/002/004/B/005 frozen behavior.

## PHASED APPROVAL

Independent scope review approved TASK-006 in two stages:

- **Phase A (authorized now):** enable keepalive during candidate acquisition,
  make live commit clear stale slate/health under the same lock, and serialize
  `_broadcast()` prebuffer update plus subscriber enqueue so a fallback chunk
  cannot be delivered after the new-channel chunk.
- **Phase B (not authorized yet):** replace first-arbitrary-chunk acceptance
  with a bounded decodable MPEG-TS readiness probe. Phase B begins only after
  Phase A is implemented, tested, handed off and independently audited.

Phase B must also handle AC-3 in a PMT both as `stream_type=0x81` and as
private `stream_type=0x06` with the appropriate descriptors; it must not assume
one PMT encoding. The exact PMT emitted by the local FFmpeg fixture must be
inspected before finalizing that parser.

## OBJECTIVE

Close two confirmed server-side gaps in the existing `StreamHub` slate/live
candidate path without replacing the Python producer, restamper or Rust reader:

1. Candidate acquisition currently suppresses keepalive output. If the old
   source stalls or dies during an 8 s ECO probe / 20 s transcode wait, no
   slate/NULL bytes are published and the TV can freeze until the switch
   finishes or fails.
2. A successful candidate commit does not clear stale slate/health state, so a
   late keepalive tick can publish slate after live commit or leave status
   lying.
3. Candidate acceptance currently treats the first arbitrary byte chunk as
   ready. It must validate a bounded decodable MPEG-TS program (PAT/PMT,
   H.264 SPS/PPS, IDR) before committing, and preserve the old channel if the
   candidate is partial, malformed or times out.

## EVIDENCE

- `server.py:73` sets `SLATE_GAP_THRESHOLD = 8.0`.
- `server.py:2196` is
  `if len(self.subscribers) == 0 or self.switching or self.in_standby: continue`
  in `_keepalive_loop`; `switching` disables slate and NULL fallback.
- `server.py:2325-2343` reads one arbitrary `os.read()` chunk as
  `wait_for_stream_chunk`; `server.py:2363` waits 8 s for ECO and
  `server.py:2389` waits 20 s for transcode.
- `server.py:2412-2436` commits the candidate but does not clear
  `slate_mode`, `stream_health` or `slate_start_time`.
- `server.py:2528-2550` has the same omission on the YouTube commit path.
- `server.py:2030-2051` (`_broadcast`) updates `prebuffer_chunks` under
  `self.lock` but enqueues to subscribers after releasing it. A slate
  publication that started before a live commit can therefore reach a
  subscriber queue after the new-channel publication. The audit requires a
  surgical ordering fix in this path.
- `server.py:1550-1705` (both ECO and transcode) and `generate_slate.sh:51-66`
  use video PID `0x100`, audio PID `0x101`, PMT start PID `0x1000`, H.264,
  AC-3, zero B-frames and repeat headers.
- `docs/architecture/ROOT_CAUSE_ANALYSIS.md:20` records “Slate suppressed
  during concurrent switch” as CONFIRMED with impact HYPOTHESIS.
- `docs/architecture/STREAM_STATE_MACHINE.md:18` records that the current
  Python slate threshold is 8 s and keepalive suppresses output while
  switching.
- `docs/architecture/TARGET_ARCHITECTURE.md:10` requires preventing stale
  slate publication after live commit and validating decodable program
  readiness, not first arbitrary bytes.
- `tests/test_server_control_plane.py` currently has no slate/candidate
  acquisition regression.

## PHASE A REQUIRED BEHAVIOR (AUTHORIZED NOW)

1. While `switch_channel()` or `switch_youtube()` is acquiring a candidate,
   `_keepalive_loop` must remain eligible to publish:
   - paced NULL packets for short old-source gaps; and
   - the existing slate chunks for gaps at/above `SLATE_GAP_THRESHOLD`.
   It must not inject when the old source is healthy (`gap <= 0.6 s`).
2. A successful candidate commit is one atomic state transition:
   - install the new process and identity;
   - clear `slate_mode`, `slate_start_time` and set `stream_health = "ok"`;
   - reset the restamper and publish the candidate bytes;
   - kill the old process only after the commit.
   A keepalive tick that started before the commit must recheck
   `last_chunk_time`/`slate_mode` under `self.lock` and must not publish stale
   slate after the commit.
3. `_broadcast()` must update the prebuffer and enqueue to subscribers under
   the same `self.lock` critical section, so a fallback publication cannot be
   delivered after a subsequent live-channel publication. The queue operations
   remain non-blocking; no unbounded wait may be introduced under the lock.
4. On candidate timeout/failure, `switching` returns to false and the existing
   keepalive/reconnect path remains authoritative. No new automatic USB/gadget
   action is introduced.
5. Existing HTTP semantics, dashboard status fields, channel selection,
   VOD/YouTube behavior and watchdog policy remain unchanged except for the
   corrected `slate_mode`/`stream_health` values at commit.

## PHASE B REQUIRED BEHAVIOR (NOT YET AUTHORIZED)

1. Candidate readiness is bounded and structural:
   - accumulate candidate bytes up to the existing 8 s/20 s deadlines and a
     fixed byte cap;
   - require at least one complete 188-byte TS packet and a valid PAT/PMT;
   - require PMT stream type H.264 (`0x1B`) for video;
   - identify AC-3 either as `stream_type=0x81` or as private `0x06` with the
     appropriate descriptors; inspect the local FFmpeg fixture PMT before
     finalizing the parser;
   - require H.264 SPS, PPS and an IDR access unit on the video PID;
   - reject truncated, non-TS, non-H.264 or non-IDR candidates without killing
     the current live process;
   - log a bounded reason (`SWITCH_CANDIDATE_NOT_READY`) and keep the old
     channel/slate path alive.
2. Candidate bytes that pass readiness are passed to the existing restamper
   once, in order, and then broadcast; no duplicate first chunk and no
   unbounded buffer.
3. On candidate timeout/failure, `switching` returns to false and the existing
   keepalive/reconnect path remains authoritative.

## DESIGN BOUNDARY

Phase A:

- Change only `_broadcast()`, `_keepalive_loop()` and the two commit blocks in
  `switch_channel()`/`switch_youtube()`.
- Keep all restamper/broadcast/process-pointer mutations under the existing
  `self.lock`; keep network/process waits outside the lock.
- No new helper is required for Phase A.

Phase B:

- Add a small pure `TsCandidateProbe` helper in `server.py` with no process,
  thread or network ownership. It consumes bounded byte chunks and returns
  `continue`/`ready`/`reject` plus a reason.
- Replace `wait_for_stream_chunk` with `wait_for_ready_candidate` that
  accumulates chunks and returns the accepted byte payload or a classified
  failure.
- Keep the existing `probe_is_h264` pre-check for ECO, but still validate the
  emitted TS program; transcode candidates are validated the same way.
- Do not add a new state-machine framework, a second writer, a queue resize,
  a ring/feeder change or a local offline slate.

## NON-GOALS

- No TASK-007 offline/tablet-local slate.
- No TASK-008 Return/bookmark policy change.
- No TASK-009 channel latency/generation redesign.
- No Rust source, feeder, ring, pacing, TS-transform or observability-schema
  change.
- No hardware/TV/USB/gadget/deploy/live-provider validation.
- No claim that host validation proves decoder behavior on the Samsung TV.

## ALLOWED FILES

Production, Phase A:

- `server.py` only: `StreamHub._broadcast`, `StreamHub._keepalive_loop`, the
  commit block in `StreamHub.switch_channel`, and the commit block in
  `StreamHub.switch_youtube`. No unrelated refactor.

Phase B, if separately authorized:

- `server.py` only: `TsCandidateProbe` (new helper) and
  `wait_for_ready_candidate` replacement in `switch_channel`/`switch_youtube`.

Tests:

- NEW `tests/test_server_slate_switch.py` (target <= 500 lines).
- `tests/test_server_control_plane.py` only if an existing helper needs a
  regression import/update; no endpoint weakening.
- `tests/test_probe_sar.py` read-only unless a shared fixture helper is needed.

Documentation:

- `docs/agent_tasks/TASK-006.md`
- `docs/architecture/TASK_006_HANDOFF.md`

## TESTS / ENVIRONMENT

Host-only, no live providers or device:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  tests/test_server_slate_switch.py
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  tests/test_server_control_plane.py tests/test_probe_sar.py
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests
```

Phase A required test cases:

1. Keepalive publishes slate while `switching=True` and old source is stale;
   it does not publish when the old source is healthy.
2. Keepalive publishes paced NULLs while `switching=True` for a short gap and
   no slate asset.
3. Successful commit clears stale `slate_mode`/`stream_health` and publishes
   only the new payload.
4. A pre-commit keepalive tick cannot publish stale slate after commit.
5. `_broadcast` queue order matches prebuffer order under concurrent
   fallback/live publication; no queue-full deadlock or blocking wait.
6. No candidate-induced output hole: with a stale old source and a slow
   candidate, at least one fallback chunk is published before commit.

Phase B required test cases (after separate authorization):

7. `TsCandidateProbe` accepts a real ffmpeg-generated H.264/AC-3 MPEG-TS with
   PAT/PMT/SPS/PPS/IDR (fixed PIDs) and rejects:
   - empty/random/truncated input;
   - a TS packet with no PAT/PMT;
   - H.264 video without IDR;
   - non-H.264 PMT stream type.
8. AC-3 detection accepts both `0x81` and private `0x06` with descriptors.
9. Candidate timeout/failure leaves the old process/identity and slate path
   alive; no commit and no process leak.

Fixtures are generated locally with ffmpeg/ffprobe; no external network. If a
fixture cannot be generated on this host, the case is `NOT_RUN` with the exact
reason, not silently skipped.

## ACCEPTANCE CRITERIA

Phase A acceptance:

- `switching` no longer suppresses keepalive when the old source is stale.
- Stale slate/health cannot survive a successful live commit.
- Fallback and live publications cannot be reordered in subscriber queues.
- Failed/timed-out candidates preserve the old channel and the existing
  bounded fallback path.
- Existing Python control-plane, SAR and full suite pass.

Phase B acceptance (after separate authorization):

- Candidate commit requires validated PAT/PMT/H.264 SPS/PPS/IDR, not first
  arbitrary bytes.
- AC-3 detection is not tied to one PMT encoding.
- Failed/timed-out candidates preserve the old channel and the existing
  bounded fallback path.
- Existing Python control-plane, SAR and full suite pass.
- Handoff records exact hashes, line counts, commands, outputs and any
  environment-limited `NOT_RUN`.
- No Rust/shell/C-reference/deploy/hardware action.

## STOP POINT

End with a host-only handoff and independent audit. Do not start TASK-007,
TASK-008 or TASK-009 automatically. Deploy and physical validation remain
**NO-GO**.
