# TASK-006 — Phase B parser handoff (host-only)

Status: **parser iteration 2 prepared; P1-P3 corrected; NOT integrated into
`server.py`.** This is the parser-preparation scope explicitly allowed by the
Phase A audit (`APPROVED / GO` for TASK-006/A, `NO-GO` for TASK-006/B). The
Phase A snapshot remains frozen. No deploy, production launch, live provider,
USB/gadget, hardware, `/data` marker or C-reference operation was performed.

## 1. Scope and boundary

- Phase A was approved at iteration 3 with:
  - `server.py` `b1c6b14a8ffc4584ec8d584c27456b73bd785b1b314c33d41c3775f2998832cb`
  - `tests/test_server_slate_switch.py` `c2bc0fb0ab03d30ae79518fc23e76f8612572308d65e174db1782f360763a705`
- This iteration adds only a pure module and its tests. `server.py` was not
  edited; Phase A hashes above are unchanged.
- The module is **not imported by `server.py`**, not used by
  `switch_channel()`/`switch_youtube()`, and creates no process, thread,
  socket, timer or network operation. `grep -R ts_candidate_probe server.py`
  returns no match.
- The contract preferred a helper inside `server.py`; the auditor explicitly
  allowed a pure `ts_candidate_probe.py` when that is more testable. That is
  the only shape deviation, and it remains host-only.

Iteration 2 disposition of the parser audit:

| Finding | Disposition |
|---|---|
| **P1:** `audio_pid` could point at AAC while `audio_kind="ac3"` | **Fixed.** The parser tracks the selected AC-3 PID; mixed AAC/AC-3 tests assert the AC-3 PID in both declaration orders. |
| **P2:** a single oversized `feed()` was copied before the cap check | **Fixed.** `feed()` checks the remaining capacity before `extend()`; a guarded-buffer test proves no copy occurs. |
| **P3:** READY discarded bytes after the selected region | **Fixed.** `ProbeResult` now exposes absolute `selected_start`/`selected_end` and a bounded `trailing` byte string; a test feeds IDR plus the next PUSI and later packets in one call and verifies no loss or duplication. |
| Registration descriptor used substring matching | **Tightened.** Only the four-byte `AC-3` format identifier at the start of the registration descriptor is accepted. |

## 2. Final artifacts and hashes

```text
add39d88cace9aab51e3b4cc09452f21b4785840fc5a820a88d49f0406fb0f6b  ts_candidate_probe.py
3ab467dc86f6b623473901c69b10a6c673b0b4acdfd116156df8e0025be17b73  tests/test_ts_candidate_probe.py
b1c6b14a8ffc4584ec8d584c27456b73bd785b1b314c33d41c3775f2998832cb  server.py (frozen Phase A)
c2bc0fb0ab03d30ae79518fc23e76f8612572308d65e174db1782f360763a705  tests/test_server_slate_switch.py (frozen Phase A)
046e98c71a9a87aea6f2edad379a55c0593a1959074077816231de19a2ea66b2  docs/agent_tasks/TASK-006.md
```

Line counts:

```text
500  ts_candidate_probe.py
466  tests/test_ts_candidate_probe.py
10826 server.py
404  tests/test_server_slate_switch.py
262  docs/agent_tasks/TASK-006.md
```

The parser module is exactly 500 lines; the new test file is 466 lines. No
existing test was removed or weakened.

## 3. Public API

```python
class TsCandidateProbe:
    def __init__(self, max_bytes=8 * 1024 * 1024): ...
    def feed(self, chunk: bytes) -> ProbeResult: ...
    def finish(self) -> ProbeResult: ...

@dataclass(frozen=True)
class ProbeResult:
    status: str          # need_more | ready | reject
    reason: str
    selected: bytes|None # only on ready
    video_pid: int|None
    audio_pid: int|None
    audio_kind: str      # unknown | none | ac3 | unsupported
    packets: int
    buffered: int
    selected_start: int  # absolute offset in the fed stream
    selected_end: int    # absolute offset, one past selected
    trailing: bytes      # bytes after selected_end in the same feed
```

`feed()` is streaming and may be called with arbitrary byte boundaries,
including one byte at a time. `finish()` classifies EOF/timeout without
inventing a ready result. `selected` is the packet-aligned region to publish
first; `trailing` is the already-read remainder of that `feed()` call and must
be consumed after `selected` without re-reading or duplicating. The absolute
offsets identify both regions even when leading garbage was skipped.

## 4. Readiness contract for `selected`

A candidate is ready only when `_selected_` is a contiguous packet-aligned
range that:

