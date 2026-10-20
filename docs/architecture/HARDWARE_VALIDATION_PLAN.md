# Validation plan and evidence gates

No hardware action was performed or authorized by this audit. Source/docs-only review can approve a host patch or approve a bounded hardware experiment, not certify production readiness. Baseline provenance is in FORENSIC_AUDIT.md. Original C remains read-only throughout.

## Layer A/B: deterministic host falsification

Use bounded playable fixtures plus parser-level malformed input. Fault matrix: HTTP404/500, DNS failure, TCP reset, silent socket/trickle, partial packet at each phase, producer exit/restart, ring exhaustion/wrap, repeated failing channels, switch during loss, loss during Return. Assert FUSE-sized replies and packet correctness independently, no source-triggered gadget operation, generation-safe commit, finite memory/waits/cancellation. A mocked gadget sink proves only no attempted operation in scope.

Schedule barriers for flush between residency-check/copy, reanchor and generation commit; reader shutdown at condvar wait; simultaneous slate/live publication; late old producer. Test two readers at conflicting offsets and document supported isolation policy. Run truly decodable TS splice checks and differential C/Rust fixtures, not only headers resembling TS. Test VOD Range200/206/416/truncation, seeks and total request deadlines independently; live fixes must leave standalone direct VOD tests passing.

TASK-001 is a deliberate red regression artifact: prove NULL-only successful-chain corruption without fixing it. Expected failing assertion is evidence, not passing product behavior. Subsequent fix requires before-fail/after-pass plus adversarial variations; restore the bad update in a temporary isolated copy and verify new tests fail. Never modify the original C tree for mutation/differential builds; build test outputs under scratch or /tmp.

## Layer C: process tests before device

Future harness must use temporary FIFO, configurable paths/local server and mock sysfs root. Never launch real default daemons against /dev/fuse or /data paths on host. Capture binary exit codes, signals, FIFO ownership/restart, descriptors, threads/RSS and structured events. Network producer kill must not kill filesystem process; shutdown must wake waiting consumers. Record exact executable hashes and all skipped privileged paths. Python imports must be isolated first; existing full suite starts threads/FFmpeg on import.

## Layer D: bounded authorized hardware experiment

Preflight later, with authorization: inventory model/kernel/RAM, deployed binary+script/image/slate hashes, active config, process tree and USB backing path. Use master deploy script only for an explicitly authorized deployment after ELF/static/runtime checks. No reset/re-enumeration workaround by default. Record fallback/recovery state, marker creation and watchdog decision, exit/signal, mount state, gadget enable/LUN, kernel USB events and TV screen with common correlation timeline. Redact source credentials.

Classify separately: upstream error, process crash, FUSE reply error/EOF, backing-file/mount loss, explicit gadget reset, physical disconnect, TV stopping SCSI reads, decoder freeze. Combine logs with camera/program timecode. NULL replies alone do not prove playable slate or USB stability.

Return experiment: play60s,Return,wait40s,reopen; repeat10/20/66/180s idle, ring resident/overwritten, bookmark<8MiB/>=8MiB, healthy/slate/switching. Capture actual backing OPEN/RELEASE and requested offsets; do not fabricate TV opens from UI clicks. Map bytes to source capture timestamp; report stale duration and justified live-edge latency. Cached prefix versus resumed host fd requires explicit classification. Metadata/identity trials need separate bounded approval; no implicit gadget recycle.

Switch experiment: at least30 transitions per supported scenario (live/live, live/failing, slate/live, live/slate, rapid switches, switch+Return, switch+recovery, slow source, late old packets). Collect T0..T7, min/median/p95/max and failures. Timecode/camera synchronization uncertainty must be reported. All successful switches must be<10s in declared network/source envelope; API acceptance is T1 only.

Slate experiment: verify provider outage with server reachable, then complete tablet network loss separately. Validate actual assets with ffprobe (0 B frames, AC-3 properties, profile/framerate/GOP), TS parser (PCR20ms, PSI/CC/timestamp transitions), ffmpeg decode and TV observation through repeated loops/recovery. Record fallback onset/display latency and total outage duration. Offline slate requirement fails if only server-generated fallback works.

## Layer E: soak / acceptance

PROPOSED staged soak:2h controlled faults then24h playback with scheduled network loss/channel churn and Return trials. Sample RSS/FD/thread count/CPU/ring age/occupancy/recovery/USB reset count every10s; logs bounded and rotated. Require zero unsolicited gadget resets/crashes, no monotonic resource growth across repeated cycles, bounded memory/queues, successful source recovery and no persistent freeze. Determine numerical CPU/RSS ceilings after tablet inventory; do not invent free-RAM evidence. Healthy stable window should show near_starve=0/nulls=0; injected faults may legitimately increase them and must be attributed, not erased. Prefix-NULL telemetry currently undercounts (strict_contract.rs:745-747).

Audit output for each delivered implementation: exactly APPROVED, CHANGES_REQUESTED, BLOCKED_BY_MISSING_EVIDENCE or APPROVED_FOR_HARDWARE_TESTING; provide scope and targeted correction requests. Missing T7/slate/Return hardware evidence prevents production approval even when host checks pass. Current overall state: BLOCKED_BY_MISSING_EVIDENCE for incident causality and hardware claims, with confirmed host defects actionable through the roadmap.
