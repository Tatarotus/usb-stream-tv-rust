# TS continuity contract

Evidence: `crates/stream_fetcher/src/ts_align.rs`, `engine/ts_xform.rs`, `engine/live_engine.rs:268-435`; C `src/ntfs/ts_xform.h`, `fuse_ntfs.c:1018,1118-1169,1434-1462,2500-2555`; Python `SeamlessRestamper:1165-1279`; `generate_slate.sh:51-66`. Requirements below are proposed acceptance contracts; actual decoder continuity remains UNKNOWN.

## Packet and program invariants

Transport packets188 bytes. Preserve phase across64KiB and128KiB reads, FIFO reconnect/drain, ring wrap, arbitrary virtual read offset and splice. A partial real packet cannot be completed with unrelated NULL payload then called a valid packet. Preserve full packet tails or choose a packet boundary before substituting. `0x47` at each188th byte alone does not establish valid PSI/PES/codec.

PAT/PMT sections must be internally valid including lengths/CRC/version/program mappings. Video PID0x100, audio0x101 and PMT0x1000 are current generated-slate intentions; verify actual live source/remux mappings. Unexpected PIDs/program changes must be rejected or intentionally remapped with consistent PSI, not silently rewritten using stale assumed PIDs.

Target media profile: H.264 main<=4.1,yuv420p,30fps,GOP30,zero B frames,repeat SPS/PPS,one slice per frame; AC-3 stereo384k/48kHz; PCR emission20ms. Supplied hardware rules constrain changes; actual bitstream must be checked. Slate generator lacks explicit `-pcr_period 20` and `slices=1`; absence of flags is not proof of incorrect emitted values. Loader assumes10s duration and only packet-multiple chunks (`server.py:1948-1959`). Validate measured duration and PCR cadence rather than trusting that assumption.

## Epoch transformation

For each committed splice choose old delivered reference and first playable new program boundary. PTS/DTS offsets use modulo2^33; PCR modulo2^33*300. One common timeline shift must retain source A/V relationships and PCR versus DTS/PTS lead. Existing implementation targets referencePTS+3000 ticks; test whether source clock/frame rate makes that safe. Do not independently force audio and video to identical PTS.

Payload-bearing CC increments modulo16 per PID; adaptation-only packets do not consume payload continuity. Preserve retransmission semantics; NULL PID is exempt from media timeline rewriting. Discontinuity signaling is an explicit fallback policy, not proof Samsung resets its decoder. Validate all emitted headers and fields at split packet/PES boundaries, not just complete packets within each individual read.

Determinism: same epoch+source position produces same transformed bytes. Probes/rereads cannot advance successful served reference. Carry/tail ownership is generation-specific. At a warm reanchor finish old packet under old transform, then start new aligned epoch; compare Rust downward `snap188(foff)` to C old-tail/pad algorithm before assuming equivalence.

## Independent tests

Parser-shaped existing fixtures are unit evidence only (strict_contract.rs:93-213 does not create a playable program). Use generated/captured decodable live/slate with PAT/PMT/IDR and source-time overlay; store bounded sanitized fixtures outside root. Concatenate actual replies across all188 phases, request sizes1/187/188/189/16384/65536/131072, partial feeder writes, splice, wrap and timestamp rollover. Check exact byte grid, PSI CRC, CC per PID, modulo PCR/PTS/DTS deltas, A/V skew, decode errors and actual frame output. `ffprobe` checks codec/B-frame/audio properties but PCR interval requires packet-level parsing. Compare C and Rust on the same fixture; C differences are evidence, not automatic oracle correctness.
