//! RC-001 iteration 12 — host reproduction of the ConnectShare "Return after a
//! long session" freeze seen in the iteration-11 hardware window.
//!
//! Hardware evidence (window 2026-10-21, candidate `1d189404…`, LEADBACK 12 MiB):
//!
//! * A 75 s session returned cleanly (the player resumed at the same file
//!   offset; the epoch was not restarted).
//! * A ~3.5 min session + 61 s idle + reopen made the player ask for
//!   `foff = 65 424` (the start of the file) while the old mapping was washed
//!   out. The engine classified that as `is_restart`, re-anchored cold to
//!   `live - lead` (measured lead 11.84 MiB ≈ 19 s) and *did* deliver real
//!   bytes — yet the TV froze on a static frame and stopped issuing reads.
//! * Before that restart read the player had issued out-of-window probes at
//!   `foff ≈ 12 GiB`, each answered with a full block of zeros (NULL).
//!
//! This harness replays that geometry against the real engine without a
//! wall-clock wait: pre-filled ring, a washed-out bookmark, a simulated 61 s
//! idle gap (`shift_time_back`), the player's duration probes, then the
//! restart read. It measures, at the return:
//!
//! 1. whether the delivered block is real data or zeros,
//! 2. the anchor lead actually honoured,
//! 3. the structural validity of the delivered TS grid, and
//! 4. the timeline step (PTS) across the return.
//!
//! The assertions encode only the audit's hard requirements: a sequential
//! return must deliver real bytes on a valid TS grid and honour the lead.

use std::time::Duration;

use fuse_ntfs::contract::{StreamBackend, TS_PKT_SZ};
use fuse_ntfs::engine::live_engine::LiveEngine;
use fuse_ntfs::engine::ts_xform::{
    pts_sub, pts_to_bytes, ts_pes_ts, ts_pid, PCR_MOD, PTS_MOD, SPLICE_PTS_EPS,
};

const MIB: u64 = 1024 * 1024;
const BLOCK: usize = 65_536;

/// Geometry copied verbatim from the iteration-11 window log.
const TV_RESTART_FOFF: u64 = 65_424;
const TV_PROBE_FOFF: u64 = 12_826_828_800;
const IDLE_GAP: Duration = Duration::from_secs(61);
const LEAD_MIB: u64 = 12;

/// Valid MPEG-TS-ish feed at the measured source cadence: 5.3 Mbit/s means one
/// 188-byte packet per 283.8 us, i.e. ~26 PTS ticks per packet at 90 kHz.
const PTS_PER_PKT: u64 = 26;