1. starts on a PAT TS packet;
2. contains a CRC-valid PAT and a CRC-valid PMT for that PAT program;
3. contains H.264 (`stream_type=0x1B`) on the PMT video PID;
4. contains a complete H.264 access unit whose first VCL NAL is an IDR
   (`nal_type=5`), with SPS (`7`) and PPS (`8`) before that IDR in the same
   access unit;
5. has no video-payload packet between the selected PAT and the start of that
   access unit;
6. has no video-payload packet between the selected PMT and the start of that
   access unit; and
7. ends at the packet boundary immediately before the next video PUSI. The
   trailing bytes may include PSI/audio packets, but never video from the next
   access unit.

A keyframe found anywhere in the buffer is insufficient. The parser records
the PAT/PMT packet offsets at the candidate access-unit start and rejects an
access unit whose start is not immediately adjacent to a fresh PAT/PMT cycle.
A test inserts a unique marker in a pre-keyframe non-IDR PES and asserts that
the marker is absent from `selected`.

The selected range is derived from the actual TS bytes, not reconstructed.
`selected_start`/`selected_end` are absolute offsets in the byte stream fed to
the probe. `trailing` contains every byte after `selected_end` that was already
present in that feed, so a future integration can continue without losing the
next PUSI or duplicating it. The test feeds PAT/PMT, IDR, the next PUSI and
following packets in one call and asserts `selected + trailing` is the exact
post-sync byte range, with no loss or duplication.

This is structural readiness, not a general proof of decodability. The
real-fixture tests write `selected` to a temporary `.ts` file and run
`ffprobe` on it; the first reported video frame is `key_frame=1` and
`pict_type=I` for those fixtures.

## 5. MPEG-TS handling

- 188-byte packets, 0x47 sync, three-consecutive-sync acquisition to resist
  false sync bytes; leading garbage is skipped.
- Adaptation-field control and adaptation length are honored; payload-less
  packets are ignored. PUSI/pointer-field handling is implemented for PSI.
- PAT/PMT sections are reassembled across packets and across reads; multiple
  sections in one payload are drained. CRC32/MPEG-2 is checked.
- PAT and PMT `current_next_indicator` is checked. The PMT program number must
  map to the PID in the parsed PAT.
- PMT ES descriptors are bounded and parsed; H.264 is accepted only as
  `0x1B`.
- Video PES headers may be split across TS packets. The PES elementary stream
  is assembled to the next PUSI, then NAL start codes (3- or 4-byte) are
  scanned with emulation-safe start-code search.
- NAL types 1 and 5 are VCL; the first VCL must be 5. SPS/PPS must appear
  before that first VCL. Forbidden-bit NALs are rejected.
- `PES_packet_length` is honored when non-zero; live length 0 is handled from
  the next PUSI.

## 6. Audio policy

The policy is explicit:

- PMT with no audio ES: accept, report `audio_kind="none"`, `audio_pid=None`.
  This matches `build_ffmpeg_cmd()`'s optional `0:a:0?` mapping.
- AC-3 as `stream_type=0x81`: accept.
- Private `stream_type=0x06` with AC-3 descriptor tag `0x6A`, or registration
  descriptor tag `0x05` whose first four bytes are `AC-3`: accept as AC-3.
- AAC (`0x0F`), MPEG audio (`0x03`/`0x04`), E-AC-3 (`0x87`), and other known
  audio types: reject as `unsupported_audio` unless an AC-3 stream is also
  present.
- Private `0x06` without an AC-3 descriptor: reject as `unsupported_audio`;
  it is not silently treated as video-only.
- If at least one AC-3 stream is present, `audio_kind="ac3"` and the reported
  `audio_pid` is the selected AC-3 PID, never a different audio stream that
  happened to appear first. Mixed AAC/AC-3 PMTs are tested in both declaration
  orders.

The local FFmpeg fixtures were inspected with `ffprobe`: video is
`codec_name=h264`, `id=0x100`; audio is `codec_name=ac3`, `id=0x101` for the
audio variants. The video-only fixture has only video.

## 7. Bounds and failure classification

- `max_bytes` defaults to 8 MiB and must hold at least four TS packets.
- `feed()` checks `len(chunk) > max_bytes - len(buffer)` **before** copying the
  chunk. An oversized single feed therefore rejects with `buffer_limit`
  without first allocating/copying it. A guarded-buffer test fails if
  `extend()` is reached.
- The raw TS buffer is capped at `max_bytes`; the current video PES is capped
  at the same bound; `selected` and `trailing` are bounded copies. No
  unbounded growth is possible before ready/reject.
- Reject reasons: `empty`, `no_sync`, `truncated`, `missing_pat`,
  `missing_pmt`, `no_h264`, `unsupported_audio`, `no_video`, `no_keyframe`,
  `incomplete_access_unit`, `buffer_limit`.
- `need_more` reasons include `waiting_for_pat`, `waiting_for_pmt`,
  `waiting_for_video`, `psi_not_adjacent`, `no_keyframe`.
