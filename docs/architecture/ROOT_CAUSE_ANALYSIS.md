# Root causes and competing hypotheses

## Established defects versus incident attribution

CONFIRMED: `LiveEngine::serve_live` updates `last_fend`, `last_read_ms` and calls `note_ckpt` even with `to_copy==0` (live_engine.rs:420-434). `note_ckpt` can store an old valid served state with a new offset/time (ts_xform.rs:682-692). Original C gates successful progress on real delivery (fuse_ntfs.c:1438-1462). The successful cursor/checkpoint update violates the successful-media invariant independently of the TV incident. Request-observation time is different: original C updates g_last_file_read_ms before delivery (fuse_ntfs.c:1211), so freezing Rust last_read_ms is not established C parity and could misclassify active starvation as Return idle. Existing test strict_contract.rs:689-755 relies on this violation for later adjacency; a one-line guard is not sufficient evidence of safe recovery.

CONFIRMED: Return resume intentionally chooses resident history (state_machine.rs:340-352). This explains a mechanism for host replay; it does not rule out TV cached bytes or server prebuffer. Large-bookmark tests explicitly fail (FORENSIC_AUDIT baseline). No speculative USB reflash is justified.

STRONGLY SUPPORTED: flush may change TS origin to a midpacket byte because main.rs drains arbitrary bytes and omits C's sync reacquisition. Aligning fetcher writes does not align every FIFO read/drain boundary. Reproduction over all188 residues must precede repair.

CONFIRMED: automatic gadget reset is reachable from the reader marker through watchdog (matrix). Four behind reads, not network failure alone, trigger it. Whether the user's incident was an actual disconnect remains UNKNOWN.

## P0 failure tree

| Candidate | Evidence strength | Missing discriminator |
|---|---|---|
| Reader asks watchdog to recycle gadget | CONFIRMED executable path; incident HYPOTHESIS | marker reason/time, watchdog execution, enable/LUN writes, kernel event |
| Starvation advances mapping without real media; recovery cannot catch reader | CONFIRMED progress defect; lasting decoder failure HYPOTHESIS | bounded producer-stop/restart trace and concatenated bytes |
| Flush corrupts stream phase | STRONGLY SUPPORTED lost guard | all-phase FIFO fixture and actual flush timing |
| Slate suppressed during concurrent switch | CONFIRMED suppression server.py:2182; impact HYPOTHESIS | stalled current source plus slow/failed switch fixture |
| Missing/incompatible deployed slate | UNKNOWN; loader may continue without asset | deployed image/asset hashes, effective config, packet profile, decode test |
| Total tablet/server network failure bypasses server slate | CONFIRMED architecture limitation | fault taxonomy and resident buffer duration; device-local slate proof |
| FUSE crash/panic or kernel transport error | UNKNOWN; abort profile and unsafe ABI exist, not a crash record | exit/signal/backtrace, FUSE errors and kernel logs |
| TV abandons playback despite USB staying enumerated | UNKNOWN | simultaneous USB state + SCSI reads + camera/timecode |

## Return hypotheses evaluated independently

| Hypothesis | Current support | Falsification / required evidence |
|---|---|---|
| H1 TV playback RAM replay | STRONGLY SUPPORTED historically by docs/briefing_investigacao_return.md §7-8, raw log unavailable | tagged displayed frames versus first requested offset after Return |
| H2 firmware resume metadata | HYPOTHESIS | compare bookmark/offset behavior across same identity and controlled identity experiment |
| H3 ring history replay | CONFIRMED possible/current explicit policy | map requested foff to generation+stream sequence/frame timestamp |
| H4 stale virtual mapping | STRONGLY SUPPORTED by no-data progress and failed large-bookmark tests | deterministic mappings before/after idle, wrap and failure |
| H5 same reader session | CONFIRMED host model shares backing fd; actual TV session UNKNOWN | kernel OPEN/RELEASE plus UI and SCSI read inactivity |
| H6 incorrect reanchor | CONFIRMED blocked large-bookmark cases; seam risk unresolved | strict host tests and arbitrary-phase byte capture |
| H7 unchanged identity/cache | HYPOTHESIS; anti-cache mechanisms exist | controlled metadata-only trials; re-enumeration only separately authorized |
| H8 TS clock/decoder behavior | STRONGLY SUPPORTED historical freeze analysis, current cause UNKNOWN | extracted PCR/PTS/DTS/CC and decode output across actual seam |

## Concurrency and scope risks

HYPOTHESIS: separately locked flush resets (live_engine.rs:69-86), snapshot/reanchor commit (:146-337), residency check/copy (:347-386) can combine generations. Deterministic barriers must demonstrate exact interleaving before adding broad synchronization. `wait_edge` has no cancellation/generation predicate (ring.rs:409-432).

CONFIRMED: static ARM build does not prove Android DNS behavior (getaddrinfo warning). VOD risks include unchecked Range response origins and cumulative read deadlines; see forensic/quality docs. They are independent of P0 cause attribution.

Priority: reproduce no-data chain defect and establish failure-event correlation; test flush/epoch publication; isolate USB recovery authority; validate retained slate; then address live-edge session policy and measured channel backlog. No single root cause for all observed symptoms is established.