/// Valid MPEG-TS-ish feed: PAT, video PES (PCR on the first packet of each
/// group) and audio PES interleaved, PTS advancing at the real source cadence.
fn make_feed(bytes: usize, base_pts: u64) -> Vec<u8> {
    let n = bytes / TS_PKT_SZ;
    let mut v = Vec::with_capacity(n * TS_PKT_SZ);
    let mut cc_v = 0u8;
    let mut cc_a = 0u8;
    let mut cc_pat = 0u8;
    for i in 0..n {
        let mut p = [0xFFu8; TS_PKT_SZ];
        if i % 40 == 0 {
            p[0] = 0x47;
            p[1] = 0x40;
            p[2] = 0x00;
            p[3] = 0x10 | (cc_pat & 0x0F);
            p[4] = 0x00;
            p[5] = 0x00;
            cc_pat = cc_pat.wrapping_add(1) & 0x0F;
        } else if i % 5 == 0 {
            let pts = (base_pts + (i as u64) * PTS_PER_PKT) % PTS_MOD;
            p[0] = 0x47;
            p[1] = 0x41;
            p[2] = 0x00;
            p[3] = 0x30 | (cc_v & 0x0F);
            p[4] = 0x07;
            p[5] = 0x10;
            let pcr = (pts * 300 + 1_200_000) % PCR_MOD;
            let base = pcr / 300;
            let ext = pcr % 300;
            p[6] = (base >> 25) as u8;
            p[7] = (base >> 17) as u8;
            p[8] = (base >> 9) as u8;
            p[9] = (base >> 1) as u8;
            p[10] = (((base & 1) as u8) << 7) | 0x7E | ((ext >> 8) as u8 & 0x01);
            p[11] = ext as u8;
            let off = 12;
            p[off] = 0x00;
            p[off + 1] = 0x00;
            p[off + 2] = 0x01;
            p[off + 3] = 0xE0;
            p[off + 6] = 0x80;
            p[off + 7] = 0x80;
            p[off + 8] = 0x05;
            let mut b = [0u8; 5];
            pts_to_bytes(pts, &mut b);
            p[off + 9..off + 14].copy_from_slice(&b);
            cc_v = cc_v.wrapping_add(1) & 0x0F;
        } else {
            let pts = (base_pts + (i as u64) * PTS_PER_PKT) % PTS_MOD;
            p[0] = 0x47;
            p[1] = 0x41;
            p[2] = 0x01; // audio PID 0x0101
            p[3] = 0x10 | (cc_a & 0x0F);
            let off = 4;
            p[off] = 0x00;
            p[off + 1] = 0x00;
            p[off + 2] = 0x01;
            p[off + 3] = 0xC0;
            p[off + 6] = 0x80;
            p[off + 7] = 0x80;
            p[off + 8] = 0x05;
            let mut b = [0u8; 5];
            pts_to_bytes(pts, &mut b);
            p[off + 9..off + 14].copy_from_slice(&b);
            cc_a = cc_a.wrapping_add(1) & 0x0F;
        }
        v.extend_from_slice(&p);
    }
    v
}

fn mapped_pos(engine: &LiveEngine, foff: u64) -> u64 {
    let a = engine.anchor.lock().unwrap_or_else(|e| e.into_inner());
    if !a.active {
        return foff;
    }
    if foff >= a.base_foff {
        a.base_spos + (foff - a.base_foff)
    } else {
        a.base_spos.saturating_sub(a.base_foff - foff)
    }
}

fn s_write(engine: &LiveEngine) -> u64 {
    engine.ring.lock().s_write()
}

fn base_spos(engine: &LiveEngine) -> u64 {
    engine.anchor.lock().unwrap_or_else(|e| e.into_inner()).base_spos
}

fn xform_deltas(engine: &LiveEngine) -> (u64, i64, i64) {
    engine.xform.lock().unwrap_or_else(|e| e.into_inner()).deltas()
}

/// First PES PTS (any PID) in the raw ring at/after `from`, within 1 MiB — the
/// same sample `collect_raw` takes at the new anchor.
fn raw_first_pes_pts(engine: &LiveEngine, from: u64) -> Option<u64> {
    let mut win = vec![0u8; MIB as usize];
    let n = engine.ring.lock().read_window(from, &mut win);
    win.truncate(n);
    first_pes(&win, from).map(|(_, p)| p)
}

fn anchor_epoch(engine: &LiveEngine) -> u64 {
    engine.anchor.lock().unwrap_or_else(|e| e.into_inner()).epoch_seq
}

/// A NULL (all-zero) block is the engine's "no data" answer; a real TS block
/// always carries 0x47 sync bytes, so totals of zero bytes are meaningless.
fn is_null_block(b: &[u8]) -> bool {
    !b.is_empty() && b.iter().all(|x| *x == 0)
}

