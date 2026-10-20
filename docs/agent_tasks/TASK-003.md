# TASK-003

## TASK ID

TASK-003. Status CHANGES_REQUESTED after independent audit; apply targeted [R1–R6 corrections](../architecture/TASK_003_AUDIT.md) within the exact supplemental child-module scope there. Original assignment follows. Specification2026-10-19. Architect produces this contract only; no instrumentation has been implemented or delegated. Stop at patch/handoff for independent audit; no deployment, hardware capture or next task.

## TITLE

Bounded tablet observability for FIFO delivery, FUSE/live reads and watchdog resync decisions, without changing recovery behavior.

## PRIORITY

P0.

## DEPENDENCIES

TASK-001 and TASK-002 AUDIT_PASSED. Read their final audits, TASK-002_SCOPE_ADDENDUM.md, AGENTS.md and this task. Preserve dirty source before editing; capture task-start commit/status/diffs and allowed/read-only hashes. No clean-tree assumption. Approved TASK-001 SHA256d71e04fca90618e0abe4cc860bcd6742524b70cdd6d25e696322a3d6df69803f; TASK-002 submitted live_engine1495ebca,state_machine4b684726,strict4f27319a; integration root076ecbc7,delivery1cc2ed1f,recovery945eac7b. Verify complete hashes against existing handoffs/current tree.

Server-side logging/test startup is deferred: TASK-010 must isolate server import before server instrumentation is assigned. This TASK-003 is a deliberately bounded tablet stage. It cannot independently establish upstream HTTP/DNS failure or TV rendering status.

## PROBLEM

We have host evidence of corrected no-data progress but no correlated trace distinguishing FIFO silence/EOF, reader recovery, FUSE transport errors, watchdog requests and observed gadget state. Existing logs sometimes block under engine locks, and timestamps originate from different clocks. Adding more unbounded eprintln calls would worsen the critical path and still leave ambiguous causality.

## EVIDENCE

Pre-task references, resolve in current Rust repository:

- crates/fuse_ntfs/src/main.rs:113-241 feeder_loop:250ms poll, repeated FIFO opens/EOF100ms retry, flush marker and per2MiB FEEDER log; no structured EOF/error/lifecycle timeline. :298-305 starts feeder; :376-387 FUSE run/teardown.
- engine/live_engine.rs:196-209 existing LIVE_TEL eprintln executes with anchor guard held; around228-261 resync/probe decisions;267-338 confirmed recovery/reference validation; around380-435 NULL starvation; around462-478 successful-media versus observation bookkeeping. TASK-002 implementation must remain behaviorally intact.
- vfs/dispatcher.rs:247-282 serial request loop and errors; :301-310 opcode dispatch; :318-326 reply writes discard result; :480-513 OPEN; :530-560 READ. Low-offset READ logs can be frequent. Current transport fh=1 and backing-image OPEN/RELEASE are not TV-media sessions.
- scripts/tv_watchdog.sh:278-301 marker consumption and resync dispatch; log() uses wall clock; cooldown/breaker decisions partly silent. Existing marker is empty/coalescing and carries no request ID.
- scripts/switch_tv_mode.sh:229-241 waits using existing feeder log text. Inspect exact grep/parser before replacing any operational log.
- docs/architecture/TASK_002_AUDIT.md: final filtered host suite146passed/2ignored/1filtered. Actual USB disconnect versus TV playback loss remains UNKNOWN.

## ROOT CAUSE

CONFIRMED evidence gap: no common event schema/process identity/time origin and incomplete failure-transition observations. This is not a confirmed root cause of USB loss. The task fixes diagnostic ambiguity only, without changing pipeline decisions.

## OBJECTIVE

Produce bounded, loss-accounted events linking tablet FIFO/read/transport observations and watchdog resync decisions by shared boot time, while keeping media bytes, replies, reanchor policy, marker behavior and device lifecycle unchanged.

## ALLOWED FILES

Existing:

- crates/fuse_ntfs/src/lib.rs — export NEW observation module.
- crates/fuse_ntfs/src/main.rs — explicit logger initialization/wiring and FIFO/process events only.
- crates/fuse_ntfs/src/engine/live_engine.rs — observation field/constructor/wiring, replace problematic logs and capture event snapshots only.
- crates/fuse_ntfs/src/vfs/dispatcher.rs — optional observer wiring and transport/lifecycle/read/error events only.
- scripts/tv_watchdog.sh — bounded structured observation plus a safe functions-only test boundary, extraction of existing resync block into a function without policy change.

