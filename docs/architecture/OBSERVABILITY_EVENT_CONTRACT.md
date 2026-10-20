# Observability event contract (TASK-003)

Bounded, loss-accounted diagnostics for the FUSE/live pipeline and the tablet
watchdog. Observation is a side channel: it never changes playback, re-anchor,
FUSE reply or resync decisions, and it may be disabled or dropped at any time
without affecting behavior.

## Line format

Every line is one ASCII record on the existing log stream (`stderr` for the
Rust daemon, the watchdog log for the shell), terminated by `\n` and at most
512 bytes:

```
USBTV_OBS version=1 component=<c> event=<e> pid=<n> instance=<pid>.<start|unknown> \
event_seq=<n> clock=<boottime|unavailable> boot_ms=<decimal|unknown> \
precision_ms=<1|10> reason=<token> [<optional numeric keys>]
```

Keys are separated by single spaces, in the order listed here. Optional keys
are omitted when not applicable; they are never emitted as empty or `unknown`.
No free-form text, URLs, paths, tokens, credentials or user strings may appear
in a line: every value is numeric or a token from the enumerations below.

## Required common keys

`version` `component` `event` `pid` `instance` `event_seq` `clock` `boot_ms` `precision_ms` `reason`

## Optional numeric keys

`transport_request_unique` `nodeid` `fh` `anchor_epoch` `file_id` `foff` `len` `mapped_spos` `s_write` `ring_old` `real_bytes` `null_bytes` `wait_ms` `errno` `dropped`

## Event names

| event | component | extra keys |
|---|---|---|
| `process_start` | process | |
| `process_stop` | process | |
| `fifo_open_error` | feeder | errno |
| `fifo_eof` | feeder | |
| `fifo_read_error` | feeder | errno |
| `fifo_idle` | feeder | |
| `fifo_data_resumed` | feeder | |
| `fifo_flush_observed` | feeder | |
| `live_read_progress` | engine | anchor_epoch file_id foff len mapped_spos s_write ring_old real_bytes null_bytes wait_ms |
| `ring_underrun` | engine | null_bytes |
| `reanchor_commit` | engine | anchor_epoch foff mapped_spos |
| `overwritten_noncommit` | engine | foff len null_bytes |
| `resync_request_result` | engine | errno |
| `fuse_open_request` | fuse | transport_request_unique nodeid |
| `fuse_open_result` | fuse | transport_request_unique nodeid fh |
| `fuse_release` | fuse | transport_request_unique nodeid |
| `fuse_read_result` | fuse | transport_request_unique nodeid fh foff len real_bytes |
| `fuse_device_read_error` | fuse | errno |
| `fuse_reply_write_result` | fuse | transport_request_unique nodeid len real_bytes errno |
| `fuse_loop_exit` | fuse | |
| `drops_summary` | process | dropped |
| `clock_unavailable` | process | |
| `calibration_invalid` | engine | |
| `resync_marker_observed` | watchdog | |
| `resync_decision` | watchdog | |
| `gadget_observation` | watchdog | |

`drops_summary` is emitted by the writer thread (never queued) at most once per
second. Its dropped value is the number of producer-side losses observed since
the previous summary (an interval), not the lifetime cumulative total.

## Reason tokens

`none` `sampled` `cold` `restart` `channel_switch` `behind_ring` `resume_after_gap` `confirmed_seek` `mapping_underflow` `reference_invalid` `future_probe` `probe_reject` `eof` `read_error` `interrupted` `flush` `data_resumed` `idle` `marker_consumed` `mode_suppressed` `missing_switch` `cooldown` `breaker` `launch` `launch_failed` `gadget_changed` `gadget_unknown` `open_invalid_node` `open_root` `open_node` `backing_image` `short_write` `write_error` `loop_unclean` `loop_clean` `unknown` `overwritten_recovery` `reanchor` `starve` `resync_request` `marker_request_ok` `marker_request_failed`

## Clock and instance

* `clock=boottime` with `precision_ms=1`: Rust uses `CLOCK_BOOTTIME` (suspend
  aware, monotonic since boot). `clock=unavailable` / `boot_ms=unknown` is
  emitted when the clock cannot be read — never a fabricated zero.
* `precision_ms=10`: the watchdog derives centiseconds from `/proc/uptime` and
  concatenates the validated decimal seconds and fraction digits
  (`<sec><cc>0`), so no 32-bit `seconds * 1000` arithmetic is performed. If the
  reading is invalid it emits `clock=unavailable boot_ms=unknown` and keeps
  `precision_ms=10` (the source resolution does not change, only this read).
* `instance=<pid>.<start_boot_ms>` identifies one process lifetime. The shell
  cannot read its own start time safely, so it emits `instance=<pid>.unknown`.

## Budgets and loss accounting

* One fixed-size event (<= 256 bytes) through a bounded queue of 128 entries;
  producers only call a non-blocking `try_send` (no formatting, no IO, no wait).
* Global token bucket: 8 events/s with a burst of 16, shared by every producer
  *and* the worker, so summaries are inside the same budget. A summary that the
  global budget refuses is counted separately and never retried in a loop.
* Per class: read progress (engine/FUSE) at most 1/s; FIFO idle at most 1/s;
  drop summaries at most 1/s.
* One worker thread formats and writes to the existing stream. A slow or
  blocked sink can only cause losses, never backpressure, and a queued backlog
  drains once the sink is released.
* The global budget of 8 events/s with a burst of 16 limits producer
  *admission* (enqueueing). Events already admitted into the bounded queue are
  written without a second output-time gate, so releasing a previously blocked
  sink can produce a rapid burst of queued lines. This is a rate-contract
  question, not producer backpressure.
