# Forensic audit — 2026-10-18

## Scope and evidence rules

CONFIRMED: audit only; no implementation, deploy, service restart, device access, gadget operation or reference-repository modification. Documentation is the only intended tracked addition. References below are working-tree line numbers, not claims about deployed binaries. Proposed contracts elsewhere are requirements, not observed behavior.

CONFIRMED: root `/home/sam/Code/usb-stream-tv-rust`, branch `main`, HEAD `8581259bed94df3062e2c7bdef2d4707442ca106`. Initial tree: 12 modified tracked files (NTFS contract, engine, main, VFS, seek integration test), three untracked files (`integration_return_and_channel_switch.rs`, `integration_strict_contract.rs`, `fuse_ntfs_consolidado.rs`). Preserved. Reference `/home/sam/Code/usb-stream-tv-prod`, HEAD `966ffad15744476b416a1de333d979a647e119bc`, pre-existing modified `.gitignore`; preserved. Comparison is against both actual working trees. Attribution to the historical migration versus subsequent uncommitted work is UNKNOWN.

CONFIRMED: read AGENTS.md, AGENT_HANDOFF.md, docs/ARCHITECTURE.md, HARDWARE_SPECS.md, LIVE_EDGE_LOCK_AND_PROBE_ISOLATION.md, audit_return_continuity.md and briefing_investigacao_return.md. Handoff's clean `master`/136-test description is historical and differs from discovery. Its hardware claims are documentation evidence, not a new device inventory. Historical Return capture analysis survives in briefing; referenced original `/tmp` log is explicitly described there as deleted. It cannot establish present runtime causality.

## Architecture and ownership

CONFIRMED (`Cargo.toml:1-19`): five workspace crates: fuse_ntfs, fuse_direct, stream_fetcher, patch_trp, sparse_unpack. Python server, torrent downloader, dashboard, shell operational control and template generation remain in the system. `fuse_ntfs_consolidado.rs` is outside workspace sources; provenance/generated status UNKNOWN. `engine/mod.rs:1-12` exports LiveEngine; stale live_engine module prose mentioning MediaEngine does not describe a second wired engine.

CONFIRMED data path:

```text
provider URL -> Python FFmpeg producer -> SeamlessRestamper -> subscriber HTTP /stream
 -> Rust stream_fetcher -> TsAligner -> FIFO -> fuse_ntfs feeder_loop
 -> Ring -> LiveEngine offset mapping/TS transform -> NtfsServer disk extent routing
 -> raw /dev/fuse -> kernel gadget backing-image fd -> SCSI -> TV NTFS/media player
```

CONFIRMED references: `server.py:1165-1279,1825-1902,2015-2038,2136-2157,4016-4034`; `crates/stream_fetcher/src/main.rs:403-527`, `ts_align.rs`; `crates/fuse_ntfs/src/main.rs:113-241,290-311,313-387`; `engine/live_engine.rs:128-445`; `vfs/dispatcher.rs:70-205`; `vfs/ntfs_mft.rs` supplies template extent routing. File-oriented catalog paths and whole-disk backing-image paths are distinct; in-process catalog tests do not establish that TV file selection traverses host FUSE LOOKUP/OPEN.

CONFIRMED control path: `dashboard.html:7839-7858` -> POST `/api/switch` (`server.py:4440-4445`) -> `StreamHub.switch_channel:2262-2433` -> asynchronous producer acquisition -> atomic proc identity replacement under hub lock -> restamper/broadcast. Existing HTTP connection can continue. Separate device-command polling in `scripts/tv_watchdog.sh:269-271` can invoke mode lifecycle; switching a source and switching gadget mode are different operations.

CONFIRMED process/lifecycle boundaries:

| Owner | Work / synchronization / blocking | Failure propagation |
|---|---|---|
| Python StreamHub | FFmpeg children, reader/keepalive threads, hub lock, switch lock, bounded subscriber queues | Producer exit/retry/slate; source acquisition can take >10s; API acknowledgement precedes commit |
| stream_fetcher | TCP connect/read/write timeouts, DNS resolver, FIFO sink, singleton flock, atomic running flag | Reconnects network; broken FIFO returns sink error and reopens; no gadget writes |
| fuse_ntfs feeder | FIFO poll 250ms, reads up to64KiB, flush marker, ring mutex/condvar | EOF/error reopens after100ms, not filesystem termination |
| LiveEngine | Shared anchor, xform, state-machine mutexes; ring mutex; 500ms edge wait,1200ms cold wait; unlocked pacing slices <=50ms | Full NULL reply on starvation; behind-probe recovery can create resync marker |
| FUSE dispatcher | Serial request loop, fixed aligned ABI buffers, direct-I/O backing image | Blocking live read delays later metadata requests; FUSE loop error exits/unmounts |
| switch_tv_mode / watchdog | Mode lock, flags, cooldown/breaker; mount/backing-file/gadget writes | Actual disconnect/remount permitted by existing scripts; not by network fetcher |
| fuse_direct VOD | Separate daemon/FAT32/range client/cache/prefetch worker | Shared mode teardown can kill it; separate correctness risks remain |