NEW explicitly authorized files (do not pretend these already exist):

- crates/fuse_ntfs/src/observability.rs — fixed event types, observer interface, bounded queue/counters.
- crates/fuse_ntfs/src/observability/writer.rs — optional cohesive clock/formatter/output-worker implementation.
- crates/fuse_ntfs/src/observability/tests.rs — unit tests with fake clock/sinks.
- crates/fuse_ntfs/src/engine/live_engine/observation.rs — optional cohesive observation-only constructor/snapshot helpers, child module of live_engine; not a place to move/change playback logic.
- crates/fuse_ntfs/src/vfs/dispatcher/observation_tests.rs — raw protocol observation tests, cfg(test) child.
- crates/fuse_ntfs/tests/integration_observability.rs — in-memory observer/engine parity, no real FUSE/device paths.
- tests/test_watchdog_observability.sh — isolated shell harness executing actual sourceable production functions with stubs/temp paths.
- docs/architecture/OBSERVABILITY_EVENT_CONTRACT.md — final implemented schema, clock/correlation limitations and collection instructions for later authorized use.

Only use optional new files if cohesive implementation needs them. No new dependencies or Cargo targets/config changes required. No changes to approved TASK-001/TASK-002 regression sources or the tracker/TS transform/ring/snapper files. Do not modify server.py, stream_fetcher, switch_tv_mode.sh, deployment scripts or original C repository.

## READ-ONLY REFERENCES

All TASK-001/002 tests, engine/{ring,state_machine,snapper,ts_xform}.rs, contract.rs, vfs/fuse_abi.rs, scripts/switch_tv_mode.sh/deploy_tablet.sh, tests/test_vod_lock_and_elf.sh, server.py, stream_fetcher and existing architecture/audits. Original /home/sam/Code/usb-stream-tv-prod read-only in full.

## FORBIDDEN CHANGES

No source-loss/slate/seek/recovery-policy fixes, new epoch allocation semantics, fh/session allocation, changed FUSE wire bytes/flags/short-read behavior, new USB reset, changed retry/poll/sleep/flush timing, marker payload/sidecar/rename protocol, watchdog lock/cooldown/breaker/flag ordering changes, network operations, subprocess diagnostics on hot read paths, ring/queue growth, per-packet logs, credential/path/URL logging, unsafe daemon execution in tests, privileged commands, /data/sysfs writes or checks, production restart/deploy/cleanup. No full server import/tests or unfiltered resync regression execution.

## CURRENT BEHAVIOR

FIFO EOF/error is retried while filesystem stays alive. LiveEngine may request existing marker on behind probes. Watchdog consumes marker before executable/cooldown/breaker decisions in live mode, then may dispatch mode-switch script asynchronously. VOD/favorites suppress the outer branch and leave marker present. A script dispatch is not completed gadget re-enumeration. No end-to-end source generation/session ID exists in this path.

## REQUIRED BEHAVIOR

1. Observation ON and OFF produce identical successful-media state, TS output bytes, NULL phase, FUSE responses, recovery decisions and marker/watchdog actions for identical inputs. Enabled telemetry may drop observations, never block playback waiting for output.
2. No observation formatting, telemetry stderr/file write, worker initialization, sink callback or intentional sleep on engine/FUSE read paths while holding playback locks. Necessary FUSE transport writes retain their existing behavior. Fixed numeric snapshot capture and nonblocking enqueue are permitted; no blocking send or unbounded per-event allocation. Replace existing lock-held LIVE_TEL output rather than adding another synchronous sink there.
3. Logs use shared tablet boot time and process-instance identity. Event IDs are local to a process; no fabricated TV playback-session/channel-generation identifiers. Actual anchor_epoch/file_id/foff/s_write/ring_old must be labeled correctly.
4. FIFO evidence is explicitly FIFO evidence, not proven HTTP/DNS/provider failure. Gadget observation is sampled state, not proof of physical cable action. Missing events/drops cannot prove no reset occurred.
5. Watchdog decision/launch logging preserves all existing control order and values, including marker left intact when VOD/favorites suppress the branch, consumed when entering live decision,120s cooldown and3per900s breaker.
6. Tests execute real observer functions and actual safely sourceable watchdog functions, with temporary paths/stubs. No daemon startup, real marker or real mode script is executed.

