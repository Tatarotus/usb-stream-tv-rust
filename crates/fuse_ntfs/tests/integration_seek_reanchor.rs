use fuse_ntfs::contract::{PROBE_ISOLATION_FOFF, TS_PKT_SZ};
use fuse_ntfs::engine::live_engine::LiveEngine;
use fuse_ntfs::engine::ring;
use fuse_ntfs::engine::state_machine::PROBE_FUTURE_MARGIN;
use fuse_ntfs::StreamBackend;

const BLOCK: usize = 65_536;

fn make_stream(n_packets: usize) -> Vec<u8> {
    let mut v = Vec::with_capacity(n_packets * TS_PKT_SZ);
    let mut cc_video = 0u8;
    let mut cc_audio = 0u8;
    for i in 0..n_packets {
        if i % 10 == 0 {
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = 0x40;
            p[2] = 0x00;
            p[3] = 0x10;
            p[4] = 0x00;
            p[5] = 0x00;
            v.extend_from_slice(&p);
        } else if i % 3 == 0 {
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = 0x41;
            p[2] = 0x00;
            p[3] = 0x10 | (cc_video & 0x0F);
            p[4] = 0x00;
            p[5] = 0x00;
            p[6] = 0x01;
            p[7] = 0xE0;
            p[8] = 0x00;
            p[9] = 0x00;
            p[10] = 0x80;
            p[11] = 0x80;
            p[12] = 0x0A;
            let pts = 900_000u64 + (i as u64) * 3000;
            let mut b = [0u8; 5];
            fuse_ntfs::engine::ts_xform::pts_to_bytes(pts, &mut b);
            p[13..18].copy_from_slice(&b);
            cc_video = cc_video.wrapping_add(1) & 0x0F;
            v.extend_from_slice(&p);
        } else if i % 7 == 0 {
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = 0x41;
            p[2] = 0x01;
            p[3] = 0x10 | (cc_audio & 0x0F);
            p[4] = 0x00;
            p[5] = 0x00;
            p[6] = 0x01;
            p[7] = 0xBD;
            p[8] = 0x00;
            p[9] = 0x00;
            p[10] = 0x80;
            p[11] = 0x80;
            p[12] = 0x0A;
            let pts = 900_000u64 + (i as u64) * 3000;
            let mut b = [0u8; 5];
            fuse_ntfs::engine::ts_xform::pts_to_bytes(pts, &mut b);
            p[13..18].copy_from_slice(&b);
            cc_audio = cc_audio.wrapping_add(1) & 0x0F;
            v.extend_from_slice(&p);
        } else {
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = 0x1F;
            p[2] = 0xFF;
            p[3] = 0x10;
            v.extend_from_slice(&p);
        }
    }
    v
}

fn is_null_block(buf: &[u8], abs_off: u64) -> bool {
    let mut expected = vec![0u8; buf.len()];
    ring::fill_null_at(&mut expected, abs_off);
    buf == expected.as_slice()
}