- The module has no process/thread/socket/network ownership, so it cannot kill
  or replace the old live process. Integration and old-process behavior remain
  a separate Phase B step.

## 8. Tests and evidence

Parser suite:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  tests/test_ts_candidate_probe.py
=> 28 passed (repeated 5 times: 28 passed each run)
```

The 28 tests cover:

- 1-byte and mixed chunk feeds; PAT/PMT fragmentation; pointer fields;
  split PES headers; 3-/4-byte NAL start codes; adaptation fields;
- real FFmpeg transcode, ECO copy (`dump_extra=freq=keyframe`) and video-only
  profiles with the same PMT/PID/mux flags as `build_ffmpeg_cmd()`;
- AC-3 `0x81`, private `0x06` with `0x6A`, registration `AC-3`, and mixed
  AAC/AC-3 PMTs in both declaration orders (the reported PID must be AC-3);
- video-only accepted as `none`;
- AAC, E-AC-3 and private `0x06` without descriptor rejected;
- no IDR, IDR without SPS/PPS, non-IDR first VCL, and keyframe after video
  without fresh PSI rejected;
- fresh PAT/PMT before a later keyframe recovers selection, with the
  pre-keyframe marker absent from `selected`;
- empty/random/truncated/no-PAT inputs, bad PAT CRC, false-sync prefixes,
  non-H.264 PMT and buffer cap rejected;
- single oversized chunks rejected before `extend()` (guarded buffer);
- absolute `selected_start`/`selected_end` and lossless `trailing` verified
  with and without leading garbage;
- real selected ranges start with PID 0 and decode in `ffprobe` with first
  video frame `key_frame=1`, `pict_type=I`.

Full Python suite:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests
=> 230 passed, 41 subtests passed
```

Rust regression (no Rust source changed in Phase B):

```sh
cargo test -p fuse_ntfs --offline -- --test-threads=1
=> 215 passed, 0 failed, 3 ignored
```

Shell regression (no shell source changed in Phase B):

```sh
bash -n tests/test_watchdog_observability.sh
sh -n scripts/tv_watchdog.sh
bash tests/test_watchdog_observability.sh
=> passed: 56  failed: 0
```

## 9. RED -> GREEN evidence

A disposable naive probe at `/tmp/opencode/task006-phaseb-red` (outside the
repository, never a deliverable) returned ready as soon as it saw an IDR NAL
anywhere in the accumulated bytes and selected the whole buffer. Against the
24-test parser suite at that stage it produced **19 failed / 5 passed**.
Representative failures:

- accepted AAC, E-AC-3 and private `0x06` without descriptor;
- accepted IDR without SPS/PPS and non-IDR before IDR;
- accepted a keyframe after video with no fresh PSI, and included the
  pre-keyframe marker in `selected`;
- accepted false-sync garbage as the selected prefix;
- did not classify truncated input or buffer-limit input;
- failed the real-fixture selected-region `ffprobe` checks.

For the three iteration-1 findings, a second disposable copy at
`/tmp/opencode/task006-phaseb-p1p3` restored the old behavior (first audio PID,
post-copy limit check, no offsets/trailing). The three new regressions failed
there as expected:

- mixed AAC/AC-3: `audio_pid=258` while `audio_kind="ac3"` (must be 257);
- oversized single chunk: guarded `extend()` was reached before rejection;
- offsets/trailing: the old result had no `selected_start`,
  `selected_end` or `trailing`.

The final repository passes **28 / 28** parser tests and **230 / 230** Python
tests. The repository was not reverted for either RED run; only the temporary
copies were changed.

## 10. Audit request and remaining gates

Please inspect via MCP and verify:

1. `selected` semantics: PAT/PMT start, complete IDR AU with SPS/PPS before
   the first VCL, no video before the AU start, and no bytes from the next AU.
2. Resume semantics: absolute offsets, `trailing` preserving every byte after
   the selection, no loss/duplication, and no unbounded copy.
3. PSI fragmentation/pointer/CRC, false-sync resistance, adaptation fields,
   split PES headers/NAL boundaries, pre-copy 8 MiB bound.
4. Audio policy, including `0x81`, `0x06` + `0x6A`/first-four-bytes
   registration, no-audio, unsupported audio, and mixed-audio PID selection.
5. Real-fixture generation matches `build_ffmpeg_cmd()` profiles and the
   `ffprobe` first-frame check.
6. Failure reasons and the absence of any `server.py` integration.
7. Hashes, line counts and test counts above.

TASK-006/B integration, deploy, hardware, gadget and live-provider validation
remain **NO-GO**. The next step after a passing parser audit would be a separate
GO to integrate `wait_for_ready_candidate` into the two switching paths.