## INVARIANTS

TASK-002 successful-progress guard and3request overwritten confirmation untouched. No new IO locks participate in anchor/sm/xform/ring ordering. Observer worker never acquires playback locks or invokes engine callbacks. Logging failure cannot panic, poison engine state, signal restart, create an extra process or cause a retry storm. Existing human operational markers used by scripts remain unchanged. In particular keep the original synchronous [FEEDER] S_write=…MB message and its per2MiB cadence at the existing feeder site, after the ring guard has been released. switch_tv_mode.sh parses this operational readiness signal even when observation is off or dropping events. Never move it into the lossy/rate-limited observer, remove it or reformat it. Its possible blocking is an inherited, explicitly documented exception; added asynchronous observation must not own the readiness signal. Other inherited synchronous logs outside this task must be disclosed, not used to claim the entire system is now nonblocking.

## PROPOSED DESIGN

### A. Explicit bounded observer

NEW Observer is cheaply cloneable at startup, not cloned/allocated per TS packet. NEW Event is a fixed-size Copy enum/record of numeric values and small enum reasons; no free-form String, URL, path, provider/channel name or raw error text. Max event payload256bytes; bounded sync_channel capacity128, producer uses try_send; atomic counters distinguish rate-suppressed, queue-full, uninitialized/disabled and sink-write errors. Do not hide counters by recursively logging them into a full queue.

Define Observer::disabled() and explicit startup constructor returning Result, plus emit(&self,event) with no output callback on caller. Names are NEW design API, not references to existing functions. Existing LiveEngine::new, FuseSession::new and dispatcher::run retain their APIs/behavior with disabled observer. Add opt-in constructor/run variants accepting an Observer for production wiring and tests; do not change StreamBackend signatures. Initialize exactly one observer/output worker in main before feeder/FUSE threads; wire clones into engine, feeder and dispatcher. Default binary mode basic; support off/basic with validated configuration read once at startup. No implicit global thread startup from library constructors/tests, no per-read env parsing. Initialization failure degrades explicitly to disabled telemetry without failing playback.

Single worker formats max512byte lines and writes to existing stderr stream. Proposed basic global budget8events/s with burst16, including summaries; sampled read progress at most1/s, FIFO idle at most1/s, drop summaries at most1/s. Lifecycle/control bursts can be lost when budget/queue exhausted: count/report loss, never promise durable critical-event delivery. Rate gates have constant-size state, not a map keyed by every requested inode/offset. Use a fake/injected clock for deterministic boundary tests. Never join indefinitely on a worker blocked on stderr at shutdown; no flush wait on reader/feeder paths. Capture shutdown best-effort; document loss.

Worker stack target<=256KiB and queue<=32KiB payload (128*256), total new telemetry allocations/stack target<=2MiB per process. Actual RSS timing evidence remains required, not inferred from type sizes alone. emit has no user-visible sink IO or intentional blocking; std channel implementation is not a hard realtime latency guarantee.

### B. Clock/schema and correlation

Use Linux CLOCK_BOOTTIME for Rust boot_ms (suspend-aware since boot); watchdog samples /proc/uptime with precision explicitly reported. Convert to u64 safely in Rust/ARM32. On clock failure emit clock=unavailable/boot_ms=unknown via the normal bounded path, not a fabricated zero or realtime value in the same domain. Document and test tiny unsafe libc boundary with initialized timespec/check return/range; no dependency needed. CLOCK_BOOTTIME availability before Linux3.4 is supported by Linux man-pages: https://man7.org/linux/man-pages/man2/clock_gettime.2.html . Actual target runtime remains hardware evidence, not proven by this reference.

Shell parsing must avoid relying on32bit seconds*1000 arithmetic; construct a validated decimal millisecond representation from uptime seconds/fraction or report explicit coarse boot_s/precision. Do not claim millisecond event ordering from centisecond/coarse sampling. Both collectors identify same host boot externally; no comparison of VPS/Python wall time to tablet boot_ms. Per-process instance=(pid,start_boot_time) and event_seq; optional boot identity only if safely available at startup and explicitly represented as unknown when unavailable.

