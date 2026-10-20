# Channel-switch latency budget

CONFIRMED: current API accepts asynchronously (`server.py:2432-2433,4440-4445`); dashboard claims TV tuning on acknowledgement (`dashboard.html:7856-7858`). `SWITCH_OK` measures server commit, not visible output. Synthetic integration_return_and_channel_switch.rs:315-349 times local catalog reads only.

| Segment | Required measurement | Proposed allocation, seconds |
|---|---|---:|
| T0->T1 dashboard intent->API acceptance | client monotonic event + request correlation |0.3 |
| T1->T2 selection/candidate accepted | server monotonic request/generation |0.2 |
| T2->T3 source acquisition begins | process/network start |0.3 |
| T3->T4 decodable program ready | PAT/PMT/codec config/IDR and first source timecode |2.0 |
| T4->T5 committed ring generation | restamper/HTTP/FIFO/ring publish |0.7 |
| T5->T6 new-generation read delivery | FUSE byte/generation capture |1.0 |
| T6->T7 rendered program | synchronized camera/timecode |3.5 |
| Contingency | measured attribution |1.5 |

PROPOSED total9.5s leaves0.5s below strict10s; allocations are experiment targets, not measured capability. Report minimum,median,p95,maximum and failures/timeouts separately. Require every successful switch within10s under a defined source/network envelope; failing sources must show fallback within its own bound, not be counted as successful channel render.

CONFIRMED barriers: switch serialization up to4s (:2263), ECO wait8s (:2351), transcode wait20s (:2381), plus probing; keepalive suppressed during switching (:2182). Python prebuffer60MiB (:1882), queue2500 chunks (:1986), existing queues not cleared by commit (:2408-2409); Rust16MiB leadback and6MiB startup (contract.rs:18,ring.rs:67). At600KiB/s these byte amounts represent27.3s and10.24s; these are arithmetic examples, not observed latency. Source-byte rate, null inflation and player buffering must be measured separately.

Proposed experiment: tag two playable channels with distinct video timecodes and generation metadata; measure server queue occupancy/age, TCP/FIFO backlog, ring sequence and oldest displayed time. Test live/live, failing candidate, slate/live, live/slate, rapidA-B-C, delayedB packets afterC, network recovery concurrent switch and Return concurrent switch. Avoid conflating catalog file_id change with HTTP source replacement. Existing process identity rejects some old data but queued packets carry no end-to-end epoch proof.

Do not reduce leadback or flush the ring on speculation: quantify acquisition/read/decoder budget, then implement stale-generation rejection and safe splice/backlog policy. T7 hardware access is BLOCKED pending explicit later authorization and actual TV participation.