/// First PES timestamp in the block (any PID), as the player would read it.
/// `abs_off` is the block's file/stream offset: the 188 grid is aligned to it,
/// not to the buffer start (a 64 KiB read at a non-multiple-of-188 offset is
/// phase-shifted by `65536 % 188 = 112` bytes), so a naive 0/188/376 scan sees
/// almost no sync bytes and silently reports the previous block's timestamp.
fn first_pes(buf: &[u8], abs_off: u64) -> Option<(u16, u64)> {
    for off in grid_offsets(buf.len(), abs_off) {
        if off + TS_PKT_SZ > buf.len() {
            break;
        }
        let pkt = &buf[off..off + TS_PKT_SZ];
        if pkt[0] == 0x47 {
            let pid = ts_pid(pkt);
            if let Some((pts, _)) = ts_pes_ts(pkt) {
                return Some((pid, pts));
            }
        }
    }
    None
}

/// Last PES timestamp in the block (any PID). With a monotonic feed this is the
/// block maximum, i.e. exactly the state `update_served` keeps as the splice
/// reference (bit-exact: the seam must continue at `ref + SPLICE_PTS_EPS`).
fn last_pes(buf: &[u8], abs_off: u64) -> Option<(u16, u64)> {
    let mut found = None;
    for off in grid_offsets(buf.len(), abs_off) {
        if off + TS_PKT_SZ > buf.len() {
            break;
        }
        let pkt = &buf[off..off + TS_PKT_SZ];
        if pkt[0] == 0x47 {
            let pid = ts_pid(pkt);
            if let Some((pts, _)) = ts_pes_ts(pkt) {
                found = Some((pid, pts));
            }
        }
    }
    found
}

/// (grid boundaries, boundaries whose sync byte is 0x47) — a valid grid means
/// equal. Phase-aware: boundaries follow the file offset, not the buffer start.
fn ts_grid(buf: &[u8], abs_off: u64) -> (usize, usize) {
    let offs: Vec<usize> = grid_offsets(buf.len(), abs_off)
        .into_iter()
        .filter(|off| *off + TS_PKT_SZ <= buf.len())
        .collect();
    let good = offs
        .iter()
        .filter(|off| buf[**off] == 0x47)
        .count();
    (offs.len(), good)
}

/// The engine writes its 188-byte grid phase-aligned to the *file* offset
/// (`fill_null_at`), so grid boundaries live where `(abs_off + off) % 188 == 0`.
fn grid_offsets(len: usize, abs_off: u64) -> Vec<usize> {
    let mut v = Vec::new();
    let mut off = 0usize;
    while off < len {
        let phase = ((abs_off + off as u64) % TS_PKT_SZ as u64) as usize;
        let chunk = (len - off).min(TS_PKT_SZ - phase);
        v.push(off);
        off += chunk;
    }
    v
}

/// TS null packets (PID 0x1FFF) inside a delivered block: filler the decoder
/// cannot decode. A real content block must not contain any.
fn null_pkts_at(buf: &[u8], abs_off: u64) -> usize {
    grid_offsets(buf.len(), abs_off)
        .into_iter()
        .filter(|off| {
            let p = &buf[*off..(*off + TS_PKT_SZ).min(buf.len())];
            p.len() == TS_PKT_SZ && p[0] == 0x47 && ts_pid(p) == 0x1FFF
        })
        .count()
}

/// First four packets as (pid, PUSI, carries a PES timestamp).
fn head_pkts(buf: &[u8]) -> Vec<(u16, bool, bool)> {
    let slots = buf.len() / TS_PKT_SZ;
    (0..slots.min(4))
        .map(|i| {
            let p = &buf[i * TS_PKT_SZ..(i + 1) * TS_PKT_SZ];
            let pusi = p[0] == 0x47 && (p[1] & 0x40) != 0;
            (ts_pid(p), pusi, ts_pes_ts(p).is_some())
        })
        .collect()
}