CONFIRMED: `main.rs:298-305` ignores feeder thread spawn result; advertised URL/file targets are passed to filesystem open rather than a URL client. Routine FIFO failure itself does not exit FUSE (`:220-239`). Main mounts /dev/fuse and tears it down after dispatcher returns (`:313-387`); despite its opening prose, gadget attachment is owned by scripts. Dispatcher OPEN notifies one shared backend before validating inode and grants constant fh=1 (`dispatcher.rs:480-497`); RELEASE acknowledges only (`:308-309`).

CONFIRMED recovery path: state_machine.rs:218-239 behind nonadjacent probes -> live_engine.rs:217-224 marker -> watchdog.sh:278-301 -> switch_tv_mode.sh:98-116 teardown and :193-258 recreation. Existing C also requests marker (`src/ntfs/fuse_ntfs.c:2371`). Source stall -> this path -> reported USB loss remains HYPOTHESIS without correlated events.

## Local baseline

CONFIRMED results on audited dirty tree, Rust/Cargo1.98.1. Safe offline host tests and local cross build only:

| Command / measurement | Status | Observed result |
|---|---|---|
| `cargo test --workspace --offline` | PASS |157 passed,2 ignored |
| `cargo test -p fuse_ntfs --test integration_strict_contract --offline -- --ignored` | FAIL |0 passed,2 failed;16.45s |
| `cargo fmt --all -- --check` | FAIL |exit1 |
| `cargo clippy --workspace --all-targets --offline -- -D warnings` | FAIL |exit101 |
| `cargo build --workspace --release --target armv7-unknown-linux-gnueabihf --offline` | PASS |static ARM EABI5 output; getaddrinfo/glibc runtime-library linker warnings |
| isolated Python subset below | PASS |162 tests,41 subtests;1.07s |
| `bash -n tests/test_vod_lock_and_elf.sh`; `bash tests/test_vod_lock_and_elf.sh` | PASS |reference-function harness, not full production-script behavior |
| line coverage >=80% | NOT_RUN |tool unavailable; percentage UNKNOWN |
| mutation >=60% | NOT_RUN |tool unavailable; score UNKNOWN |
| maximum cyclomatic complexity <=20 | NOT_RUN |tool unavailable; maximum UNKNOWN |
| source files >500 physical lines | FAIL against proposed target |13 tracked Rust/Python/shell;15 including two pre-existing oversized untracked Rust files |
| security/license dependency scan | NOT_RUN |no tool-backed vulnerability/license verdict |
| device / TV / ffprobe capture / soak | NOT_RUN |no runtime stability or bitstream compatibility conclusion |

CONFIRMED Python command:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_torrent_vod.py tests/test_torrent_vod_stress.py tests/test_fat32_integrity.py
```

CONFIRMED: full Python discovery deliberately NOT_RUN: `tests/test_server_control_plane.py:5` imports server; `server.py:2549` instantiates HUB and `:1893-1902` starts producer/threads. `test_probe_sar.py:26-35` cleanup follows import. It is not safe offline isolation. Stress assertion `test_torrent_vod_stress.py:205` uses `or True`; passing count overstates falsification strength. Shell harness copies reference functions. FAT tests model generated templates rather than executing Rust FUSE.

CONFIRMED ignored failures: strict_contract.rs:1143 expects live bytes after large bookmark but receives NULLs; :949 cursor actual9,437,184 versus expected8,456,024. These are known failures, not passing exceptions. Repeatedly running them is unnecessary until a change targets them.

CONFIRMED lint examples: patch_trp/main.rs:137,179 modulus lints; :345 map_or; dispatcher.rs:357,421,434,467 host-redundant ABI casts; ntfs_mft.rs:244 len_without_is_empty. Do not remove architecture-sensitive casts solely to silence host Clippy.

CONFIRMED: .cargo/config.toml:1-3 uses a host-absolute ARM GNU linker and crt-static. Installed targets include x86_64 GNU, aarch64 musl, ARMv7 GNU/musl/Android. File inspection reports static ARM EABI5/Linux3.2.0; Android3.4 execution compatibility and DNS remain UNKNOWN. Handoff identifies SM-T110/Android4.4.2/ARMv7; exact SoC and currently available RAM require device evidence.

## Evidence limitations / risk ranking

CONFIRMED: local `server_events.log` has4 lines/180 bytes and no SLATE_ENTER, SLATE_EXIT, SWITCH_OK, SOURCE_FAILURE, WATCHDOG_FFMPEG_KILL or GADGET_STATE_CHANGE events. No current correlated tablet/kernel/TV trace was established. No credentials were copied into these reports.

See [root causes](ROOT_CAUSE_ANALYSIS.md), [behavior comparison](C_VS_RUST_BEHAVIOR_MATRIX.md), [roadmap](../agent_tasks/IMPLEMENTATION_ROADMAP.md). P0 source-loss isolation precedes session/latency optimization. Existing slate must first be tested, not replaced on the assumption it is absent.
