# Stream state machine contract — proposed

CONFIRMED current `engine/state_machine.rs` concerns read/open/gap/seek decisions, not network-source/slate lifecycle. Current source/slate state lives in Python StreamHub booleans and threads (`server.py:1884-1902,2040-2260,2262-2433`). Do not replace the reader machine with a source machine.

| Source state | Event / guard | Next state / required action |
|---|---|---|
| LIVE_ACTIVE | no validated media within configured gap | SOURCE_STALLED; keep FS and gadget; bounded wait, account byte age |
| SOURCE_STALLED | local/provider-reachable valid slate ready | SLATE_ACTIVE; one atomic publication/splice, retain output timeline |
| SOURCE_STALLED | valid original generation returns before fallback | LIVE_ACTIVE; validate program and epoch, no late slate commit |
| SLATE_ACTIVE | candidate source appears | SOURCE_RECOVERING; continue slate, acquire bounded PAT/PMT+codec config+IDR |
| SOURCE_RECOVERING | candidate complete and current generation | LIVE_ACTIVE; atomic splice; discard old candidate output |
| SOURCE_RECOVERING | timeout/error/cancellation | SLATE_ACTIVE; explicit classified retry with bounded delay |
| any active state | channel request | new candidate generation; preserve current valid output until commit; supersede stale candidate |
| any active state | slate invalid/missing | explicit degraded state; paced packet-safe NULL strategy only as temporary FS protection; no claim of playable fallback |
| any active state | consumer seek/reopen | reader-state transition only; source ownership unchanged |
| any state | shutdown | cancel wakes waits, stop producer/fallback, join workers; no automatic source-triggered USB reset |

PROPOSED initial host targets, pending hardware calibration: classify source silence and publish valid fallback within2s at the producer boundary; retain <=500ms edge wait where currently used, account1200ms cold-start exception separately. These are design goals, not measured TV response. Actual current Python slate threshold is8s and keepalive suppresses output while switching (`server.py:73,2182`), so it does not meet these goals by construction.

Require monotonic state-event time, source/channel generation and transition reason. Correlation is propagated through acquisition->commit->ring->read, not inferred from adjacent wall-clock log lines. At least one media-bearing stream must remain valid through candidate acquisition. Retries must not silently mask permanently invalid configuration. Bound retry attempt frequency and cancel superseded work; choose numerical retry policy after measuring existing source connection costs.

Fault tests: source500/404, DNS failure, TCP reset, no data/trickle, partial TS packet, producer kill/restart, buffer wrap, failed/rapid switch, loss during inferred reopen, invalid slate, incompatible returned program, late old candidate, shutdown blocked at edge. Use simulated clock where possible; fixtures include a truly decodable TS in addition to parser-shaped packets. See HARDWARE_VALIDATION_PLAN.md for device evidence.