#[test]
fn seek_reanchor_flow() {
    let engine = LiveEngine::new();
    let data = make_stream((20 * 1024 * 1024) / TS_PKT_SZ);
    engine.ring.write_chunk(&data);

    // Phase 1: sequential playback up to foff = 901120.
    let mut buf = vec![0u8; BLOCK];
    // open at 0
    let n = engine.serve_live(0, 0, &mut buf, 0);
    assert_eq!(n, BLOCK);
    assert!(!is_null_block(&buf, 0));

    let mut foff = BLOCK as u64;
    while foff < 901_120 {
        let want = if foff + BLOCK as u64 > 901_120 {
            (901_120 - foff) as usize
        } else {
            BLOCK
        };
        if want == 0 {
            break;
        }
        if buf.len() != want {
            buf.resize(want, 0);
        }
        let n = engine.serve_live(0, foff, &mut buf, 0);
        assert_eq!(n, want);
        assert!(!is_null_block(&buf, foff), "sequential read must serve real data");
        foff += want as u64;
    }

    let anchor_foff_before = engine.anchor.lock().unwrap_or_else(|e| e.into_inner()).base_foff;
    let anchor_spos_before = engine.anchor.lock().unwrap_or_else(|e| e.into_inner()).base_spos;
    let last_fend_before = engine.anchor.lock().unwrap_or_else(|e| e.into_inner()).last_fend;
    let epoch_before = engine.anchor.lock().unwrap_or_else(|e| e.into_inner()).epoch_seq;

    // Phase 2: isolated bookmark probe at 800 MB.
    let probe_size = 65_536usize;
    let probe_off = 799_997_952u64;
    let mut pbuf = vec![0u8; probe_size];
    engine.serve_live(0, probe_off, &mut pbuf, 0);
    assert!(
        is_null_block(&pbuf, probe_off),
        "isolated probe must be NULL TS packets"
    );

    {
        let a = engine.anchor.lock().unwrap_or_else(|e| e.into_inner());
        assert_eq!(a.last_fend, last_fend_before, "chain must not advance");
        assert_eq!(a.base_foff, anchor_foff_before, "anchor foff must not move");
        assert_eq!(a.base_spos, anchor_spos_before, "anchor spos must not move");
        assert_eq!(a.epoch_seq, epoch_before, "epoch must not change");
    }

    // Phase 3: consecutive probe sequence in the future must NEVER re-anchor (bug 3 fix).
    let mut pbuf2 = vec![0u8; 4096];
    engine.serve_live(0, probe_off + 4096, &mut pbuf2, 0);
    assert!(is_null_block(&pbuf2, probe_off + 4096));

    let mut pbuf3 = vec![0u8; 8192];
    engine.serve_live(0, probe_off + 4096 + 8192, &mut pbuf3, 0);
    assert!(is_null_block(&pbuf3, probe_off + 4096 + 8192));

    {
        let a = engine.anchor.lock().unwrap_or_else(|e| e.into_inner());
        assert_eq!(
            a.epoch_seq, epoch_before,
            "future probes must never trigger ConfirmedSeek or re-anchor"
        );
    }

    // Phase 4: restart at 0 after playing (restart_at_zero) triggers live edge re-anchor.
    let mut restart_buf = vec![0u8; BLOCK];
    let n = engine.serve_live(0, 0, &mut restart_buf, 0);
    assert_eq!(n, BLOCK);
    {
        let a = engine.anchor.lock().unwrap_or_else(|e| e.into_inner());
        assert!(
            a.epoch_seq > epoch_before,
            "restarting at foff=0 must trigger re-anchor to live edge"
        );
    }
    assert!(
        !is_null_block(&restart_buf, 0),
        "playback must resume with fresh D12-snapped packets at foff=0"
    );
}

// ---------------------------------------------------------------------------
// TASK-001 — starvation must not fabricate successful chain progress.
//
// `serve_live` must advance the *successful-media* cursor (`anchor.last_fend`)
// only when real producer bytes were copied (`to_copy > 0`); a NULL-only reply
// must not manufacture adjacency or a fresh checkpoint from earlier media.
// Setup: ~8.0 MiB stream (> `MIN_STREAM_START`, `ring_old == 0`), one real
// 64 KiB delivery, reader parked on the writer edge, no producer thread.
// ---------------------------------------------------------------------------

/// 44_620 * 188 = 8_388_560 B (~8.0 MiB), above `MIN_STREAM_START`.
const STREAM_PACKETS: usize = 44_620;
/// Request lengths: FUSE's 16 KiB page and a packet multiple (12032 = 188*64).
const TS_ALIGNED_LEN: usize = 16_384;
const PACKET_MULTIPLE_LEN: usize = 188 * 64;
/// A packet-partial tail (188*k + 94) makes `s_write` land off the 188 grid.
const ODD_PHASE_TAIL: usize = 94;

#[derive(Clone, Copy)]
struct AnchorSnap {
    active: bool,
    file_id: u32,
    base_foff: u64,
    base_spos: u64,
    last_fend: u64,
    epoch_seq: u64,
    s_write: u64,
}

impl AnchorSnap {
    /// The engine's own `foff -> spos` mapping (live_engine.rs:160-168).
    fn mapped(&self, foff: u64) -> u64 {
        if foff >= self.base_foff {
            self.base_spos + (foff - self.base_foff)
        } else {
            self.base_spos.saturating_sub(self.base_foff - foff)
        }
    }
}

fn snapshot(engine: &LiveEngine) -> AnchorSnap {
    let s_write = engine.ring.lock().s_write();
    let a = engine.anchor.lock().unwrap_or_else(|e| e.into_inner());
    AnchorSnap {
        active: a.active,
        file_id: a.file_id,
        base_foff: a.base_foff,
        base_spos: a.base_spos,
        last_fend: a.last_fend,
        epoch_seq: a.epoch_seq,
        s_write,
    }
}

/// Installs an active anchor with an exact identity mapping: `base_foff == 0`
/// and `base_spos == 0`, hence `mapped(foff) == foff` for every request.
fn engine_with_identity_anchor(stream: &[u8]) -> LiveEngine {
    let engine = LiveEngine::new();
    engine.ring.write_chunk(stream);
    let now = engine.ring.lock().now_ms();
    {
        let mut a = engine.anchor.lock().unwrap_or_else(|e| e.into_inner());
        a.active = true;
        a.file_id = 0;
        a.base_foff = 0;
        a.base_spos = 0;
        a.last_fend = 0;
        a.epoch_seq = 7;
        a.last_read_ms = now;
    }
    engine
}