Fixed schema version1, prefix USBTV_OBS, ASCII key=value fields, one event per line, max512bytes. Required common keys: version,component,event,pid,instance,event_seq,clock,boot_ms(or explicit unknown),precision_ms,reason. Optional numeric fields: transport_request_unique,nodeid,fh,anchor_epoch,file_id,foff,len,mapped_spos,s_write,ring_old,real_bytes,null_bytes,wait_ms,errno,dropped. Stable enums only, sanitize/format unknowns deliberately; field/key ordering deterministic for fixtures.

The resync marker remains the existing empty presence signal. Rust request_seq and watchdog observation_seq are separate local identifiers. Marker coalescing/races preclude exact1:1matching: attach only temporal candidate correlation and UNKNOWN attribution where necessary. No token, sidecar, marker JSON or new IPC protocol in this task.

### C. Minimum truthful event sites

| Site | Events / observations |
|---|---|
| main/feeder | PROCESS_START/STOP; FIFO_OPEN_ERROR, FIFO_EOF, FIFO_READ_ERROR, FIFO_IDLE, FIFO_DATA_RESUMED, FIFO_FLUSH_OBSERVED; errno categories/counters and last-data age, no source URL |
| live engine | sampled LIVE_READ_PROGRESS with real versus NULL bytes; RING_UNDERRUN; REANCHOR_COMMIT with actual reason/epoch; OVERWRITTEN_NONCOMMIT; RESYNC_REQUEST_RESULT with existing File::create result observed without changing action |
| FUSE | backing/catalog FUSE_OPEN_REQUEST/RESULT and RELEASE observations; sampled FUSE_READ_RESULT with requested/replied size and request unique; FUSE_DEVICE_READ_ERROR; FUSE_REPLY_WRITE_RESULT when existing write result is short/error; loop termination reason |
| watchdog | RESYNC_MARKER_OBSERVED, RESYNC_DECISION reason=mode_suppressed/missing_switch/cooldown/breaker/launch; GADGET_OBSERVATION on changed sampled state; dispatch_attempt only, not reset_complete |

Use existing logic's reason/return values; do not recompute classification or call evaluate/calibrate twice for logging. Existing reply() already has libc::write result: observe it, but do not add retries/change replies. OPEN still calls existing backend hook in existing order even for invalid-node path; record request/result truthfully, do not silently fix that bug. fh remains existing value. A backing image OPEN is not TV re-open. Build reader events from coherent already-held snapshots rather than independently relocking multiple mutable objects solely to invent consistency.

FIFO idle may be inferred from existing250ms poll loop and time since successful data; choose1s diagnostic threshold, without modifying poll timeout/sleep or declaring source failure. Transition counters/events must not reset playback state. Repeated empty FIFO opens must not produce8logs every100ms: rate limit/coalesce explicitly. No injection or local slate.

Replace existing LIVE_TEL and low-offset FUSE per-read logging with bounded observations retaining useful fields. Keep parser-dependent FEEDER output outside the observer queue, synchronously at its existing unlocked site as required above. Copy small numeric events under existing locks; queue enqueue may occur there only if it cannot run sink/formatting/initialization or block waiting for capacity. Existing lock-held diagnostic eprintln cannot remain as an accidental second sink for the replaced sites.

### D. Watchdog observation without operational changes

Extract existing resync decision block into a NEW function in tv_watchdog.sh, invoked at the same loop point. Its control operations/order remain unchanged. Put just sourceable observation/resync function definitions before startup side effects; add explicit USBTV_WATCHDOG_FUNCTIONS_ONLY=1 import mode returning before any token reads, lock acquisition, state-file creation, network or service commands. In normal execution this flag is unset and existing initialization/loop runs as before. Test/import mode is for host harness only, never set in deployment.

Functions-only harness supplies BASE,STATE,USBD,BB,LOGF and stub functions/commands in an isolated temporary directory BEFORE invoking extracted functions. Pass the primary/fallback switch executable candidates as explicit arguments/dependencies to the extracted resync function, so the harness never probes or invokes hardcoded /system or /data candidates. Normal loop passes exactly the current /system/xbin/switch_tv_mode.sh and BASE fallback in the same precedence; this is dependency injection, not changed production selection policy. Mock cooldown/log/setsid/mode-script dependencies deliberately; assert their call order. Do not copy production function bodies into tests, source the unguarded daemon, or invoke real default paths. An untouched invalid token path in tests must not be read merely by sourcing.