struct Report {
    open_lead: u64,
    open_epoch: u64,
    session_foff: u64,
    session_reads: usize,
    session_null_blocks: usize,
    epoch_reanchors: Vec<(u64, u64, u64)>,
    washed_out: bool,
    bookmark_spos: u64,
    ring_old: u64,
    probe_null_blocks: usize,
    return_bytes: usize,
    return_null_block: bool,
    return_lead: u64,
    return_epoch: u64,
    return_grid: (usize, usize),
    pre_break_pts: Option<u64>,
    return_pts: Option<u64>,
    return_anchor: u64,
    return_raw_anchor_pts: Option<u64>,
    return_deltas: (u64, i64, i64),
    post_return_reads: usize,
    post_return_null_blocks: usize,
    probe_null_pkts: usize,
    probe_detail: Vec<(usize, u8, u8, u8, u8)>,
    return_null_pkts: usize,
    return_head: Vec<(u16, bool, bool)>,
}

fn run() -> Report {
    let engine = LiveEngine::with_lead(LEAD_MIB * MIB);
    let chunk_bytes = 32 * MIB as usize;
    let chunk_pkts = (chunk_bytes / TS_PKT_SZ) as u64;
    let mut base_pts = 900_000u64;
    let mut next_chunk = || {
        let f = make_feed(chunk_bytes, base_pts);
        base_pts += chunk_pkts * PTS_PER_PKT; // continuous timeline across chunks
        f
    };

    // Pre-fill the ring (96 MiB) so a cold open honours the configured lead.
    for _ in 0..3 {
        engine.ring.write_chunk(&next_chunk());
    }

    let mut buf = vec![0u8; BLOCK];

    // --- cold open: anchor lands `lead` behind the live edge.
    let n = engine.serve_live(0, 0, &mut buf, 1200);
    let open_lead = s_write(&engine).saturating_sub(mapped_pos(&engine, 0));
    let open_epoch = anchor_epoch(&engine);

    // --- a long session: sequential playback past the 32 MiB restart horizon.
    // The live edge must keep moving (as the paced feeder does on hardware),
    // otherwise the reader reaches the edge every lead-length and the engine
    // re-anchors - an artifact that does not happen on the tablet.
    let mut foff = n as u64;
    let mut session_null_blocks = 0usize;
    let mut session_reads = 0usize;
    let mut epoch_reanchors: Vec<(u64, u64, u64)> = Vec::new(); // (foff, epoch, s_write)
    let mut last_pts = first_pes(&buf[..n], 0).map(|(_, p)| p);
    let mut epoch_now = open_epoch;
    while foff < 40 * MIB {
        // Keep at least 4 MiB of live cushion ahead of the reader.
        if s_write(&engine).saturating_sub(mapped_pos(&engine, foff)) < 4 * MIB {
            engine.ring.write_chunk(&next_chunk());
        }
        let n = engine.serve_live(0, foff, &mut buf, 1200);
        if n == 0 {
            break;
        }
        session_reads += 1;
        if is_null_block(&buf[..n]) {
            session_null_blocks += 1;
        }
        if let Some((_, p)) = last_pes(&buf[..n], foff) {
            last_pts = Some(p);
        }
        foff += n as u64;
        let e = anchor_epoch(&engine);
        if e != epoch_now {
            epoch_reanchors.push((foff, e, s_write(&engine)));
            epoch_now = e;
        }
    }
    let pre_break_pts = last_pts;
    let session_foff = foff;

    // --- the Return: 61 s idle (the hardware gap), live edge keeps moving and
    // washes the bookmark out of the 128 MiB ring.
    engine.ring.lock().shift_time_back(IDLE_GAP);
    for _ in 0..6 {
        engine.ring.write_chunk(&next_chunk()); // +192 MiB behind the gap
    }
    let (ring_old, _sw) = {
        let t = engine.ring.lock().telemetry();
        (t.ring_old, t.s_write)
    };
    let bookmark_spos = mapped_pos(&engine, foff);
    let washed_out = bookmark_spos < ring_old;

    // --- the player's duration probes, exactly as the TV issued them.
    let mut probe_null_blocks = 0usize;
    let mut probe_null_pkts = 0usize;
    let mut probe_detail: Vec<(usize, u8, u8, u8, u8)> = Vec::new();
    for _ in 0..2 {
        // Poison the buffer: if the filler is not written, this reveals it.
        buf.iter_mut().for_each(|b| *b = 0xAA);
        let n = engine.serve_live(0, TV_PROBE_FOFF, &mut buf, 1200);
        if is_null_block(&buf[..n]) {
            probe_null_blocks += 1;
        }
        probe_null_pkts += null_pkts_at(&buf[..n], TV_PROBE_FOFF);
        let unwritten = buf[..n].iter().filter(|b| **b == 0xAA).count();
        probe_detail.push((n, buf[0], buf[1], buf[2], buf[3]));
        assert_eq!(unwritten, 0, "the engine must fill every byte of a read response");
    }

    // --- the restart read: the TV asks for the start of the file.
    let n = engine.serve_live(0, TV_RESTART_FOFF, &mut buf, 1200);
    let return_null_block = is_null_block(&buf[..n]);
    let return_bytes = n;
    let return_lead = s_write(&engine).saturating_sub(mapped_pos(&engine, TV_RESTART_FOFF));
    let return_epoch = anchor_epoch(&engine);
    let return_grid = ts_grid(&buf[..n], TV_RESTART_FOFF);
    let return_pts = first_pes(&buf[..n], TV_RESTART_FOFF).map(|(_, p)| p);
    // Proof of the reference-anchored warm splice: the epoch delta against the
    // raw sample `collect_raw` takes at the new anchor. A cold identity epoch
    // would leave delta_pts == 0; a stale reference shows up as a non-3000 step
    // against pre_break_pts.
    let return_anchor = base_spos(&engine);
    let return_raw_anchor_pts = raw_first_pes_pts(&engine, return_anchor);
    let return_deltas = xform_deltas(&engine);
    let return_null_pkts = null_pkts_at(&buf[..n], TV_RESTART_FOFF);
    let return_head = head_pkts(&buf[..n]);

    // --- does sequential playback resume cleanly after the return?
    let mut pos = TV_RESTART_FOFF + n as u64;
    let mut post_return_null_blocks = 0usize;
    let mut post_return_reads = 0usize;
    for _ in 0..8 {
        let n = engine.serve_live(0, pos, &mut buf, 1200);
        if n == 0 {
            break;
        }
        post_return_reads += 1;
        if is_null_block(&buf[..n]) {
            post_return_null_blocks += 1;
        }
        pos += n as u64;
    }

    Report {
        open_lead,
        open_epoch,
        session_foff,
        session_reads,
        session_null_blocks,
        epoch_reanchors,
        washed_out,
        bookmark_spos,
        ring_old,
        probe_null_blocks,
        return_bytes,
        return_null_block,
        return_lead,
        return_epoch,
        return_grid,
        pre_break_pts,
        return_pts,
        return_anchor,
        return_raw_anchor_pts,
        return_deltas,
        post_return_reads,
        post_return_null_blocks,
        probe_null_pkts,
        probe_detail,
        return_null_pkts,
        return_head,
    }
}