/// True when `buf` carries a video (0x100) or audio (0x101) PES packet, so a
/// PAT-only fixture cannot pass as real media.
fn has_media_packet(buf: &[u8]) -> bool {
    buf.chunks_exact(TS_PKT_SZ).any(|p| {
        let pid = (((p[1] & 0x1F) as u16) << 8) | p[2] as u16;
        p[0] == 0x47
            && matches!(pid, 0x100 | 0x101)
            && p[4..7] == [0x00, 0x00, 0x01]
            && matches!(p[7], 0xE0 | 0xBD)
    })
}

/// Serves one adjacent 64 KiB block of real producer media and asserts it is
/// genuinely non-null. This populates the xform `served` reference that the
/// fabricated checkpoint would otherwise reuse.
fn serve_real_media(engine: &LiveEngine) {
    let before = snapshot(engine);
    let mut buf = vec![0u8; BLOCK];
    let n = engine.serve_live(0, 0, &mut buf, 0);
    assert_eq!(n, BLOCK, "real delivery must fill the request");
    assert!(
        has_media_packet(&buf),
        "fixture must deliver a real media PID"
    );
    assert!(!is_null_block(&buf, before.mapped(0)));
}

/// Parks the reader on the writer edge (`last_fend == s_write`) and proves the
/// mapping, so the next request is adjacent and lands on the starvation edge.
fn park_at_writer_edge(engine: &LiveEngine) -> AnchorSnap {
    let s_write = engine.ring.lock().s_write();
    {
        let mut a = engine.anchor.lock().unwrap_or_else(|e| e.into_inner());
        a.last_fend = s_write;
    }
    let snap = snapshot(engine);
    assert_eq!(snap.mapped(snap.last_fend), s_write, "edge == s_write");
    snap
}

/// Drives one edge-starvation request and returns `(before, after)`.
/// Every invariant except the successful cursor is asserted here, so the
/// caller's final cursor equality is the single, precise baseline failure.
fn observe_starvation(
    stream: &[u8],
    req_len: usize,
    expect_phase: Option<u64>,
) -> (AnchorSnap, AnchorSnap) {
    let engine = engine_with_identity_anchor(stream);
    serve_real_media(&engine);

    let before = park_at_writer_edge(&engine);
    if let Some(phase) = expect_phase {
        assert_eq!(
            before.s_write % TS_PKT_SZ as u64,
            phase,
            "fixture must place the writer edge on the intended TS phase"
        );
    }

    let mut dst = vec![0x5Au8; req_len];
    let n = engine.serve_live(0, before.last_fend, &mut dst, 0);

    assert_eq!(n, dst.len(), "starvation reply must fill the request");
    let mapped = before.mapped(before.last_fend);
    assert_eq!(
        mapped, before.s_write,
        "request must start on the writer edge"
    );
    assert!(
        is_null_block(&dst, mapped),
        "starvation reply must be full phase-aware NULL packets"
    );

    let after = snapshot(&engine);
    assert!(after.active && after.file_id == before.file_id);
    assert_eq!(
        after.base_foff, before.base_foff,
        "anchor foff must not move"
    );
    assert_eq!(
        after.base_spos, before.base_spos,
        "anchor spos must not move"
    );
    assert_eq!(after.epoch_seq, before.epoch_seq, "epoch must not change");
    (before, after)
}

/// A NULL-only reply to an established, adjacent reader must not advance the
/// successful-media cursor. EXPECTED RED before the TASK-001 repair.
#[test]
fn starved_reader_ts_aligned_must_not_advance_successful_cursor() {
    let stream = make_stream(STREAM_PACKETS);
    let (before, after) = observe_starvation(&stream, TS_ALIGNED_LEN, Some(0));
    assert_eq!(
        after.last_fend, before.last_fend,
        "NULL-only starvation reply must not advance the successful-media last_fend"
    );
}

/// Same contract for a packet-multiple (188*64) request length.
#[test]
fn starved_reader_packet_multiple_must_not_advance_successful_cursor() {
    let stream = make_stream(STREAM_PACKETS);
    let (before, after) = observe_starvation(&stream, PACKET_MULTIPLE_LEN, Some(0));
    assert_eq!(
        after.last_fend, before.last_fend,
        "NULL-only starvation reply must not advance the successful-media last_fend"
    );
}

/// Same contract when the request offset is off the 188 grid (phase 94).
#[test]
fn starved_reader_odd_phase_must_not_advance_successful_cursor() {
    let mut stream = make_stream(STREAM_PACKETS);
    stream.extend(std::iter::repeat(0x47u8).take(ODD_PHASE_TAIL));
    let (before, after) = observe_starvation(&stream, TS_ALIGNED_LEN, Some(ODD_PHASE_TAIL as u64));
    assert_eq!(
        after.last_fend, before.last_fend,
        "NULL-only starvation reply must not advance the successful-media last_fend"
    );
}

