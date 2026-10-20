# TASK-003 independent adversarial audit — 2026-10-19

Outcome: CHANGES_REQUESTED. Scope: submitted tablet observability patch. No production implementation edit, device, deploy, actual marker or service action by architect. Native parallel read-only reviews covered logger, wiring and watchdog; coordinator executed safe tests and isolated falsifiers. Existing playback-policy change was not established; diagnostics correctness/bounds and missing required tests are the blockers.

## Verified delivery / baseline

CONFIRMED hashes match handoff for observability.rs84c8de4b,writer54c93104,tests f99816ca,engine helper e554a386,FUSE tests0bcc2171,integration4063463f. Frozen state-machine4b684726 and TASK-001d71e04fc also match. New Rust files inspected are<=500lines and rustfmt-clean. git diff --check passes. HEAD is not a clean task baseline; provenance of every historical WIP hunk cannot be established by HEAD diff alone.

CONFIRMED independently executed:

- cargo test -p fuse_ntfs --offline -- --test-threads=1 --skip probe_isolation_future_and_behind: exit0;125library+4cont_splice+9no_data+4observability+3return/channel+7seek+13strict=165passed;2ignored;1explicit safety filter.
- sh -n scripts/tv_watchdog.sh; bash -n tests/test_watchdog_observability.sh; sh tests/test_watchdog_observability.sh: exit0;46passed/0failed. Fake paths/commands only; no real watchdog loop.
- rustfmt --check on6new Rust files: exit0. ARM build/Clippy/workspace-no-run not independently repeated because concrete functional failures already justify corrections; implementer-reported outcomes remain attributed. No measured mutation score/coverage/ARM runtime compatibility claim.

## Independent falsification artifacts

Rust copy: /tmp/task003-independent-gt_muf0o. Copied Cargo files+crates, appended audit-only library tests, explicit CARGO_TARGET_DIR=copy/isolated-target. Main workspace sources and target not used for mutants. Ran cargo test -p fuse_ntfs --lib --offline -- audit_ --test-threads=1: exit101,0passed/5failed. Python orchestration exits are not Cargo test results.

| Counterexample | Confirmed outcome |
|---|---|
| audit_gate_remains_bounded_after_49_days | Gate::new(1),same now=(1<<32)+1000 admits second event without elapsed time |
| audit_drop_summaries_share_the_global_burst | default8/s,burst16; blocked sink released at fixed clock0;17lines written including summary |
| audit_formatter_handles_empty_output_without_panicking | empty out slice panics in writer.rs254 |
| audit_formatter_rejects_overflow_instead_of_silent_field_loss | populated record returns510bytes while silently omitting populated trailing fields |
| audit_concurrent_sequences_do_not_invert | deterministic two-producer barrier clock produces output event_seq[2,1] |

Shell copy/stubs: /tmp/task003-shell-audit-pivs52sq. Functions-only import, all gadget/log paths redirected to temporary files, no resync invocation. A readable empty-LUN content stub with nonzero apparent size and raw CONFIGURED state produces gadget_state=other,lun=present. Script explicitly prints2FAIL lines; orchestration shell exit0 is not a passing semantic check. This demonstrates the test distinction missing from existing ordinary-file fixtures. No host/tablet sysfs attributes were read.

## Required targeted corrections

### R1 — Real global rate budget, long uptime and delayed output

HIGH. observability.rs:314-354 packs last boot milliseconds into32bits but compares against fullu64 now. At2^32ms (~49.7days), each call treats truncation as a large elapsed interval and refills the bucket. Progress/idle gates fail similarly. Fix time representation/arithmetic without truncating the relevant uptime or comparing mixed widths; keep bounded/no playback IO behavior and unavailable-clock policy explicit. Add tests before/at/after the boundary and long-running same-timestamp emissions.

writer.rs:415-438 writes DropsSummary directly, bypassing global gate. Contract includes summaries in8/s+burst16. Current producer-only counters test is insufficient. Apply one effective global budget to all emitted lines, including summaries; also test a slow sink/queued backlog draining after release rather than only admissions. Do not turn output pacing into caller waits or an unbounded queue. Test unavailable clock without summary flooding. Default fixed-time40event scenario must output at most16total lines, with explicit loss accounting.