#[test]
fn tv_return_after_long_session_delivers_real_data_on_a_valid_grid() {
    let r = run();
    println!("\n=== TV Return reproduction (12 MiB lead, 128 MiB ring) ===");
    println!(
        "cold open: lead {:.2} MiB, epoch {}",
        r.open_lead as f64 / MIB as f64,
        r.open_epoch
    );
    println!(
        "session: {} reads, {:.2} MiB delivered, {} NULL blocks",
        r.session_reads,
        r.session_foff as f64 / MIB as f64,
        r.session_null_blocks
    );
    for (foff, ep, sw) in &r.epoch_reanchors {
        println!(
            "  re-anchor during session at foff {:.2} MiB -> epoch {} (s_write {:.1} MiB)",
            *foff as f64 / MIB as f64,
            ep,
            *sw as f64 / MIB as f64
        );
    }
    println!(
        "return: bookmark spos {:.1} MiB, ring_old {:.1} MiB, washed_out={}",
        r.bookmark_spos as f64 / MIB as f64,
        r.ring_old as f64 / MIB as f64,
        r.washed_out
    );
    println!(
        "probes at {:.1} GiB: {} all-NULL block(s), {} null TS packets delivered",
        TV_PROBE_FOFF as f64 / 1024.0 / 1024.0 / 1024.0,
        r.probe_null_blocks,
        r.probe_null_pkts
    );
    println!("probe responses (n, first4) {:?}", r.probe_detail);
    println!(
        "restart read: {} bytes, NULL={}, lead {:.2} MiB, epoch {}, TS grid {}/{}",
        r.return_bytes,
        r.return_null_block,
        r.return_lead as f64 / MIB as f64,
        r.return_epoch,
        r.return_grid.1,
        r.return_grid.0
    );
    match (r.pre_break_pts, r.return_pts) {
        (Some(a), Some(b)) => println!(
            "timeline: pre-break PTS {} -> return PTS {} (step {} ticks = {:.2} s)",
            a,
            b,
            pts_sub(b, a),
            pts_sub(b, a) as f64 / 90_000.0
        ),
        _ => println!("timeline: PTS not observed on one side"),
    }
    println!(
        "returned block: {} null TS packets, head (pid,pusi,pts) {:?}",
        r.return_null_pkts, r.return_head
    );
    println!(
        "splice: anchor {} raw PES {:?} epoch/delta_pts/delta_pcr {:?}",
        r.return_anchor, r.return_raw_anchor_pts, r.return_deltas
    );
    println!(
        "post-return: {} sequential reads, {} NULL block(s)\n",
        r.post_return_reads, r.post_return_null_blocks
    );

    assert!(
        r.washed_out,
        "harness: the bookmark must be washed out of the ring for this scenario"
    );
    assert!(
        !r.return_null_block,
        "a sequential restart/return read must deliver real bytes, not a NULL block \
         (the hardware freeze correlated with zero-filled reads)"
    );
    assert_eq!(
        r.return_null_pkts, 0,
        "the returned content block must not contain null-packet filler"
    );
    assert_eq!(
        r.return_grid.0, r.return_grid.1,
        "the returned block must be a valid TS grid ({} of {} slots synced)",
        r.return_grid.1, r.return_grid.0
    );
    assert!(
        r.return_lead <= LEAD_MIB * MIB + 4 * MIB,
        "the return anchor lead {:.2} MiB exceeds the configured lead",
        r.return_lead as f64 / MIB as f64
    );
    assert!(
        r.return_lead >= (LEAD_MIB - 1) * MIB,
        "the return anchor lead {:.2} MiB fell short of the live band",
        r.return_lead as f64 / MIB as f64
    );
    // ITERATION 12 (B): the delivered timeline must CONTINUE at the last
    // delivered reference + SPLICE_PTS_EPS. A cold identity epoch cannot satisfy
    // this, nor can an un-spliced resume at the stale position: both show up as
    // a step of the raw content distance instead of the 3000-tick seam.
    match (r.pre_break_pts, r.return_pts) {
        (Some(a), Some(b)) => assert_eq!(
            pts_sub(b, a),
            SPLICE_PTS_EPS as i64,
            "the return must continue the delivered timeline at ref + SPLICE_PTS_EPS \
             (warm splice), never restart it (cold identity epoch)"
        ),
        (a, b) => panic!("timeline PTS missing across the return: pre={a:?} post={b:?}"),
    }
    assert_eq!(
        r.post_return_null_blocks, 0,
        "sequential playback after the return must be all real media"
    );
    assert_eq!(
        r.post_return_reads, 8,
        "the player must keep reading after the return (the hardware freeze showed \
         zero reads after the cold re-anchor)"
    );
}
