# Target architecture — proposed, not implemented

Basis: [forensics](FORENSIC_AUDIT.md), [matrix](C_VS_RUST_BEHAVIOR_MATRIX.md). Preserve Python producer/restamper, Rust fetcher, FIFO, ring and filesystem split. No new async runtime, broad rewrite, gadget reflash or replacement VOD pipeline is justified.

## Smallest safe changes

1. Establish deterministic regression evidence and bounded correlated events before modifying recovery policy. Separate successful-media cursor from attempted-read/diagnostic cursor. NULLs must not forge successful progress or fresh timestamp references.
2. Restore packet-phase acquisition after FIFO drain/reopen as a bounded feeder responsibility. A flush is a generation transaction: old reads cannot commit into a newly cleared ring/anchor/xform. Demonstrate races with barriers, then choose the smallest locking/version check. Network work remains outside engine locks.
3. Keep FUSE alive through producer EOF. Routine upstream failure must not request USB recycle. Resync marker behavior becomes an explicit operator policy, with reason, cooldown and audit evidence, rather than an implicit media recovery mechanism. Preserve backing file/volume geometry during source loss. Changes require dedicated regression evidence; existing mode scripts remain sole gadget authority.
4. Repair/test existing Python slate for provider-only failure and candidate acquisition. It already owns continuous restamping. Prevent stale slate publication after live commit and validate decodable program readiness, not first arbitrary bytes.
5. Cover total tablet-link failure separately. Server slate cannot satisfy this case. Investigate a bounded, prevalidated local TS asset and feeder-side arbitration into the same ring/continuity pipeline. Do not create a second unsynchronized writer or concatenate raw timestamp epochs. Implementation is BLOCKED pending asset profile, memory/CPU and splice evidence.
6. Preserve host backing-handle identity separately from inferred TV playback session. Only demonstrated reopen evidence may invoke live-edge reanchor; cold reset must not throw away continuity needed by TV cached bytes. Multiple readers need an explicit policy before per-reader engines are introduced.
7. Use publication generation/candidate identity at asynchronous boundaries, validate PAT/PMT/SPS/PPS/IDR, and bound backlog age. Preserve downstream virtual-byte mapping and splice clock continuity when skipping history. Do not blindly clear all queues/ring on channel request.

## Ownership/contracts

| Plane | Owner | Contract |
|---|---|---|
| Source acquisition | Python FFmpeg supervisor / Rust HTTP fetcher | bounded connect/read waits, explicit reasoned recovery, no gadget control |
| Media publication | Python restamper and feeder arbitration | one committed generation at a time; aligned packets; no late source publication |
| Buffer/read | Rust engine | bounded128MiB ring, no feeder throttle; successful progress only on real bytes; cooperative waits and explicit deadline/cancellation |
| Filesystem | raw FUSE / NTFS template | stable virtual disk geometry, validated ABI and offsets, full intended live replies; ordinary VOD EOF/error semantics handled separately |
| Device | switch_tv_mode/reconnect watchdog policy | only explicit mode/operator authority reconfigures gadget; routine media outage does not |

CONFIRMED current memory parameters:128MiB nominal live ring (rounded down to188 grid),6MiB start threshold,16MiB leadback; VOD approximately40MiB base caches plus temporaries. PROPOSED budgets: no additional unbounded queue, no per-read allocation proportional to virtual file size, no extra128MiB ring per TV click. Inventory actual free RAM and existing Python queue bytes before approving local slate. Maintain <=50ms individual pacing sleep with locks released; total FUSE service time, not slice size alone, must fit a measured safe limit. Existing3500ms default is a deadline value, not measured safety proof.

Final acceptance is hardware-gated. Pure host approval permits a bounded hardware experiment, never production-readiness claims.