### R2 — Bounded formatting and bounded collector

MEDIUM. writer.rs:191-255,262-301 silently omits fields on overflow and always calls finish; zero-length out panics, and a larger caller slice is not explicitly clamped to512. Handoff's claim that overflow returns0/counts sink_errors is false. Implement an unambiguous whole-record overflow/error contract (return0 as previously reported is acceptable), no partial/malformed record, no silent populated-field loss. write_event must not call Sink with an invalid/zero-size record; increment a documented formatting-loss/error counter. Test empty,undersized,512,and oversized buffers plus maximum numeric fields and mandatory keys. Document exact field-loss policy rather than making schema parity tests accept omission.

MemorySink at writer.rs:114-147 is described as bounded but stores every line with Vec::push and has no capacity policy. Give the injected memory collector a documented event/byte cap and an explicit full outcome/counter (or return sink error); retain API compatibility through a bounded default if needed. Test sustained output/storage bound and collection failure. Queue size and worker stack alone do not bound this sink.

### R3 — Actual engine outcomes and ON/OFF parity

HIGH evidence gap. live_engine.rs:202-203 emits LiveReadProgress BEFORE decision/delivery. observation.rs:22-31 contains requested size/initial positions, no real_bytes/null_bytes/file_id/anchor_epoch. Cold no-data path live_engine.rs:153-159 returns NULL without any engine event. The trace cannot distinguish successful delivery from requested/read-attempt progress as required.

Capture actual post-delivery producer-copied bytes versus synthetic NULL filler, relevant mapping/epoch and bounded wait, including cold starvation and new noncommit paths. If a pre-read event is retained, label it as request/attempt instead of output result. Do not equate producer bytes with decodable video (ring bytes can themselves be NULL). Preserve TASK-002 counters/decisions, including its disclosed prefix-NULL telemetry limitation; observation may report actual synthetic filler accurately without changing those counters.

Required integration parity tests are missing: integration_observability.rs:251-289 is one cold smoke on all0x47bytes; the other3tests compare schema. Add actual enabled-versus-disabled byte/state comparison for real delivery, NULL-only starvation/cold path, confirmed low-offset overwritten recovery and invalid-reference noncommit, using bounded parser-valid fixtures. Assert expected events come from real hooks and correspond to outputs. Existing TASK-001/002 regressions are frozen; do not modify them to accommodate instrumentation. Avoid default real marker path.

### R4 — Preserve diagnostic failure evidence at transport/marker sites

MEDIUM. dispatcher.rs:588 reports only written length, omitting requested length,rin.offset/fh. report_write_error at690-697 discards errno,expected/actual length and request correlation. Unexpected device-read return at294-295 skips FuseLoopExit. Documented len/errno evidence is therefore absent when needed.

Capture existing values before any observer call can alter errno; report requested versus returned bytes, offset/handle/request unique at meaningful read/write-result sites, and unclean loop termination on error. Do not retry/change existing writes, FUSE response bytes, INIT or open/reset ordering. Add actual safe fake-FD/socketpair failure tests (e.g.invalid descriptor for device/read/write error, bounded nonblocking socket for short writes) demonstrating outcomes/events. Never mount /dev/fuse or change signal policy just for a test.

Engine resync helper observation.rs:63-74 reduces existing File::create result to bool and labels it launch/launch_failed. Creating an empty marker is not launching watchdog recovery. Use truthful marker-request creation/success/failure reasons and preserve numeric errno when available, without changing marker contents/path/consumption or adding new IPC. Runtime real-marker coverage remains NOT_RUN; pure event tests and source review must say so.

### R5 — Observe actual gadget attribute values