Observation uses the existing log file through bounded shell functions; at most one decision/changed-state line per loop category, with repeated suppression limited to1/s. Read-only sampled gadget state uses current enable/state/LUN presence, classifies unreadable/unknown explicitly; log only allowlisted state and LUN present/empty/unknown, never raw backing path. Poll no faster than existing1s loop. A transient disconnect/reconnect between samples can be missed; document this limitation. No new sleep, state writer, retry, subprocess restart or changed marker policy. Logging failure must not alter cooldown/branch/launch outcome; preserve function return semantics explicitly.

## API / DATA CONTRACTS

Observer/Event/Config/Stats and optional observer-aware constructors are NEW; document exact implemented signatures in OBSERVABILITY_EVENT_CONTRACT.md. Existing public construction and StreamBackend/FUSE wire contracts remain backward compatible. Disabled/new library observers have no worker/sink and no external IO. Test observer takes bounded in-memory event storage or fake sink; instrumented engine calls must exercise actual emit sites, not just formatter fixtures.

Event counts describe observations, not delivery guarantees. Drop/error stats survive a blocked/full/disconnected sink without requiring it to work. Bounded collection APIs are testable without global mutable singleton contamination. Monotonic timestamps represent observations, not media PCR/PTS; anchor_epoch represents mapping epoch, not provider/channel generation. No TV session allocator or new ID in fh.

For watchdog: functions-only import must be side-effect-free; resync/gadget observation functions take their dependency environment from explicit test setup, retain production defaults in normal mode, and use exit-status-neutral telemetry. No eval, credential values or untrusted event reason strings.

## EDGE CASES

- Disabled/uninitialized logger; bad config; queue full; burst; permanently blocked sink; partial/error writes; worker failure; shutdown with unread queue.
- Clock unavailable, fraction parsing, ARM32 rollover/long uptime, same timestamp multiple events, sparse/coalesced marker history, unknown boot identity.
- No producer data, FIFO EOF/reconnect/error/idle/resume, successful flush, zero-copy reader/NULL suffix, unchanged engine epochs on ordinary no-data paths.
- Valid/invalid backing OPEN, RELEASE without a TV session, normal read versus short/error reply write, loop ENODEV/EINTR handling unchanged.
- Marker while live/VOD/favorites; missing switch binary; cooldown false; breaker at boundary; launch attempt; script invocation failure; observer write failure; invalid uptime and unreadable gadget fields.
- Long logging input/sentinel URL/token must never reach output; events accept numeric/enumerated values, not raw source strings.

## TESTS

1. Observer unit tests: fake clock, exact schema/line length, numeric overflow/unknown fields, rate boundary/burst, bounded queue overflow and independently observable drops, disabled no-worker state, sink failures. Fake blocked sink must prove producer try_send paths complete without waiting for sink; deterministic barriers, bounded watchdog timeout to catch hangs. No performance claims based solely on a passing timing assertion.
2. Integration engine parity with enabled in-memory observer versus disabled: same fixtures/read sequence produce bit-identical bytes and matching anchor/cursor/epoch state; events correctly distinguish real/NULL/confirmed-overwritten/noncommit paths. Preserve TASK-001/002 test sources and hashes. No engine path that creates real resync marker is invoked; test resync-event construction statically/purely and mark runtime branch coverage NOT_RUN instead of creating a fake claim.
3. Raw FUSE observation child tests use aligned request buffers and existing test-safe fake fd/socketpair mechanisms; exercise actual hooks and reply outcomes without mounting or /dev/fuse. Positive/negative OPEN outcome and backing-image semantics explicit. If current dispatcher harness lacks safe FD support, implement a bounded local socketpair harness in allowed test module; no production transport redesign.
4. Shell harness imports actual functions only, proves no top-level side effects, and runs fake directory scenarios. Compare expected marker consume/preserve, cooldown calls, breaker counts and launch arguments/order; fake launch records intent and never invokes real scripts. Simulate event-write failure and show identical decisions. Read-only gadget fixtures test transition/unknown behavior. Tests must not remove/create/read the actual /data marker.
5. Deliberate negative controls in an isolated source AND CARGO_TARGET_DIR: bypass queue bound/drop counter; restore lock-held output or inline sink (blocked-sink test must catch producer waiting); change watchdog decision under logging failure; mislabel launch as reset-complete; mix clock domains. Choose at least3meaningful controls supported by actual tests. Record individual Cargo/shell exits; no shared target contamination.