* Counters distinguish: emitted, rate-suppressed, queue-full, disabled
  (uninitialized/off), sink-write errors, formatting losses, gated summaries,
  formatted. Counters are exposed as atomics and are never logged through the
  queue itself.
* Lifecycle/control bursts may be lost when the budget or queue is exhausted;
  loss is reported by the next `drops_summary` and the counters.
* `USBTV_OBS=off|basic` is validated once at startup; absent means `basic`.
  `off`, an invalid value, or a failed worker spawn leaves an explicitly
  disabled observer (no thread, no sink, counters only) and never fails
  playback. No environment variable is read per request or per read.

## Formatting and field loss

A record is rendered whole or not at all. The formatter writes into a caller
slice clamped to the contract bound and either returns one complete
`\n`-terminated record or returns `0`, meaning "no record": the caller must not
write the buffer, and no partial or malformed line can reach the sink. A
populated optional key is never truncated and never silently dropped: a record
whose complete key set does not fit is discarded whole and counted as a
formatting loss. The worker never calls a sink with a zero-length record, and
sink storage is bounded (line and byte caps); reaching a cap is a counted sink
error, never backpressure.

An empty or undersized buffer yields no record. `errno` is a signed decimal
using the `UNSET` sentinel for "not applicable"; emitters pass
`raw_os_error().unwrap_or(0)`, so the only value that collides with the sentinel
is never produced.

## Sequence semantics

`event_seq` is a unique per-process allocation id, claimed by the producer
before the clock is captured and before the event is enqueued. Within a single
producer the numbers are strictly increasing, but concurrent producers may
reach the output in a different order than their allocation: `event_seq` is not
an output-ordering guarantee and must not be used to infer wall-clock order
between threads. Only the worker's `drops_summary` uses the sentinel `0`.

## Engine outcomes

* `live_read_progress` is emitted once per read, after delivery, from the values
  that read actually produced. `real_bytes` counts the bytes copied from the
  ring and `null_bytes` the synthetic NULL filler written by the same read; both
  add up to the delivered payload. Copied ring bytes may themselves be NULL
  packets, so `real_bytes` is not a claim about decodable video. `wait_ms` is
  the bounded edge wait actually spent, `anchor_epoch`/`file_id` are the
  identity in effect for the request, and `mapped_spos` is the stream position
  the request mapped to (omitted when the request had no mapping).
* A decision record (`reanchor_commit`, `overwritten_noncommit`) and the result
  record of the same read are separate events with separate reasons.
* `ring_underrun` reports the NULL filler written while starving at the live
  edge, immediately before the result record of the same read.
* Creating the resync marker is a *request* to the watchdog, not a recovered
  stream: `resync_request_result` carries `marker_request_ok` or
  `marker_request_failed` (with `errno` when available). A watchdog launch
  remains a watchdog concern (`launch` / `launch_failed`).

## FUSE transport outcomes

* `fuse_read_result` carries the requested geometry as well as the outcome:
  `transport_request_unique`, `nodeid`, `fh`, `foff` and `len` (requested), plus
  `real_bytes` for the bytes actually placed in the reply payload. A payload
  shorter than requested is labelled `short_write`.
* `fuse_reply_write_result` reports a failed or short `write(2)` with the
  `errno` captured before any observer call, the expected `len`, the actual
  `real_bytes` and the originating request. Observation never retries a write,
  alters reply bytes or changes INIT/open ordering.
* `fuse_loop_exit` reports `loop_clean` for an orderly stop (unmount or signal)
  and `loop_unclean` when the loop leaves on an unexpected device error, after
  the `fuse_device_read_error` record for that error.

## Watchdog observation rules

* `scripts/tv_watchdog.sh` keeps its 1 s loop; observation adds at most one
  line per loop category (marker, decision, gadget), never a new sleep.
* The resync block is an extracted function taking the primary and fallback
  switch-script candidates as arguments. Its decision order, cooldown key,
  breaker, marker consumption and launch semantics are unchanged.
* `USBTV_WATCHDOG_FUNCTIONS_ONLY=1` loads definitions only: no token read, no
  lock, no state directory, no loop, no network. It is a host-test/import mode.
* Gadget sampling is read-only and allowlisted: `state` (configured,
  connected, disconnected, other), `enable` (0/1/unknown) and LUN presence
  (present, empty, unknown). The raw backing path and its size are never
  logged.
* Watchdog-only categorical fields `gadget_state`, `gadget_enable` and `lun`
  carry the tokens above and are the only non-numeric extra keys authorized
  for `gadget_observation`. Accepted values include `gadget_state=unknown`
  and `lun=read_error`/`lun=empty`.

## Worker-generated drop summary

`drops_summary` is produced by the output worker, not by a producer. It keeps
every required common key and carries the sequence sentinel `0`; the payload
field `dropped` counts the producer losses observed since the previous
summary (an interval), not the lifetime cumulative total. The summary itself
is rate limited to one line per second.

## Verification

* `crates/fuse_ntfs/src/observability/tests.rs` — queue, budget, blocked sink,
  clock failure, drop summary, disabled observer.
* `crates/fuse_ntfs/src/vfs/dispatcher/observation_tests.rs` — raw
  `/dev/fuse` open/read/release frames over a `SOCK_SEQPACKET` pair.
* `crates/fuse_ntfs/tests/integration_observability.rs` — engine end-to-end and
  schema parity against this document.
* `tests/test_watchdog_observability.sh` — shell harness for the extracted
  resync function (functions-only import mode, mocked dependencies).