MEDIUM. tv_watchdog.sh:82-96 recognizes lowercase state only and uses -s lun0/file for presence. Android legacy state_show uses uppercase CONFIGURED/CONNECTED/DISCONNECTED in [Android kernel source](https://android.googlesource.com/kernel/common/+/59c75bcf603f355ff34282422e8c3f2165b9d5f2/drivers/usb/gadget/android.c). This is primary reference evidence, not proof of deployed tablet kernel revision. Current handling collapses distinct uppercase states to other, potentially suppressing subsequent changes.

Normalize explicitly supported uppercase/lowercase tokens to the allowlist and test all transitions. Read the LUN attribute content safely and classify readable-empty/readable-nonempty/read-error without outputting its content or inferring it from st_size. Sysfs attributes are show()/read-driven, as [kernel documentation](https://kernel.org/doc/html/v6.10/filesystems/sysfs.html) explains; regular-file -s fixtures do not model them. Add stubbed content/stat mismatch, empty/newline/unreadable cases and ensure raw backing paths never reach logs. Do not touch actual sysfs.

Keep original resync mode/consume/cooldown/breaker/launch ordering, which static review found preserved. The current harness's log-failure control becomes inactive under root yet counts success: use an unconditionally failing temporary log target (e.g.a directory) to prove neutrality on every UID. Functions-only empty PATH proves no external command, not no filesystem queries (-x probes still occur at startup); either move the guard/definitions before those probes or document/test the precise narrower import contract. No service startup/token read permitted.

### R6 — Honest sequence semantics, test evidence and resource reporting

MEDIUM correlation contract. event_seq is allocated before clock capture/enqueue (observability.rs:458-493). Concurrent producers can output2then1, as reproduced. Avoid adding blocking producer serialization or unbounded reorder storage just to satisfy a misleading claim. Either implement bounded documented output ordering, or define sequence as unique allocation ID with possible cross-thread output reordering and test the actual concurrency behavior. Update event contract/tests/handoff; per-process wall-output order must not be inferred from a sequential2event test. Summary0sentinel remains explicitly separate.

Record RSS/process footprint as NOT_RUN unless measured;18KiBqueue+256KiBstack is a structural estimate, not measured resident memory. MemorySink currently invalidates a generic bounded-memory claim. Do not report all instrumentation requirements covered by schema tests or all125library tests as new coverage. Keep raw failures/clock/order limitations visible.

## Scope adjustment to avoid another line-limit compression pass

Existing allowed files remain as TASK-003. For these corrections only, authorize optional cohesive extraction into these exact NEW paths if needed:

- crates/fuse_ntfs/src/observability/gate.rs
- crates/fuse_ntfs/src/observability/format.rs
- crates/fuse_ntfs/src/observability/sinks.rs
- crates/fuse_ntfs/src/observability/budget_tests.rs
- crates/fuse_ntfs/src/observability/format_tests.rs
- crates/fuse_ntfs/src/vfs/dispatcher/observation.rs
- crates/fuse_ntfs/tests/observability/fixtures.rs
- crates/fuse_ntfs/tests/observability/parity.rs

These are child modules of existing approved parents, no new Cargo target/dependency, no playback/transport-policy extraction. Keep fixed public imports stable where possible. Each NEW file<=500formatted lines; no duplicated fixtures or removed tests. Existing live_engine500/main500/watchdog500 ceilings and dispatcher baseline1012+80cap remain. If impossible, report a concrete BLOCKED scope issue rather than silently raising budgets or minifying. The original source/ring/tracker/TS/snapper/test freezes remain in force.

## Revalidation and stop point

Keep safe filtered Rust suite, fake watchdog harness and new-file formatting checks. Include the isolated before/after falsifiers above; each individual command exit matters. Do not execute real resync engine branch, unfiltered marker case, --ignored cases, actual daemon, server import, network/hardware/deployment or marker cleanup. No new behavior/policy work. Original C remains read-only.

Verify requested/replied/media fields against actual bytes/state rather than docs only. Record default-budget output including summaries, sink storage cap, full uptime boundaries and concurrent emission assumptions. Commit no changes automatically and do not start TASK-004. Return targeted correction patch and handoff for another independent audit. No production/USB/TV success is established.