## ACCEPTANCE CRITERIA

- Scope-only diff, no policy/media/transport outcome changes; all approved old tests remain green under safe filter.
- Event semantics/clock/schema validated by tests and final event document; no source/provider/TV/gadget-success claim unsupported by observations.
- Bounded asynchronous Rust output, loss accounting and no sink IO/formatting under playback locks at instrumented sites; no rate-dependent state mutation.
- Watchdog fake tests demonstrate unchanged actions/order and telemetry failure neutrality. Real default-path resync runtime coverage explicitly NOT_RUN.
- New code fully formatted, sizes within budgets below, no new unrelated warnings. Existing legacy fmt/lint remain explicit baseline findings, not reported PASS.
- ARMv7 build verified offline where available. Coverage/mutation/CCN tools not installed by this task; no invented percentages. No hardware or end-to-end USB stability verdict.

## VALIDATION COMMANDS

Inspect tests/harness first. Never execute the real watchdog/default FUSE daemon.

```sh
cargo test -p fuse_ntfs --test integration_observability --offline -- --test-threads=1
cargo test -p fuse_ntfs --offline -- --test-threads=1 --skip probe_isolation_future_and_behind
bash -n tests/test_watchdog_observability.sh
sh -n scripts/tv_watchdog.sh
bash tests/test_watchdog_observability.sh
cargo test --workspace --no-run --offline
cargo build -p fuse_ntfs --release --target armv7-unknown-linux-gnueabihf --offline
git diff --check
git status --short
```

Run new-file rustfmt checks; inspect changed legacy hunks separately. If ShellCheck is already available, check dialect-compatible files and disclose baseline findings; no install needed. Exact expected test total is146plus genuinely new tests, with2known ignored/1filtered remaining visible. Existing frozen test hashes must match. No --ignored/unfiltered workspace/pytest server import. No endpoint/network probes or /data status/cleanup.

## RESOURCE BUDGET

New source/test files<=500physical lines each and formatted; no compacting statements to evade limits. live_engine stays<=500 (current500) by replacing the problematic large log block and keeping logger machinery in the NEW module. If constructor/event-snapshot helpers need space, use the authorized child engine/live_engine/observation.rs with mod observation in the parent; no playback/reanchor/delivery logic extraction or shortening substantive comments is authorized. main.rs<=500 (current388);lib.rs<=500;tv_watchdog.sh<=500(current310). Dispatcher is an existing1012line legacy module: allow at most80net new physical lines for observer wiring/call sites; put new tests in authorized child, no unrelated split/format. Approved TASK-002 state-machine cap is unchanged and file is read-only here.

Capacity128*max256byte events, one worker,<=512byte lines,8events/s basic+burst16 global, fixed rate state, target<=2MiB new memory/stack per process. Instrumentation must not add waits to playback/producer. Warm host parity tests should add no network/device dependence. Report measured event costs, storage rate and process footprint honestly; synthetic timing is not tablet timing. Existing log-file housekeeping stays unchanged; bounds apply to added observation output, not a false claim that all inherited logs are now bounded.

## ROLLBACK

Revert only TASK-003-owned hunks against task-start snapshots and remove only task-created modules/tests/docs when reverting. Never reset whole dirty files. Observer-off is a diagnostic fallback, not proof rollback occurred; no deploy/production change was authorized. Do not clean markers, buffers or user data.

## HANDOFF FORMAT

Starting commit/status/hash inventory; task-only full diff; exact NEW API/wiring; event schema/sample sanitized outputs; clock domains/precision and correlation limits; operational marker/parser compatibility; lock and worker/cancellation analysis; watchdog action-order parity; tests and actual command stdout/stderr/exits; explicit skipped/ignored/NOT_RUN; bounded-resource/rate measurements; isolated negative-control evidence; before/after formatted line counts; baseline fmt/lint findings; deviations/risks. State that no hardware/deploy/source-policy change occurred, and that upstream attribution, physical USB disconnection, TV reopen and T7remain UNKNOWN without authorized runtime evidence. Stop and return patch for audit.