/// Control: a non-empty real delivery DOES advance the successful cursor and
/// must not move the anchor or epoch.
#[test]
fn real_media_delivery_advances_successful_cursor() {
    let stream = make_stream(STREAM_PACKETS);
    let engine = engine_with_identity_anchor(&stream);
    let before = snapshot(&engine);
    assert_eq!(before.last_fend, 0, "reader starts with nothing served");

    serve_real_media(&engine);

    let after = snapshot(&engine);
    assert_eq!(
        after.last_fend, BLOCK as u64,
        "real delivery must advance the successful-media last_fend"
    );
    assert_eq!(after.base_foff, before.base_foff);
    assert_eq!(after.base_spos, before.base_spos);
    assert_eq!(
        after.epoch_seq, before.epoch_seq,
        "sequential read must not re-anchor"
    );
}

/// Control: an isolated future probe returns full phase-aware NULLs, keeps the
/// successful cursor, and never re-anchors or escalates to resync.
#[test]
fn future_probe_preserves_successful_cursor() {
    let stream = make_stream(STREAM_PACKETS);
    let engine = engine_with_identity_anchor(&stream);
    serve_real_media(&engine);

    let before = snapshot(&engine);
    let probe_off = before.s_write + 4 * 1024 * 1024;
    assert!(
        probe_off >= PROBE_ISOLATION_FOFF,
        "probe must clear the 8 MiB floor"
    );
    assert!(
        before.mapped(probe_off) >= before.s_write + PROBE_FUTURE_MARGIN,
        "probe must land beyond the future margin and be non-adjacent"
    );

    let mut pbuf = vec![0x5Au8; TS_ALIGNED_LEN];
    let n = engine.serve_live(0, probe_off, &mut pbuf, 0);
    assert_eq!(n, pbuf.len(), "probe reply must fill the request");
    assert!(
        is_null_block(&pbuf, probe_off),
        "probe reply must be phase-aware NULLs"
    );

    let after = snapshot(&engine);
    assert_eq!(
        after.last_fend, before.last_fend,
        "isolated probe must not move the chain"
    );
    assert_eq!(after.base_foff, before.base_foff);
    assert_eq!(after.base_spos, before.base_spos);
    assert_eq!(
        after.epoch_seq, before.epoch_seq,
        "isolated probe must not re-anchor"
    );
}

/// Repeated starvation on one established reader (R1). Every attempt requests
/// the **pre-starvation** writer-edge offset, never the possibly corrupted
/// post-read cursor, and all replies are collected before asserting.
///
/// Classification: attempt 1 is adjacent (`Continue`); retries are non-adjacent
/// but `probe_pos` stays inside the resident window (`is_future`/`is_behind`
/// false) with a sub-second gap, so `gap_jump_candidate` is false and
/// `jump_count` never reaches 4 — also `Continue`, no `ProbeReject`,
/// `ConfirmedSeek` or `ProbeReject` (never a gadget action).
#[test]
fn repeated_starvation_on_one_reader_must_not_advance_successful_cursor() {
    let stream = make_stream(STREAM_PACKETS);
    let lens = [TS_ALIGNED_LEN, PACKET_MULTIPLE_LEN, TS_ALIGNED_LEN];
    let engine = engine_with_identity_anchor(&stream);
    serve_real_media(&engine);
    let base = park_at_writer_edge(&engine);
    let foff = base.last_fend;
    let mapped = base.mapped(foff);

    let mut seen = Vec::new();
    for len in lens {
        let mut dst = vec![0x5Au8; len];
        let n = engine.serve_live(0, foff, &mut dst, 0);
        let obs = (len, n, is_null_block(&dst, mapped), snapshot(&engine));
        seen.push(obs);
    }

    assert!(seen.len() >= 3, "need >= 3 attempts");
    let mut bad = Vec::new();
    for (i, (len, n, null_ok, after)) in seen.iter().enumerate() {
        if n != len {
            bad.push(format!("attempt {i}: short reply {n}/{len}"));
        }
        if !null_ok || after.mapped(foff) != mapped {
            bad.push(format!("attempt {i}: no phase-aware NULLs at {mapped}"));
        }
        if !after.active
            || after.file_id != base.file_id
            || after.base_foff != base.base_foff
            || after.base_spos != base.base_spos
            || after.epoch_seq != base.epoch_seq
        {
            bad.push(format!("attempt {i}: anchor/epoch moved"));
        }
        if after.last_fend != base.last_fend {
            bad.push(format!(
                "attempt {i}: cursor advanced {} -> {}",
                base.last_fend, after.last_fend
            ));
        }
    }
    assert!(
        bad.is_empty(),
        "{} repeated-starvation violation(s):\n{}",
        bad.len(),
        bad.join("\n")
    );
}
