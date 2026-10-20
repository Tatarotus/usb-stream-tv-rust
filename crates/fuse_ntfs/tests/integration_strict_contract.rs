//! Strict contract suite for Return/Resume, channel switch, probe isolation
//! and the timeline-splice ("Caminho C") invariants.
//!
//! This is the hardened counterpart of
//! `integration_return_and_channel_switch.rs`. Every assertion here is
//! *phase-free* (188-aligned reads where exact seam math is required) and
//! *bit-exact* (expected values are recomputed from the raw ring, never
//! approximated with tolerances):
//!
//! 1. `return_resume_seam_is_bit_exact` — 4th chained read triggers exactly
//!    one splice epoch; the anchor is the position-preserving PAT below the
//!    bookmark; `delta_pts == pts_sub(ref + 3000, raw_first)`; every delivered
//!    PES PTS/PCR equals `pts_apply`/`pcr_apply` of the raw packet at the same
//!    absolute position; video CC continues `prev + 1 (mod 16)` across the
//!    seam; calibration tier is `Served`.
//! 2. `return_resume_arbitrary_phase_is_position_preserving` — same splice
//!    with the real 1_900_544 bookmark and 16 KiB reads (odd phase).
//! 3. `gap_and_jump_boundaries_are_strict` — SM-level: 20_000 ms is not a
//!    gap, 20_001 ms is; 256 KiB jump is not a resume, 256 KiB + 1 is.
//! 4. `interrupted_debounce_never_splices` — a broken resume chain returns to
//!    Live and later adjacent reads never produce `ResumeAfterGap`.
//! 5. `washed_out_return_falls_back_to_live_edge` — the first two fully
//!    overwritten requests remain all-NULL with the epoch and successful
//!    cursor frozen; the third consecutive request confirms the bounded
//!    recovery and re-anchors to the live band, calibrated from the genuine
//!    last delivered state (`Served` tier — no NULL-only reply ever stamps a
//!    checkpoint); `ref + 3000` continuity holds against the last real
//!    playback block.
//! 6. `probe_isolation_future_and_behind` — future probes (>= s_write + 2 MiB)
//!    never touch the chain (epoch, anchor, fend frozen) and return all-NULL;
//!    behind-probes (< ring_old, non-adjacent) likewise stay NULL-only and
//!    never request a gadget action (TASK-005); adjacency then resumes with
//!    bit-exact continuity.
//! 7. `channel_switch_needs_contiguous_run` — non-contiguous probes never
//!    switch; the switch fires exactly on the 16th contiguous 64 KiB read
//!    (1 MiB); each switch bumps the epoch once and resets to identity.
//! 8. `pts_wrap_playback_and_splice` — 33-bit PTS rollover: playback passes
//!    the wrap forward on the circle; a splice landing after the wrap keeps
//!    `ref + 3000` continuity.
//! 9. `live_edge_lock_waits_and_delivers` — a read at the writer head blocks
//!    on the condvar and delivers the feeder bytes that arrive later, with
//!    `near_starve == 0` and no NULL fill.
//! 10. `d12_pat_sps_scan_is_linear` — regression guard for the tier-1
//!    PAT+SPS scan: a cold re-anchor on an SPS-less 24 MiB stream must not
//!    walk the quadratic PAT x 1 MiB matrix (~7.6 s pre-fix).
//! 11. `collect_raw_pid_coverage` — verifies collect_raw samples all reference
//!     PIDs (PAT, Video, Audio) without premature truncation.
//! 12. `seam_audio_cc_contract` — verifies audio CC continuity (last + 1 mod 16)
//!     across position-preserving resume splices.
//! 13. `cold_reopen_from_zero_after_washout_is_fresh_live` — Return + re-enter
//!     with the player restarting the file from offset 0: `restart_at_zero`
//!     lands bit-exactly on the live band in one fresh identity epoch.
//! 14. `known_bug_resume_at_large_bookmark_never_returns_to_live` (ignored) —
//!     Return + re-enter resuming at a saved bookmark >= 8 MiB: probe
//!     isolation preempts the 2-step resume and the chain cursor freezes, so
//!     the TV never reaches the live band in-band.
//! 15. `stale_bookmark_after_return_cold_reanchors_fresh_live` — the tablet
//!     incident: a large backward jump (> 32 MiB, stale saved bookmark
//!     below 8 MiB) into a washed-out region is a cold restart — the very
//!     first read after Return delivers the live band, PAT with SPS
//!     immediately behind it, bit-exact ring bytes in a fresh identity
//!     epoch; never the NULL head that loses PAT/SPS and freezes the MStar.
//! 16. `known_bug_large_bookmark_backward_jump_probe_preempts_cold_reanchor`
//!     (ignored) — the same incident with the bookmark >= 8 MiB: the probe
//!     rejection returns before the cold re-anchor, so the fixed path is
//!     unreachable and the first read is still NULL soup.

use std::sync::Arc;
use std::time::{Duration, Instant};

use fuse_ntfs::contract::{StreamBackend, LEADBACK, TS_PKT_SZ};
use fuse_ntfs::engine::live_engine::LiveEngine;
use fuse_ntfs::engine::ring::NULL_PKT;
use fuse_ntfs::engine::state_machine::{SmCtx, SmDecision, StateMachine};
use fuse_ntfs::engine::ts_xform::{
    pcr_apply, pcr_sub, pts_apply, pts_sub, pts_to_bytes, pts_wrap, ts_cc, ts_has_sps, ts_pcr,
    ts_pes_ts, ts_pid, ts_write_pcr, PCR_MOD, PTS_MOD, SPLICE_PTS_EPS,
};
use fuse_ntfs::vfs::catalog::{Catalog, LIVE_INO_BASE, SWITCH_THRESHOLD};
use fuse_ntfs::vfs::dispatcher::NtfsServer;
use fuse_ntfs::vfs::ntfs_mft::NtfsTemplate;

const MI: u64 = 1024 * 1024;
const BLOCK: usize = 65_536;
/// 188 * 100: phase-free read size for exact seam math.
const READ: usize = 18_800;
/// Packet unit of `strict_stream`: bytes per 10 packets.
const UNIT_B: u64 = 10 * TS_PKT_SZ as u64; // 1880

const PID_PAT: u16 = 0x0000;
const PID_VID: u16 = 0x0100;
const PID_AUD: u16 = 0x0101;

/// Production-shaped strict stream. Fixed 10-packet unit:
/// `[PAT, SPS(vid), VID+PCR, AUDIO, VID, NULL, VID, AUDIO, NULL, VID]`.
///
/// * PTS(packet i) = `base_pts + i * 1000`; PCR = PTS * 300 mod PCR_MOD.
/// * Per-PID continuity counters are strictly sequential (mod 16) in stream
///   order, so "first CC after a seam == previous CC + 1" is exact.
/// * With `with_sps = false` the SPS packet is replaced by a VIDEO packet
///   (same CC sequence) so the stream stays decodable-shaped.
fn strict_stream(base_pts: u64, n_packets: usize, with_sps: bool) -> Vec<u8> {
    let mut v = Vec::with_capacity(n_packets * TS_PKT_SZ);
    let (mut cc_pat, mut cc_vid, mut cc_aud) = (0u8, 0u8, 0u8);
    let null = |v: &mut Vec<u8>| v.extend_from_slice(&NULL_PKT);

    for i in 0..n_packets {
        let pts = (base_pts + i as u64 * 1000) % PTS_MOD;
        match i % 10 {
            0 => {
                let mut p = [0xFFu8; TS_PKT_SZ];
                p[0] = 0x47;
                p[1] = 0x40;
                p[2] = 0x00;
                p[3] = 0x10 | (cc_pat & 0x0F);
                p[4] = 0x00;
                p[5] = 0x00; // table_id = PAT
                cc_pat = cc_pat.wrapping_add(1) & 0x0F;
                v.extend_from_slice(&p);
            }
            1 => {
                if with_sps {
                    let mut p = [0xFFu8; TS_PKT_SZ];
                    p[0] = 0x47;
                    p[1] = 0x41; // PUSI, PID 0x0100
                    p[2] = 0x00;
                    p[3] = 0x10 | (cc_vid & 0x0F);
                    p[4] = 0x00;
                    p[5] = 0x00;
                    p[6] = 0x00;
                    p[7] = 0x01;
                    p[8] = 0x67; // NAL type 7 = SPS
                    cc_vid = cc_vid.wrapping_add(1) & 0x0F;
                    v.extend_from_slice(&p);
                } else {
                    let mut p = [0xFFu8; TS_PKT_SZ];
                    p[0] = 0x47;
                    p[1] = 0x41;
                    p[2] = 0x00;
                    p[3] = 0x10 | (cc_vid & 0x0F);
                    p[4] = 0x00;
                    cc_vid = cc_vid.wrapping_add(1) & 0x0F;
                    v.extend_from_slice(&p);
                }
            }
            2 => {
                let mut p = [0xFFu8; TS_PKT_SZ];
                p[0] = 0x47;
                p[1] = 0x41;
                p[2] = 0x00;
                p[3] = 0x30 | (cc_vid & 0x0F); // AF + payload
                p[4] = 0x07;
                p[5] = 0x10; // PCR flag
                ts_write_pcr((pts * 300) % PCR_MOD, &mut p);
                let off = 12usize; // 4 + 1 + 7
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
                cc_vid = cc_vid.wrapping_add(1) & 0x0F;
                v.extend_from_slice(&p);
            }
            3 | 7 => {
                let mut p = [0xFFu8; TS_PKT_SZ];
                p[0] = 0x47;
                p[1] = 0x41;
                p[2] = 0x01; // PID 0x0101
                p[3] = 0x10 | (cc_aud & 0x0F);
                p[4] = 0x00;
                p[5] = 0x00;
                p[6] = 0x01;
                p[7] = 0xBD; // private stream 1 (AC-3)
                p[8] = 0x00;
                p[9] = 0x00;
                p[10] = 0x80;
                p[11] = 0x80;
                p[12] = 0x05;
                let mut b = [0u8; 5];
                pts_to_bytes(pts, &mut b);
                p[13..18].copy_from_slice(&b);
                cc_aud = cc_aud.wrapping_add(1) & 0x0F;
                v.extend_from_slice(&p);
            }
            5 | 8 => null(&mut v),
            _ => {
                let mut p = [0xFFu8; TS_PKT_SZ];
                p[0] = 0x47;
                p[1] = 0x41;
                p[2] = 0x00;
                p[3] = 0x10 | (cc_vid & 0x0F);
                p[4] = 0x00;
                p[5] = 0x00;
                p[6] = 0x01;
                p[7] = 0xE0;
                p[8] = 0x00;
                p[9] = 0x00;
                p[10] = 0x80;
                p[11] = 0x80;
                p[12] = 0x05;
                let mut b = [0u8; 5];
                pts_to_bytes(pts, &mut b);
                p[13..18].copy_from_slice(&b);
                cc_vid = cc_vid.wrapping_add(1) & 0x0F;
                v.extend_from_slice(&p);
            }
        }
    }
    v
}

// ---------------------------------------------------------------- scanning

/// All complete 188-byte packets in `buf` whose first byte is at stream
/// position >= `foff`, paired with their absolute position.
fn pkts(buf: &[u8], foff: u64) -> Vec<(u64, &[u8])> {
    let phase = (foff % TS_PKT_SZ as u64) as usize;
    let mut i = if phase == 0 { 0 } else { TS_PKT_SZ - phase };
    let mut out = Vec::new();
    while i + TS_PKT_SZ <= buf.len() {
        out.push((foff + i as u64, &buf[i..i + TS_PKT_SZ]));
        i += TS_PKT_SZ;
    }
    out
}

/// True when `buf` is byte-exactly a run of NULL packets at any stream phase.
///
/// `fill_null_at` writes whole packets, so the buffer is exactly `NULL_PKT`
/// rotated by the phase: derive the phase from the first sync byte and check
/// every byte against the template. (A leading partial contains no `0x47`; a
/// trailing partial may contain the sync byte of the next packet.)
fn all_null(buf: &[u8]) -> bool {
    let Some(off) = buf.iter().position(|&b| b == 0x47) else {
        return false;
    };
    if off >= TS_PKT_SZ {
        return false;
    }
    let p = (TS_PKT_SZ - off) % TS_PKT_SZ;
    buf.iter()
        .enumerate()
        .all(|(i, &b)| b == NULL_PKT[(p + i) % TS_PKT_SZ])
}

fn cc_list(buf: &[u8], foff: u64, pid: u16) -> Vec<u8> {
    pkts(buf, foff)
        .iter()
        .filter(|(_, p)| p[0] == 0x47 && ts_pid(p) == pid)
        .map(|(_, p)| ts_cc(p))
        .collect()
}

fn first_cc(buf: &[u8], foff: u64, pid: u16) -> Option<u8> {
    cc_list(buf, foff, pid).first().copied()
}

fn last_cc(buf: &[u8], foff: u64, pid: u16) -> Option<u8> {
    cc_list(buf, foff, pid).last().copied()
}

/// Last PES PTS of any PID in stream order (mirrors `update_served`).
fn last_pes_pts_any(buf: &[u8], foff: u64) -> Option<u64> {
    pkts(buf, foff)
        .iter()
        .filter_map(|(_, p)| ts_pes_ts(p).map(|(pts, _)| pts))
        .last()
}

/// First PES PTS of any PID in stream order (mirrors `collect_raw`).
fn first_pes_pts_any(buf: &[u8], foff: u64) -> Option<u64> {
    pkts(buf, foff)
        .iter()
        .find_map(|(_, p)| ts_pes_ts(p).map(|(pts, _)| pts))
}

fn pcr_list(buf: &[u8], foff: u64) -> Vec<u64> {
    pkts(buf, foff)
        .iter()
        .filter_map(|(_, p)| ts_pcr(p))
        .collect()
}

// ---------------------------------------------------------------- ring access

fn raw_pkt_at(engine: &LiveEngine, abs: u64) -> [u8; TS_PKT_SZ] {
    let mut p = [0u8; TS_PKT_SZ];
    engine.ring.lock().read_at(abs, &mut p);
    p
}

/// First PES PTS (any PID) in the raw ring at/after `from`, within 1 MiB.
fn raw_first_pes_pts(engine: &LiveEngine, from: u64) -> Option<u64> {
    let mut win = vec![0u8; MI as usize];
    let n = engine.ring.lock().read_window(from, &mut win);
    win.truncate(n);
    first_pes_pts_any(&win, from)
}

fn ec(engine: &LiveEngine) -> u64 {
    engine.anchor.lock().unwrap().epoch_seq
}

fn base_spos(engine: &LiveEngine) -> u64 {
    engine.anchor.lock().unwrap().base_spos
}

fn deltas(engine: &LiveEngine) -> (u64, i64, i64) {
    engine.xform.lock().unwrap().deltas()
}

/// Plays forward sequentially from 0 until `end` file offset in `BLOCK` reads.
fn play_to(engine: &LiveEngine, end: u64) {
    let mut buf = vec![0u8; BLOCK];
    let mut foff = 0u64;
    while foff < end {
        let n = engine.serve_live(0, foff, &mut buf, 1000);
        assert_eq!(n, BLOCK, "playback read at {foff}");
        assert!(!all_null(&buf), "playback read at {foff} must be real data");
        foff += BLOCK as u64;
    }
}

/// Shared fixture: 112_000-packet stream A (SPS-bearing) + cold open, played
/// to 4 MiB, then a 45 s Return gap and 84_000 packets of stream B.
fn return_fixture(engine: &Arc<LiveEngine>) -> (u64, u64, u64) {
    engine
        .ring
        .write_chunk(&strict_stream(1_000_000, 112_000, true));
    let mut buf = vec![0u8; BLOCK];
    let n = engine.serve_live(0, 0, &mut buf, 1000);
    assert_eq!(n, BLOCK);
    let base0 = base_spos(engine);
    assert_eq!(base0 % TS_PKT_SZ as u64, 0, "anchor is 188-aligned");
    let e0 = ec(engine);
    play_to(engine, 4 * MI);
    let sw_before = engine.ring.lock().s_write();
    engine.ring.lock().shift_time_back(Duration::from_secs(45));
    engine
        .ring
        .write_chunk(&strict_stream(50_000_000, 84_000, true));
    let sw_after = engine.ring.lock().s_write();
    let _ = (sw_before, sw_after);
    (base0, e0, sw_after)
}

// ================================================================== test 1

/// The aligned, bit-exact seam: every seam quantity is recomputed from the
/// raw ring, so nothing here is a tolerance.
#[test]
fn return_resume_seam_is_bit_exact() {
    let engine = Arc::new(LiveEngine::new());
    let (base0, e0, sw) = return_fixture(&engine);

    let bm: u64 = 1_900_492; // 188 * 10109
    assert_eq!(bm % TS_PKT_SZ as u64, 0);
    let mut rb = vec![0u8; READ];

    // Reads 1..=3 (0-based 0..=2): pre-splice replay, identity epoch.
    for i in 0..3u64 {
        let f = bm + i * READ as u64;
        let n = engine.serve_live(0, f, &mut rb, 1000);
        assert_eq!(n, READ);
        assert_eq!(ec(&engine), e0, "read {i}: no splice before the 4th chained read");
        assert!(!all_null(&rb), "read {i}: replay is real in-window data");
        let mapped = base0 + f;
        let mut raw = vec![0u8; READ];
        engine.ring.lock().read_at(mapped, &mut raw);
        assert_eq!(rb, raw, "read {i}: identity epoch is byte-exact");
    }
    let ref_pts = match last_pes_pts_any(&rb, bm + 2 * READ as u64) {
        Some(p) => p,
        None => panic!("pre-splice tail must carry a PES"),
    };
    let last_vid_before = last_cc(&rb, bm + 2 * READ as u64, PID_VID).expect("pre-seam video CC");
    let last_pcr_before = *pcr_list(&rb, bm + 2 * READ as u64).last().expect("pre-seam PCR");

    // 4th chained read: ResumeAfterGap -> position-preserving splice.
    let f3 = bm + 3 * READ as u64;
    let counters_before = {
        let x = engine.xform.lock().unwrap();
        (x.ref_served, x.ref_ckpt, x.ref_scan, x.ref_none, x.splices)
    };
    let n = engine.serve_live(0, f3, &mut rb, 1000);
    assert_eq!(n, READ);
    assert_eq!(ec(&engine), e0 + 1, "exactly one splice epoch, on the 4th chained read");
    let base1 = base_spos(&engine);
    // Tier 1 walks back from the snapped bookmark to the PAT of group
    // `floor(f3 / 1880)`, exactly one PAT period below the target.
    assert_eq!(
        base1,
        base0 + (f3 / UNIT_B) * UNIT_B,
        "position-preserving PAT anchor at the bookmark"
    );
    assert!(sw - base1 > LEADBACK, "anchor must not fall back to the live edge");

    let (eid, dpts, dpcr) = deltas(&engine);
    assert_eq!(eid, e0 + 1);
    let target_pts = pts_wrap(ref_pts + SPLICE_PTS_EPS);
    let raw_first = raw_first_pes_pts(&engine, base1).expect("raw anchor PES");
    assert_eq!(dpts, pts_sub(target_pts, raw_first), "delta_pts is exact");
    assert_eq!(dpcr, dpts * 300, "PCR tracks PTS at 27 MHz");
    let counters_after = {
        let x = engine.xform.lock().unwrap();
        (x.ref_served, x.ref_ckpt, x.ref_scan, x.ref_none, x.splices)
    };
    assert_eq!(counters_after.0, counters_before.0 + 1, "calibration tier = Served");
    assert_eq!(counters_after.1, counters_before.1, "no ckpt tier");
    assert_eq!(counters_after.2, counters_before.2, "no ring-scan tier");
    assert_eq!(counters_after.3, counters_before.3, "no invalid calibration");
    assert_eq!(counters_after.4, counters_before.4 + 1, "one splice");

    // The delivered block starts exactly at the new anchor (aligned read).
    assert_eq!(first_pes_pts_any(&rb, f3), Some(target_pts), "first seam PES == ref + 3000");

    // Delivered bytes start at the mapped anchor position (== base1 here).
    let m3 = base1;
    // Bit-exact per-packet transform: every PES/PCR equals the raw packet at
    // the same absolute position under the epoch deltas, and PCR only moves
    // forward inside the block.
    let mut prev_pcr: Option<u64> = None;
    let mut pes_seen = 0usize;
    for (abs, pkt) in pkts(&rb, m3) {
        if pkt[0] != 0x47 {
            continue;
        }
        let raw = raw_pkt_at(&engine, abs);
        if let Some((pts, _)) = ts_pes_ts(pkt) {
            let (raw_pts, _) = ts_pes_ts(&raw).expect("raw PES at the same position");
            assert_eq!(pts, pts_apply(raw_pts, dpts), "PES PTS at abs={abs}");
            pes_seen += 1;
        }
        if let Some(pcr) = ts_pcr(pkt) {
            let raw_pcr = ts_pcr(&raw).expect("raw PCR at the same position");
            assert_eq!(pcr, pcr_apply(raw_pcr, dpcr), "PCR at abs={abs}");
            if let Some(pp) = prev_pcr {
                assert!(pcr_sub(pcr, pp) > 0, "PCR strictly forward at abs={abs}");
            }
            prev_pcr = Some(pcr);
        }
    }
    assert!(pes_seen >= 4, "block must deliver several PES packets");

    // Video CC continues prev + 1 across the seam and stays sequential inside.
    let first_vid_after = first_cc(&rb, f3, PID_VID).expect("seam video CC");
    assert_eq!(
        first_vid_after,
        (last_vid_before + 1) & 0x0F,
        "video CC continues across the seam (prev {last_vid_before})"
    );
    let vid_ccs = cc_list(&rb, f3, PID_VID);
    for w in vid_ccs.windows(2) {
        assert_eq!(w[1], (w[0] + 1) & 0x0F, "video CC sequential inside the block");
    }
    // PCR seam: strictly forward from the last pre-splice PCR.
    let first_pcr_after = pcr_list(&rb, f3)[0];
    assert!(pcr_sub(first_pcr_after, last_pcr_before) > 0, "PCR forward across the seam");
    // No NULL *fill*: the source stream carries exactly 2 NULLs per 10-packet
    // unit; a fill would add more.
    let nulls: usize = pkts(&rb, f3).iter().filter(|(_, p)| ts_pid(p) == 0x1FFF).count();
    assert_eq!(nulls, (READ / TS_PKT_SZ / 10) * 2, "no NULL fill inside the block");

    // Following sequential read stays in the same epoch and keeps exactness.
    let rb3 = rb.clone();
    let f4 = f3 + READ as u64;
    let n4 = engine.serve_live(0, f4, &mut rb, 1000);
    assert_eq!(n4, READ);
    assert_eq!(ec(&engine), e0 + 1, "no extra epoch on the next read");
    let m4 = base1 + READ as u64;
    for (abs, pkt) in pkts(&rb, m4) {
        if pkt[0] != 0x47 {
            continue;
        }
        let raw = raw_pkt_at(&engine, abs);
        if let Some((pts, _)) = ts_pes_ts(pkt) {
            let (raw_pts, _) = ts_pes_ts(&raw).expect("raw PES");
            assert_eq!(pts, pts_apply(raw_pts, dpts), "post-seam PES PTS at abs={abs}");
        }
        if let Some(pcr) = ts_pcr(pkt) {
            let raw_pcr = ts_pcr(&raw).expect("raw PCR");
            assert_eq!(pcr, pcr_apply(raw_pcr, dpcr), "post-seam PCR at abs={abs}");
        }
    }
    let vid_ccs4 = cc_list(&rb, f4, PID_VID);
    for w in vid_ccs4.windows(2) {
        assert_eq!(w[1], (w[0] + 1) & 0x0F, "video CC sequential in the next block");
    }
    assert_eq!(
        first_cc(&rb, f4, PID_VID).expect("next block video CC"),
        (last_cc(&rb3, f3, PID_VID).expect("seam block video CC") + 1) & 0x0F,
        "video CC continues across the block boundary"
    );
    let nulls4: usize = pkts(&rb, f4).iter().filter(|(_, p)| ts_pid(p) == 0x1FFF).count();
    assert_eq!(nulls4, (READ / TS_PKT_SZ / 10) * 2, "no NULL fill in the next block");
}

// ================================================================== test 2

/// Same splice, but with the *real* TV bookmark (1_900_544, phase 52) and the
/// real 16 KiB block size. Position-preserving selection must still hold and
/// every delivered PES/PCR is checked bit-exactly against the raw ring.
#[test]
fn return_resume_arbitrary_phase_is_position_preserving() {
    let engine = Arc::new(LiveEngine::new());
    let (base0, e0, sw) = return_fixture(&engine);

    let bm: u64 = 1_900_544; // real saved bookmark, not 188-aligned
    let mut rb = vec![0u8; 16_384];

    for i in 0..3u64 {
        let f = bm + i * 16_384;
        let n = engine.serve_live(0, f, &mut rb, 1000);
        assert_eq!(n, 16_384);
        assert_eq!(ec(&engine), e0, "read {i}: no splice before the chain confirms");
        assert!(!all_null(&rb));
    }
    let ref_pts = last_pes_pts_any(&rb, bm + 2 * 16_384).expect("pre-splice tail PES");

    let f3 = bm + 3 * 16_384;
    let n = engine.serve_live(0, f3, &mut rb, 1000);
    assert_eq!(n, 16_384);
    assert_eq!(ec(&engine), e0 + 1, "one splice epoch on the 4th chained read");
    let base1 = base_spos(&engine);

    // Position-preserving: far from the live edge, one PAT walk-back of the
    // snapped bookmark target.
    assert!(sw - base1 > LEADBACK, "must not fall back to the live edge");
    let target = {
        let t = base0 + f3;
        t - (t % TS_PKT_SZ as u64)
    };
    assert!(base1 <= target, "anchor at or below the snapped target");
    assert!(target - base1 < 2 * UNIT_B, "walk-back within two PAT periods");

    // Exact splice math, phase included (collect_raw starts at the aligned
    // anchor, so the first raw PES is unambiguous).
    let (_, dpts, dpcr) = deltas(&engine);
    let want = pts_wrap(ref_pts + SPLICE_PTS_EPS);
    let raw_first = raw_first_pes_pts(&engine, base1).expect("raw anchor PES");
    assert_eq!(dpts, pts_sub(want, raw_first), "delta_pts exact (odd phase)");
    assert_eq!(dpcr, dpts * 300);

    // Delivered bytes start at the mapped position: anchor + phase offset.
    let mapped0 = base1 + (f3 % TS_PKT_SZ as u64);
    assert_eq!(first_pes_pts_any(&rb, mapped0), Some(want), "first PES == ref + 3000");

    let mut prev_pcr: Option<u64> = None;
    for (abs, pkt) in pkts(&rb, mapped0) {
        if pkt[0] != 0x47 {
            continue;
        }
        let raw = raw_pkt_at(&engine, abs);
        if let Some((pts, _)) = ts_pes_ts(pkt) {
            let (raw_pts, _) = ts_pes_ts(&raw).expect("raw PES at the same position");
            assert_eq!(pts, pts_apply(raw_pts, dpts), "PES PTS at abs={abs}");
        }
        if let Some(pcr) = ts_pcr(pkt) {
            let raw_pcr = ts_pcr(&raw).expect("raw PCR at the same position");
            assert_eq!(pcr, pcr_apply(raw_pcr, dpcr), "PCR at abs={abs}");
            if let Some(pp) = prev_pcr {
                assert!(pcr_sub(pcr, pp) > 0, "PCR forward at abs={abs}");
            }
            prev_pcr = Some(pcr);
        }
    }
    let vids = cc_list(&rb, mapped0, PID_VID);
    assert!(vids.len() > 10);
    for w in vids.windows(2) {
        assert_eq!(w[1], (w[0] + 1) & 0x0F, "video CC sequential (odd phase)");
    }
}

// ================================================================== test 3

/// SM boundary semantics: the gap and jump thresholds are strict `>`.
#[test]
fn gap_and_jump_boundaries_are_strict() {
    use fuse_ntfs::engine::state_machine::{RESUME_GAP_MS, RESUME_JUMP};
    assert_eq!(RESUME_GAP_MS, 20_000);
    assert_eq!(RESUME_JUMP, 256 * 1024);

    let s_write = 400 * MI;

    // gap == RESUME_GAP_MS is not a gap; +1 ms is.
    for (gap, expect_watch) in [(RESUME_GAP_MS, false), (RESUME_GAP_MS + 1, true)] {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let mut fend = 0u64;
        let mut last_ms = 1_000;
        for i in 0..4u64 {
            let f = 51_000_000 + i * 16_384;
            last_ms = 1_000 + i * 100;
            let c = SmCtx { anchor_valid: true, prev_fend: fend, probe_pos: None, s_write, ring_old: 0 };
            assert_eq!(sm.evaluate(f, 16_384, last_ms, &c), SmDecision::Continue);
            fend = f + 16_384;
        }
        let c = SmCtx { anchor_valid: true, prev_fend: fend, probe_pos: None, s_write, ring_old: 0 };
        assert_eq!(sm.evaluate(80_000_000, 16_384, last_ms + gap, &c), SmDecision::Continue);
        let watching = format!("{:?}", sm.phase()) == "GapWatch";
        assert_eq!(watching, expect_watch, "gap={gap}ms watch={watching}");
    }

    // jump == RESUME_JUMP is not a resume; +1 byte is.
    for (jump, expect_watch) in [(RESUME_JUMP, false), (RESUME_JUMP + 1, true)] {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let fend = 51_000_000u64;
        // Expire the opening grace with a first non-adjacent read in-window.
        let c0 = SmCtx { anchor_valid: true, prev_fend: fend, probe_pos: None, s_write, ring_old: 0 };
        assert_eq!(sm.evaluate(60_000_000, 16_384, 12_000, &c0), SmDecision::Continue);
        let foff = 60_016_384 + jump;
        let c = SmCtx { anchor_valid: true, prev_fend: 60_016_384, probe_pos: None, s_write, ring_old: 0 };
        assert_eq!(sm.evaluate(foff, 16_384, 80_000, &c), SmDecision::Continue);
        let watching = format!("{:?}", sm.phase()) == "GapWatch";
        assert_eq!(watching, expect_watch, "jump={jump} watch={watching}");
    }
}

// ================================================================== test 4

/// An interrupted resume chain drops back to Live; later adjacent reads must
/// never fabricate `ResumeAfterGap`, and a fresh gap can still resume.
#[test]
fn interrupted_debounce_never_splices() {
    let mut sm = StateMachine::new();
    sm.note_open(0);
    let s_write = 400 * MI;
    let ring_old = 0;

    // Steady playback chain.
    let mut fend = 0u64;
    for i in 0..4u64 {
        let f = 51_000_000 + i * 16_384;
        let c = SmCtx { anchor_valid: true, prev_fend: fend, probe_pos: None, s_write, ring_old };
        assert_eq!(sm.evaluate(f, 16_384, 1_000 + i * 100, &c), SmDecision::Continue);
        fend = f + 16_384;
    }

    // Step 1 of a resume (gap 60 s, jump > 256 KiB).
    let c = SmCtx { anchor_valid: true, prev_fend: fend, probe_pos: None, s_write, ring_old };
    assert_eq!(sm.evaluate(80_000_000, 16_384, 61_000, &c), SmDecision::Continue);
    assert_eq!(sm.phase(), fuse_ntfs::engine::state_machine::SmPhase::GapWatch);
    // Step 2: one chained read (chain = 2 of 4).
    let c = SmCtx { anchor_valid: true, prev_fend: 80_016_384, probe_pos: None, s_write, ring_old };
    assert_eq!(sm.evaluate(80_016_384, 16_384, 61_100, &c), SmDecision::Continue);

    // Interruption: non-adjacent read without a fresh gap resets to Live.
    let c = SmCtx { anchor_valid: true, prev_fend: 80_032_768, probe_pos: None, s_write, ring_old };
    assert_eq!(sm.evaluate(85_000_000, 16_384, 61_200, &c), SmDecision::Continue);
    assert_ne!(sm.phase(), fuse_ntfs::engine::state_machine::SmPhase::GapWatch);
    assert_eq!(sm.resumes, 0, "interrupted chain must never splice");

    // Subsequent sequential reads: no fabrication of a resume.
    let mut prev = 85_000_000u64;
    for i in 0..6u64 {
        let c = SmCtx { anchor_valid: true, prev_fend: prev, probe_pos: None, s_write, ring_old };
        assert_eq!(
            sm.evaluate(prev, 16_384, 61_300 + i * 100, &c),
            SmDecision::Continue,
            "read {i} after interruption"
        );
        prev += 16_384;
    }
    assert_eq!(sm.resumes, 0);

    // A brand-new full resume after a fresh gap still works.
    let c = SmCtx { anchor_valid: true, prev_fend: prev, probe_pos: None, s_write, ring_old };
    assert_eq!(sm.evaluate(120_000_000, 16_384, 121_400, &c), SmDecision::Continue);
    assert_eq!(sm.phase(), fuse_ntfs::engine::state_machine::SmPhase::GapWatch);
    let mut prev = 120_000_000u64;
    let mut last = SmDecision::Continue;
    for i in 1..4u64 {
        let c = SmCtx { anchor_valid: true, prev_fend: prev, probe_pos: None, s_write, ring_old };
        last = sm.evaluate(prev, 16_384, 121_500 + i * 100, &c);
        prev += 16_384;
    }
    assert_eq!(last, SmDecision::ResumeAfterGap, "a fresh chain still resumes");
    assert_eq!(sm.resumes, 1);
    assert_eq!(sm.gap_detects, 2);
}

// ================================================================== test 5

/// Washed-out Return: the bookmark was overwritten. The first two fully
/// overwritten requests stay all-NULL with the epoch and the successful cursor
/// frozen; the third consecutive request confirms the bounded recovery and
/// re-anchors to the snapped live band, calibrated from the genuine last
/// delivered state (`Served` tier). A NULL-only reply never stamps a
/// checkpoint, so no forged post-gap record can be selected.
#[test]
fn washed_out_return_falls_back_to_live_edge() {
    let engine = Arc::new(LiveEngine::new());
    engine
        .ring
        .write_chunk(&strict_stream(500_000, 56_000, true)); // ~10.04 MiB
    let mut buf = vec![0u8; BLOCK];
    let n = engine.serve_live(0, 0, &mut buf, 1000);
    assert_eq!(n, BLOCK);
    let base0 = base_spos(&engine);
    let e0 = ec(&engine);

    // Playback to 2 MiB, capturing the last delivered block (the splice
    // reference for the later fallback).
    let mut foff = BLOCK as u64;
    while foff < 2 * MI {
        let n = engine.serve_live(0, foff, &mut buf, 1000);
        assert_eq!(n, BLOCK);
        foff += BLOCK as u64;
    }
    let last_play_foff = 2 * MI - BLOCK as u64;
    let ref_pts = last_pes_pts_any(&buf, last_play_foff).expect("playback tail PES");
    let ref_vid_cc = last_cc(&buf, last_play_foff, PID_VID).expect("playback tail video CC");
    assert_eq!(engine.anchor.lock().unwrap().last_fend, 2 * MI);

    // The ring fully wraps: 752_000 more packets (~141 MiB).
    engine
        .ring
        .write_chunk(&strict_stream(80_000_000, 752_000, true));
    let sw = engine.ring.lock().s_write();
    let ring_old = engine.ring.lock().ring_old();
    assert!(ring_old > base0 + 2 * MI, "the old position is washed out");

    engine.ring.lock().shift_time_back(Duration::from_secs(7200));

    let bm: u64 = 1_900_492; // aligned
    let mut rb = vec![0u8; READ];

    let before = {
        let x = engine.xform.lock().unwrap();
        (x.ref_served, x.ref_ckpt, x.ref_scan, x.ref_none, x.epoch_id())
    };

    // Reads 1 and 2: fully overwritten region -> all-NULL, no epoch change, no
    // successful-cursor advance and no checkpoint stamp. The jump is only
    // 196_608 B (< RESUME_JUMP), so the SM never opens a GapWatch; recovery is
    // the engine's bounded overwrite tracker and needs a third request.
    // Telemetry quirk: prefix NULLs (pos < ring_old) are not counted by
    // note_null, so null_bytes stays 0.
    for (i, f) in [bm, bm + READ as u64].iter().enumerate() {
        let n = engine.serve_live(0, *f, &mut rb, 1000);
        assert_eq!(n, READ, "speculative overwritten read {i}");
        assert!(all_null(&rb), "read {i} must be all-NULL");
        assert_eq!(ec(&engine), e0, "read {i} must not re-anchor");
        assert_eq!(
            engine.anchor.lock().unwrap().last_fend,
            2 * MI,
            "read {i} must not advance the successful cursor"
        );
        assert_eq!(engine.ring.lock().telemetry().null_bytes, 0);
    }
    let mid = {
        let x = engine.xform.lock().unwrap();
        (x.ref_served, x.ref_ckpt, x.ref_scan, x.ref_none, x.epoch_id())
    };
    assert_eq!(mid, before, "no calibration on speculative NULL reads");

    // Read 3: exactly the third consecutive request recovers to the live band.
    let f3 = bm + 2 * READ as u64;
    let n = engine.serve_live(0, f3, &mut rb, 1000);
    assert_eq!(n, READ);
    assert_eq!(ec(&engine), e0 + 1, "exactly one epoch on the 3rd read");
    assert!(!all_null(&rb), "re-anchor must deliver live data");
    let base1 = base_spos(&engine);
    let target = sw - LEADBACK;
    let snapped = target - (target % TS_PKT_SZ as u64);
    assert!(base1 <= snapped && snapped - base1 < 2 * UNIT_B, "live-band anchor");
    let after = {
        let x = engine.xform.lock().unwrap();
        (x.ref_served, x.ref_ckpt, x.ref_scan, x.ref_none, x.epoch_id())
    };
    assert_eq!(after.4, e0 + 1);
    // The recovery calibrates from the genuine last delivered state (`Served`
    // tier): the NULL-only reads stamped no checkpoint, so the Ckpt tier is
    // empty (their authentic predecessors are 7200 s stale) and no forged
    // post-gap record can be selected.
    assert_eq!(after.0, before.0 + 1, "recovery calibrates from served");
    assert_eq!(after.1, before.1, "no checkpoint selected, forged or stale");
    assert_eq!(after.2, before.2, "no ring-scan tier");
    assert_eq!(after.3, before.3, "no invalid-calibration fallback");

    // Splice math is exact against the last real served state; the timeline
    // continues at ref + SPLICE_PTS_EPS instead of repeating the raw PTS.
    let (eid, dpts, dpcr) = deltas(&engine);
    assert_eq!(eid, e0 + 1);
    let want = pts_wrap(ref_pts + SPLICE_PTS_EPS);
    let raw_first = raw_first_pes_pts(&engine, base1).expect("raw live-band PES");
    assert_eq!(dpts, pts_sub(want, raw_first), "fallback delta_pts exact");
    assert_eq!(dpcr, dpts * 300, "fallback PCR tracks PTS");
    assert_eq!(first_pes_pts_any(&rb, f3), Some(want), "first PES == ref + 3000");
    assert!(pts_sub(want, ref_pts) == SPLICE_PTS_EPS as i64, "forward continuity");

    // Bit-exact per-packet transform and video CC continuation from the last
    // real playback block (the NULL block delivered no CC update).
    let mut prev_pcr: Option<u64> = None;
    for (abs, pkt) in pkts(&rb, base1) {
        if pkt[0] != 0x47 {
            continue;
        }
        let raw = raw_pkt_at(&engine, abs);
        if let Some((pts, _)) = ts_pes_ts(pkt) {
            let (raw_pts, _) = ts_pes_ts(&raw).expect("raw PES at the same position");
            assert_eq!(pts, pts_apply(raw_pts, dpts), "fallback PES PTS at abs={abs}");
        }
        if let Some(pcr) = ts_pcr(pkt) {
            let raw_pcr = ts_pcr(&raw).expect("raw PCR at the same position");
            assert_eq!(pcr, pcr_apply(raw_pcr, dpcr), "fallback PCR at abs={abs}");
            if let Some(pp) = prev_pcr {
                assert!(pcr_sub(pcr, pp) > 0, "fallback PCR forward at abs={abs}");
            }
            prev_pcr = Some(pcr);
        }
    }
    assert_eq!(
        first_cc(&rb, base1, PID_VID).expect("fallback video CC"),
        (ref_vid_cc + 1) & 0x0F,
        "video CC continues from the last real playback block"
    );
    let vids = cc_list(&rb, base1, PID_VID);
    for w in vids.windows(2) {
        assert_eq!(w[1], (w[0] + 1) & 0x0F, "fallback video CC sequential");
    }

    // Reads 4 and 5: steady live-band playback in the same epoch.
    for k in 1..3u64 {
        let f = f3 + k * READ as u64;
        let n = engine.serve_live(0, f, &mut rb, 1000);
        assert_eq!(n, READ);
        assert_eq!(ec(&engine), e0 + 1);
        assert!(!all_null(&rb));
        for (abs, pkt) in pkts(&rb, base1 + k * READ as u64) {
            if pkt[0] != 0x47 {
                continue;
            }
            let raw = raw_pkt_at(&engine, abs);
            if let Some((pts, _)) = ts_pes_ts(pkt) {
                let (raw_pts, _) = ts_pes_ts(&raw).expect("raw PES");
                assert_eq!(pts, pts_apply(raw_pts, dpts), "steady read {k} PTS at abs={abs}");
            }
            if let Some(pcr) = ts_pcr(pkt) {
                let raw_pcr = ts_pcr(&raw).expect("raw PCR");
                assert_eq!(pcr, pcr_apply(raw_pcr, dpcr), "steady read {k} PCR at abs={abs}");
            }
        }
    }
    assert_eq!(engine.ring.lock().telemetry().near_starve, 0);
}

// ================================================================== test 13

/// Return + re-enter where the player restarts the file from offset 0: even
/// with the whole playback region washed out, the read at 0 is a
/// `restart_at_zero` and must land bit-exactly on the live band
/// (`s_write - LEADBACK`) in a fresh identity epoch.
#[test]
fn cold_reopen_from_zero_after_washout_is_fresh_live() {
    let engine = Arc::new(LiveEngine::new());
    engine
        .ring
        .write_chunk(&strict_stream(500_000, 56_000, true)); // ~10.04 MiB
    let mut buf = vec![0u8; BLOCK];
    let n = engine.serve_live(0, 0, &mut buf, 1000);
    assert_eq!(n, BLOCK);
    let e0 = ec(&engine);

    // Watch up to 2 MiB, then the stream runs for ~2 h of content: the whole
    // playback region is overwritten.
    let mut foff = BLOCK as u64;
    while foff < 2 * MI {
        let n = engine.serve_live(0, foff, &mut buf, 1000);
        assert_eq!(n, BLOCK);
        foff += BLOCK as u64;
    }
    engine
        .ring
        .write_chunk(&strict_stream(80_000_000, 752_000, true));
    let sw = engine.ring.lock().s_write();
    let ring_old = engine.ring.lock().ring_old();
    assert!(ring_old > base_spos(&engine) + 2 * MI, "the old position is washed out");
    engine.ring.lock().shift_time_back(Duration::from_secs(7200));

    // Re-enter with a cold restart: the TV reads from the file start.
    let n = engine.serve_live(0, 0, &mut buf, 1000);
    assert_eq!(n, BLOCK);
    assert_eq!(ec(&engine), e0 + 1, "restart re-anchors exactly once");
    assert!(!all_null(&buf), "the live band is real data");
    let base1 = base_spos(&engine);
    let target = sw - LEADBACK;
    let snapped = target - (target % TS_PKT_SZ as u64);
    assert!(base1 <= snapped && snapped - base1 < 2 * UNIT_B, "live-band anchor");
    // `restart_at_zero` is a cold restart: fresh identity epoch, so the
    // delivered bytes are bit-exact ring bytes at the new anchor.
    let mut raw = vec![0u8; BLOCK];
    engine.ring.lock().read_at(base1, &mut raw);
    assert_eq!(buf, raw, "cold restart delivers raw bytes bit-exactly");
    assert_eq!(engine.ring.lock().telemetry().near_starve, 0);
}

// ================================================================== test 14

/// Return + re-enter where the player resumes at its saved bookmark (>= 8 MiB)
/// after an idle gap. Expected, by parity with the sub-8-MiB case (test 5):
/// the washed-out head is NULL-filled while the chain cursor advances, so the
/// next sequential read becomes adjacent and `seq_behind_ring` re-anchors to
/// the live band in one splice.
///
/// KNOWN BUG: for `foff >= PROBE_ISOLATION_FOFF` the probe rejection in
/// `StateMachine::evaluate` runs *before* the 2-step resume, so the bookmark
/// read returns `ProbeReject` without advancing the chain cursor: the next
/// read is never adjacent again and the TV gets an endless NULL soup. The old
/// workaround (4th behind-probe escalating to a USB recycle) was removed by
/// TASK-005 because it was destructive; the in-band resume remains the correct
/// fix and is tracked by TASK-008.
#[test]
#[ignore = "KNOWN BUG: >=8 MiB washed-out bookmark resume is stuck in probe isolation and never reaches the live band"]
fn known_bug_resume_at_large_bookmark_never_returns_to_live() {
    let engine = Arc::new(LiveEngine::new());
    engine
        .ring
        .write_chunk(&strict_stream(500_000, 64_000, true)); // ~12.03 MiB
    let mut buf = vec![0u8; BLOCK];
    let n = engine.serve_live(0, 0, &mut buf, 1000);
    assert_eq!(n, BLOCK);
    let e0 = ec(&engine);

    // Watch up to 9 MiB: a realistic bookmark above the 8 MiB probe floor.
    // (The cold open parks the anchor ~2 MiB behind the writer head, so the
    // initial stream must be long enough that file offset 9 MiB still maps
    // inside the written region.)
    let mut foff = BLOCK as u64;
    while foff < 9 * MI {
        let n = engine.serve_live(0, foff, &mut buf, 1000);
        assert_eq!(n, BLOCK);
        foff += BLOCK as u64;
    }
    let last_play_foff = 9 * MI - BLOCK as u64;
    let ref_pts = last_pes_pts_any(&buf, last_play_foff).expect("playback tail PES");

    // The stream runs ~2 h; the whole playback region is washed out.
    engine
        .ring
        .write_chunk(&strict_stream(80_000_000, 752_000, true));
    let sw = engine.ring.lock().s_write();
    assert!(
        base_spos(&engine) + 8 * MI < engine.ring.lock().ring_old(),
        "the bookmark maps below ring_old"
    );
    engine.ring.lock().shift_time_back(Duration::from_secs(7200));

    // The TV resumes at its saved bookmark: >= 8 MiB, and ~1 MiB behind the
    // old read head (the read-ahead gap a real player leaves between its
    // playback position and the last byte it asked for).
    let p = 8 * MI + UNIT_B;
    let n = engine.serve_live(0, p, &mut buf, 1000);
    assert_eq!(n, BLOCK);
    assert!(all_null(&buf), "washed-out head is NULL-filled");
    assert_eq!(ec(&engine), e0, "no splice on the washed-out head");
    assert_eq!(
        engine.anchor.lock().unwrap().last_fend,
        p + BLOCK as u64,
        "the chain cursor must advance so the next read is adjacent (test-5 parity)"
    );

    // Next sequential read: `seq_behind_ring` must re-anchor to the live band.
    let n = engine.serve_live(0, p + BLOCK as u64, &mut buf, 1000);
    assert_eq!(n, BLOCK);
    assert_eq!(ec(&engine), e0 + 1, "exactly one re-anchor");
    assert!(!all_null(&buf), "live band delivers real data");
    let base1 = base_spos(&engine);
    let target = sw - LEADBACK;
    let snapped = target - (target % TS_PKT_SZ as u64);
    assert!(base1 <= snapped && snapped - base1 < 2 * UNIT_B, "live-band anchor");
    let want = pts_wrap(ref_pts + SPLICE_PTS_EPS);
    assert_eq!(
        first_pes_pts_any(&buf, p + BLOCK as u64),
        Some(want),
        "resume continues at ref + 3000"
    );
}

// ================================================================== test 15

/// The production incident, reproduced: the TV played to 36 MiB (`fend`), the
/// user pressed Return (idle gap; the stream advanced enough to wash the whole
/// playback region out of the ring), and the TV re-entered the file reading
/// its *stale* saved bookmark - 2_195_456, the exact offset seen on the
/// tablet - a backward jump of ~34 MiB into overwritten bytes.
///
/// The naive guard (`restart_at_zero`, `foff == 0`) missed this because the TV
/// never asks byte 0: it asks its bookmark. The fix treats
/// `large_backward_jump && is_washed_out` as a cold restart: the anchor is
/// planted exactly at the requested offset (`base_foff = snap188(foff)`), the
/// read is aligned on the fresh live edge at a PAT with the SPS immediately
/// behind it, and the epoch is reset to identity - so the very first read
/// delivers decodable live data instead of the NULL head that loses PAT/SPS
/// and freezes the MStar on the first frame.
#[test]
fn stale_bookmark_after_return_cold_reanchors_fresh_live() {
    let engine = Arc::new(LiveEngine::new());
    engine
        .ring
        .write_chunk(&strict_stream(500_000, 56_000, true)); // ~10.04 MiB
    let mut buf = vec![0u8; BLOCK];
    let n = engine.serve_live(0, 0, &mut buf, 1000);
    assert_eq!(n, BLOCK);
    let base0 = base_spos(&engine);
    assert_eq!(base0 % TS_PKT_SZ as u64, 0, "anchor is 188-aligned");
    let e0 = ec(&engine);

    // The TV plays to 36 MiB. The anchor parks ~2 MiB behind the writer head,
    // so the stream must be extended first for foff 36 MiB to map inside the
    // written region (the same growth as production, where the writer had
    // advanced to hundreds of MiB).
    engine
        .ring
        .write_chunk(&strict_stream(20_000_000, 170_000, true)); // ~30.5 MiB
    let mut foff = BLOCK as u64; // no re-read of 0 (the SM would see a seek)
    while foff < 36 * MI {
        let n = engine.serve_live(0, foff, &mut buf, 1000);
        assert_eq!(n, BLOCK, "playback read at {foff}");
        assert!(!all_null(&buf), "playback is in-window all along");
        foff += BLOCK as u64;
    }
    assert_eq!(engine.anchor.lock().unwrap().last_fend, 36 * MI);
    assert_eq!(ec(&engine), e0, "playback never bumps the epoch");

    // Return + absence: the stream advances ~100 MiB, washing out the whole
    // playback region (in production the ring had also already dropped the
    // old position when the TV re-opened the file).
    engine
        .ring
        .write_chunk(&strict_stream(80_000_000, 560_000, true)); // ~100 MiB
    let sw = engine.ring.lock().s_write();
    let ring_old = engine.ring.lock().ring_old();
    let bm: u64 = 2_195_456; // the tablet's bookmark, exactly
    assert!(ring_old > base0 + bm, "the bookmark is washed out");
    assert!(
        36 * MI - bm > 32 * MI,
        "the jump qualifies as large_backward_jump"
    );
    engine.ring.lock().shift_time_back(Duration::from_secs(7200));

    // Read 1 - the re-open. Must be live data in the first block.
    let mut rb = vec![0u8; READ];
    let n = engine.serve_live(0, bm, &mut rb, 1000);
    assert_eq!(n, READ);
    assert!(
        !all_null(&rb),
        "the first read after Return must be live data, never the NULL soup"
    );
    assert_eq!(ec(&engine), e0 + 1, "cold re-anchor fires exactly once");
    let base1 = base_spos(&engine);
    assert_eq!(base1 % TS_PKT_SZ as u64, 0, "live anchor is 188-aligned");
    let target = sw - LEADBACK;
    let snapped = target - (target % TS_PKT_SZ as u64);
    assert!(base1 <= snapped && snapped - base1 < 2 * UNIT_B, "live-band anchor");

    // The anchor is planted exactly at the requested offset, snapped to the
    // packet grid, and the fresh identity epoch delivers raw ring bytes.
    let phase = bm % TS_PKT_SZ as u64;
    assert_eq!(
        engine.anchor.lock().unwrap().base_foff,
        bm - phase,
        "base_foff == snap188(bookmark)"
    );
    assert_eq!(deltas(&engine), (e0 + 1, 0, 0), "identity epoch (no shift)");
    let mut raw = vec![0u8; READ];
    engine.ring.lock().read_at(base1 + phase, &mut raw);
    assert_eq!(rb, raw, "fresh live data is bit-exact ring bytes");
    assert_eq!(engine.anchor.lock().unwrap().last_fend, bm + READ as u64);

    // The live edge lands on a PAT with the SPS immediately behind it: the
    // first full packet the decoder syncs on is the SPS (the PAT is clipped
    // by the 180-byte phase, its 8-byte tail still leads the block).
    assert_eq!(ts_pid(&raw_pkt_at(&engine, base1)), PID_PAT, "anchor on a PAT");
    assert!(
        ts_has_sps(&raw_pkt_at(&engine, base1 + TS_PKT_SZ as u64)),
        "SPS immediately after the anchor PAT"
    );
    let (abs0, p0) = *pkts(&rb, base1 + phase).first().expect("full packets");
    assert_eq!(abs0, base1 + TS_PKT_SZ as u64, "first full packet is the SPS");
    assert!(ts_has_sps(p0), "first full packet carries the SPS");

    // Read 2 - the chain continues live: no second splice, still bit-exact.
    let n = engine.serve_live(0, bm + READ as u64, &mut rb, 1000);
    assert_eq!(n, READ);
    assert!(!all_null(&rb));
    assert_eq!(ec(&engine), e0 + 1, "no second re-anchor");
    let mut raw = vec![0u8; READ];
    engine.ring.lock().read_at(base1 + phase + READ as u64, &mut raw);
    assert_eq!(rb, raw, "second block is bit-exact too");
    assert_eq!(engine.ring.lock().telemetry().near_starve, 0);
}

// ================================================================== test 16

/// The same production incident, but the TV's bookmark is above the 8 MiB
/// probe floor (40 MiB) with the same class of large backward jump (~34 MiB).
///
/// Expected, by parity with test 15: a washed-out backward jump is a cold
/// restart - the first read delivers the live band (PAT+SPS) in a fresh
/// identity epoch instead of NULL packets.
///
/// KNOWN BUG: in `StateMachine::evaluate` the probe classification runs before
/// the engine's restart decision, and `SmDecision::ProbeReject` returns early
/// inside the probe arm of `serve_live`, so `is_restart` never reaches the
/// re-anchor: the first read is all-NULL and the chain cursor freezes - the
/// MStar loses PAT/SPS exactly as in the incident this fix was written for.
/// The minimal follow-up is to let the restart predicate bypass probe
/// rejection (skip `ProbeReject` when `large_backward_jump && is_washed_out`).
#[test]
#[ignore = "KNOWN BUG: probe rejection preempts the cold re-anchor for washed-out bookmarks >= 8 MiB"]
fn known_bug_large_bookmark_backward_jump_probe_preempts_cold_reanchor() {
    let engine = Arc::new(LiveEngine::new());
    engine
        .ring
        .write_chunk(&strict_stream(500_000, 56_000, true)); // ~10.04 MiB
    let mut buf = vec![0u8; BLOCK];
    let n = engine.serve_live(0, 0, &mut buf, 1000);
    assert_eq!(n, BLOCK);
    let base0 = base_spos(&engine);
    let e0 = ec(&engine);

    // Play to 74 MiB so a stale 40 MiB bookmark is > 32 MiB behind `fend`.
    engine
        .ring
        .write_chunk(&strict_stream(20_000_000, 390_000, true)); // ~70 MiB
    let mut foff = BLOCK as u64;
    while foff < 74 * MI {
        let n = engine.serve_live(0, foff, &mut buf, 1000);
        assert_eq!(n, BLOCK, "playback read at {foff}");
        assert!(!all_null(&buf), "playback is in-window all along");
        foff += BLOCK as u64;
    }
    assert_eq!(engine.anchor.lock().unwrap().last_fend, 74 * MI);
    assert_eq!(ec(&engine), e0, "playback never bumps the epoch");

    engine
        .ring
        .write_chunk(&strict_stream(80_000_000, 560_000, true)); // ~100 MiB
    let sw = engine.ring.lock().s_write();
    let ring_old = engine.ring.lock().ring_old();
    let bm: u64 = 40 * MI;
    assert!(ring_old > base0 + bm, "the bookmark is washed out");
    assert!(74 * MI - bm > 32 * MI, "large backward jump");
    engine.ring.lock().shift_time_back(Duration::from_secs(7200));

    // Read 1: desired = cold re-anchor onto the live band, exactly like test 15.
    let mut rb = vec![0u8; READ];
    let n = engine.serve_live(0, bm, &mut rb, 1000);
    assert_eq!(n, READ);
    assert!(
        !all_null(&rb),
        "the first read after Return must be live data (test-15 parity)"
    );
    assert_eq!(ec(&engine), e0 + 1, "cold re-anchor fires exactly once");
    let base1 = base_spos(&engine);
    let target = sw - LEADBACK;
    let snapped = target - (target % TS_PKT_SZ as u64);
    assert!(base1 <= snapped && snapped - base1 < 2 * UNIT_B, "live-band anchor");
    let phase = bm % TS_PKT_SZ as u64;
    assert_eq!(engine.anchor.lock().unwrap().base_foff, bm - phase);
    assert_eq!(deltas(&engine), (e0 + 1, 0, 0), "identity epoch (no shift)");
    let mut raw = vec![0u8; READ];
    engine.ring.lock().read_at(base1 + phase, &mut raw);
    assert_eq!(rb, raw, "fresh live data is bit-exact ring bytes");
}

// ================================================================== test 6

/// Probe isolation: reads outside the ring window NULL-fill without touching
/// the chain; behind-probes remain non-destructive NULL replies (TASK-005);
/// the chain then resumes exactly where it stopped.
#[test]
fn probe_isolation_future_and_behind() {
    use fuse_ntfs::engine::state_machine::SmPhase;
    let engine = Arc::new(LiveEngine::new());
    engine
        .ring
        .write_chunk(&strict_stream(1_000_000, 112_000, true)); // ~21 MiB
    let mut buf = vec![0u8; BLOCK];
    let n = engine.serve_live(0, 0, &mut buf, 1000);
    assert_eq!(n, BLOCK);
    let base0 = base_spos(&engine);
    let e0 = ec(&engine);
    play_to(&engine, 4 * MI);
    let fend_before = engine.anchor.lock().unwrap().last_fend;
    let sw = engine.ring.lock().s_write();

    // ---- future probes: mapped >= s_write + 2 MiB, foff >= 8 MiB.
    let future_foff = 24 * MI;
    assert!(future_foff >= 8 * MI);
    assert!(base0 + future_foff >= sw + 2 * MI, "probe must be classified as future");
    let rejects_before = engine.sm.lock().unwrap().probe_rejects;
    let nulls_before = engine.ring.lock().telemetry().null_bytes;
    for i in 0..3u64 {
        let f = future_foff + i * 64 * 1024;
        let n = engine.serve_live(0, f, &mut buf, 1000);
        assert_eq!(n, BLOCK);
        let rejects_now = engine.sm.lock().unwrap().probe_rejects;
        assert!(
            all_null(&buf),
            "future probe {i} must be NULL-filled: head={:02x?} rejects={rejects_now}",
            &buf[..8]
        );
        assert_eq!(ec(&engine), e0, "future probe {i} never touches the epoch");
        assert_eq!(base_spos(&engine), base0, "future probe {i} never re-anchors");
        assert_eq!(
            engine.anchor.lock().unwrap().last_fend,
            fend_before,
            "future probe {i} never advances the chain"
        );
    }
    assert_eq!(engine.sm.lock().unwrap().probe_rejects, rejects_before + 3);
    assert_eq!(engine.sm.lock().unwrap().phase(), SmPhase::Live);
    assert_eq!(
        engine.ring.lock().telemetry().null_bytes,
        nulls_before + 3 * BLOCK as u64,
        "probe NULLs are counted by note_null"
    );

    // The untouched chain resumes at the exact next position, identity-exact.
    let n = engine.serve_live(0, fend_before, &mut buf, 1000);
    assert_eq!(n, BLOCK);
    assert!(!all_null(&buf));
    assert_eq!(ec(&engine), e0);
    let mut raw = vec![0u8; BLOCK];
    engine.ring.lock().read_at(base0 + fend_before, &mut raw);
    assert_eq!(buf, raw, "chain resumes byte-exact after future probes");

    // ---- behind probes: force ring_old past the probe mapping.
    engine
        .ring
        .write_chunk(&strict_stream(200_000_000, 800_000, true)); // +150.4 MB
    let ring_old = engine.ring.lock().ring_old();
    let behind_foff = 8 * MI + TS_PKT_SZ as u64;
    assert!(behind_foff >= 8 * MI);
    assert!(
        base0 + behind_foff < ring_old,
        "probe must map behind ring_old (mapped {}, ring_old {ring_old})",
        base0 + behind_foff
    );
    let fend_after_chain = engine.anchor.lock().unwrap().last_fend;
    for i in 0..4u64 {
        let f = behind_foff + i * BLOCK as u64;
        let n = engine.serve_live(0, f, &mut buf, 1000);
        assert_eq!(n, BLOCK, "behind probe {i}");
        assert!(all_null(&buf), "behind probe {i} must be NULL-filled");
        assert_eq!(ec(&engine), e0, "behind probe {i} never touches the epoch");
        assert_eq!(
            engine.anchor.lock().unwrap().last_fend,
            fend_after_chain,
            "behind probe {i} never advances the chain"
        );
    }
    assert_eq!(
        engine.sm.lock().unwrap().probe_rejects,
        rejects_before + 7,
        "3 future + 4 behind probes rejected"
    );
    // TASK-005: no escalation and no marker. Every behind probe is the same
    // bounded NULL-only rejection; the chain stays untouched.
    let n = engine.serve_live(0, behind_foff + 5 * BLOCK as u64, &mut buf, 1000);
    assert_eq!(n, BLOCK);
    assert!(all_null(&buf));
    assert_eq!(engine.sm.lock().unwrap().probe_rejects, rejects_before + 8);
    assert_eq!(engine.ring.lock().telemetry().near_starve, 0);
}

// ================================================================== test 7

/// Channel switch hardening: only a >= 1 MiB *contiguous* read run on the
/// inactive channel flips it; each flip bumps the epoch exactly once and the
/// engine follows the file the TV is actually reading.
#[test]
fn channel_switch_needs_contiguous_run() {
    assert_eq!(SWITCH_THRESHOLD % BLOCK as u64, 0, "16 reads must be exact");

    let template = NtfsTemplate::synthesized();
    let live_size = template.layout.data_size;
    let catalog = Catalog::new(live_size);
    let engine = Arc::new(LiveEngine::new());
    let server = NtfsServer::new(template, catalog, engine.clone());

    engine
        .ring
        .write_chunk(&strict_stream(10_000_000, 112_000, true)); // ~21 MiB
    let mut buf = vec![0u8; BLOCK];

    // Channel 0 playback: 2 MiB.
    let ch0 = LIVE_INO_BASE;
    let ch1 = LIVE_INO_BASE + 1;
    for i in 0..32u64 {
        let n = server.serve_read(ch0, i * BLOCK as u64, &mut buf);
        assert_eq!(n, BLOCK);
        assert!(!all_null(&buf));
    }
    assert_eq!(server.catalog.active_file_id(), 0);
    let e0 = ec(&engine);
    let switch_start = Instant::now();

    // First probe on channel 1 (offset 0): the engine follows the file the TV
    // reads and re-anchors to the live edge with a fresh identity epoch.
    let n = server.serve_read(ch1, 0, &mut buf);
    assert_eq!(n, BLOCK);
    assert_eq!(server.catalog.active_file_id(), 0, "single probe must not switch");
    assert_eq!(ec(&engine), e0 + 1, "first read of the new file re-anchors once");
    assert_eq!(engine.anchor.lock().unwrap().file_id, 1);
    let base1 = base_spos(&engine);
    {
        // Identity delivery right after a channel re-anchor.
        let mut raw = vec![0u8; BLOCK];
        engine.ring.lock().read_at(base1, &mut raw);
        assert_eq!(buf, raw, "channel re-anchor delivers raw bytes");
    }

    // Remaining non-contiguous probes must not switch the catalog. Offsets are
    // deliberately non-monotonic so the in-window seek detector never sees 4
    // sequential jumps either (offsets are all > 0: a read at foff = 0 after a
    // big jump is a *restart* and legitimately re-anchors).
    for &off in &[5 * MI, 1 * MI, 7 * MI, 2 * MI, 9 * MI, 3 * MI, 6 * MI] {
        let n = server.serve_read(ch1, off, &mut buf);
        assert_eq!(n, BLOCK);
        assert_eq!(server.catalog.active_file_id(), 0, "probe at {off} must not switch");
    }
    assert_eq!(ec(&engine), e0 + 1, "probes never bump the epoch");

    // Contiguous run broken before the threshold: the accumulator resets.
    let run0 = 1 * MI;
    for i in 0..15u64 {
        let n = server.serve_read(ch1, run0 + i * BLOCK as u64, &mut buf);
        assert_eq!(n, BLOCK);
    }
    assert_eq!(server.catalog.active_file_id(), 0, "15 x 64 KiB < 1 MiB");
    let n = server.serve_read(ch1, 12 * MI, &mut buf);
    assert_eq!(n, BLOCK);
    assert_eq!(server.catalog.active_file_id(), 0, "non-contiguous read resets the run");

    // Clean contiguous run: the switch fires exactly on the 16th read.
    for i in 0..15u64 {
        let n = server.serve_read(ch1, run0 + i * BLOCK as u64, &mut buf);
        assert_eq!(n, BLOCK);
        assert!(!all_null(&buf));
        assert_eq!(server.catalog.active_file_id(), 0, "must not switch before 1 MiB (read {i})");
    }
    let n = server.serve_read(ch1, run0 + 15 * BLOCK as u64, &mut buf);
    assert_eq!(n, BLOCK);
    assert_eq!(server.catalog.active_file_id(), 1, "switch fires on the 16th read");
    assert_eq!(
        server.catalog.active_node().expect("active node").name,
        "TV AO VIVO 2.tp"
    );
    assert_eq!(ec(&engine), e0 + 1, "no extra epoch on the threshold read");

    // Switch back to channel 0: same contract, one more epoch on the first
    // read of the other file.
    for i in 0..15u64 {
        let n = server.serve_read(ch0, run0 + i * BLOCK as u64, &mut buf);
        assert_eq!(n, BLOCK);
        assert_eq!(server.catalog.active_file_id(), 1, "still channel 1 (read {i})");
    }
    let n = server.serve_read(ch0, run0 + 15 * BLOCK as u64, &mut buf);
    assert_eq!(n, BLOCK);
    assert_eq!(server.catalog.active_file_id(), 0, "switch back on the 16th read");
    assert_eq!(ec(&engine), e0 + 2, "switch back bumps the epoch once");
    assert_eq!(engine.anchor.lock().unwrap().file_id, 0);

    let elapsed = switch_start.elapsed();
    assert!(elapsed < Duration::from_secs(10), "switch sequence took {elapsed:?}");
}

// ================================================================== test 8

/// 33-bit PTS rollover: playback passes straight through the wrap on the
/// circle, and a splice after the wrap continues at `ref + 3000`.
#[test]
fn pts_wrap_playback_and_splice() {
    // Wrap lands ~10 MiB into the stream, inside the cold-open window.
    let base_pts = PTS_MOD - 55_775_000;
    let engine = Arc::new(LiveEngine::new());
    engine
        .ring
        .write_chunk(&strict_stream(base_pts, 112_000, true));
    let mut buf = vec![0u8; BLOCK];
    let n = engine.serve_live(0, 0, &mut buf, 1000);
    assert_eq!(n, BLOCK);
    let base0 = base_spos(&engine);

    // Sweep [base0, base0 + 12 MiB] in unaligned 64 KiB reads so straddling
    // packets cross the wrap under the tail carry.
    let mut prev: Option<u64> = None;
    let mut prev_pcr: Option<u64> = None;
    let mut observed_wrap = false;
    let end_foff = 12 * MI;
    let mut foff = BLOCK as u64;
    while foff < end_foff {
        let n = engine.serve_live(0, foff, &mut buf, 1000);
        assert_eq!(n, BLOCK);
        assert!(!all_null(&buf));
        for (abs, pkt) in pkts(&buf, base0 + foff) {
            if pkt[0] != 0x47 {
                continue;
            }
            let raw = raw_pkt_at(&engine, abs);
            if let Some((pts, _)) = ts_pes_ts(pkt) {
                let (raw_pts, _) = ts_pes_ts(&raw).expect("raw PES");
                assert_eq!(pts, raw_pts, "identity PTS bytes survive the wrap");
                if let Some(p) = prev {
                    let d = pts_sub(pts, p);
                    assert!(d > 0, "PTS must move forward on the circle: {p} -> {pts}");
                    if pts < p {
                        observed_wrap = true;
                    }
                }
                prev = Some(pts);
            }
            if let Some(pcr) = ts_pcr(pkt) {
                let raw_pcr = ts_pcr(&raw).expect("raw PCR");
                assert_eq!(pcr, raw_pcr, "identity PCR bytes survive the wrap");
                if let Some(p) = prev_pcr {
                    assert!(pcr_sub(pcr, p) > 0, "PCR forward on the circle");
                }
                prev_pcr = Some(pcr);
            }
        }
        foff += BLOCK as u64;
    }
    assert!(observed_wrap, "the sweep must actually cross 2^33");

    // Splice whose reference sits just below the wrap and whose raw anchor
    // window crosses it: every quantity is circular.
    use fuse_ntfs::engine::ts_xform::{RawSample, XformRef};
    let mut r = XformRef::invalid();
    r.valid = true;
    r.pts = PTS_MOD - 3_000; // decoder at ref, SPLICE_PTS_EPS below the wrap
    r.pcr_valid = true;
    r.pcr = (PTS_MOD - 3_000) * 300;
    r.ccs[0] = (PID_VID, 5);
    r.ncc = 1;
    let mut raw = RawSample::empty();
    raw.pts = Some(PTS_MOD - 2_000); // raw anchor 1000 ticks after the ref
    raw.pcr = Some((PTS_MOD - 2_000) * 300);
    raw.ccs[0] = (PID_VID, 9);
    raw.ncc = 1;
    let mut x = fuse_ntfs::engine::ts_xform::TsXformState::new();
    assert!(x.begin_splice_epoch(7, &r, &raw));
    let (_, dpts, dpcr) = x.deltas();
    assert_eq!(dpts, pts_sub(pts_wrap(r.pts + SPLICE_PTS_EPS), PTS_MOD - 2_000));
    assert_eq!(dpcr, dpts * 300);
    // Raw packets ascending on the circle through the wrap: 2^33 - 2000 ..
    // .. 500. The delivered timeline must stay forward with uniform steps.
    let raw_pts = [
        PTS_MOD - 2_000,
        PTS_MOD - 1_500,
        PTS_MOD - 1_000,
        PTS_MOD - 500,
        0,
        500,
    ];
    let mut blk = Vec::new();
    for (i, p) in raw_pts.iter().enumerate() {
        let mut pkt = [0xFFu8; TS_PKT_SZ];
        pkt[0] = 0x47;
        pkt[1] = 0x41;
        pkt[2] = 0x00;
        pkt[3] = 0x10 | (i as u8 & 0x0F);
        pkt[4] = 0x00;
        pkt[5] = 0x00;
        pkt[6] = 0x01;
        pkt[7] = 0xE0;
        pkt[10] = 0x80;
        pkt[11] = 0x80;
        pkt[12] = 0x05;
        let mut b = [0u8; 5];
        pts_to_bytes(*p, &mut b);
        pkt[13..18].copy_from_slice(&b);
        blk.extend_from_slice(&pkt);
    }
    let src = blk.clone();
    x.apply_block(&mut blk, 0, false, |abs, pkt| {
        let start = abs as usize;
        if start + TS_PKT_SZ <= src.len() {
            pkt.copy_from_slice(&src[start..start + TS_PKT_SZ]);
            true
        } else {
            false
        }
    });
    // Re-parse delivered values: pts_apply on the circle, no repeated values.
    let mut got = Vec::new();
    for (_, pkt) in pkts(&blk, 0) {
        if let Some((pts, _)) = ts_pes_ts(pkt) {
            got.push(pts);
        }
    }
    assert_eq!(got.len(), 6);
    for (i, p) in raw_pts.iter().enumerate() {
        assert_eq!(got[i], pts_apply(*p, dpts), "splice-across-wrap value {i}");
    }
    for w in got.windows(2) {
        assert!(pts_sub(w[1], w[0]) > 0, "delivered PTS forward across the wrap");
    }
    assert_eq!(
        got[0],
        pts_wrap(r.pts + SPLICE_PTS_EPS),
        "continuity target is ref + 3000 across the wrap"
    );
    assert_eq!(
        pts_sub(got[0], r.pts),
        SPLICE_PTS_EPS as i64,
        "exactly +3000 from ref to the first delivered PES"
    );
    assert!(got[0] < r.pts, "the delivered timeline crossed 2^33 numerically");
}

// ================================================================== test 9

/// Live Edge Lock (AGENTS.md §2.2): a read at the writer head blocks on the
/// condvar, then delivers the feeder bytes with no starve and no NULL fill.
#[test]
fn live_edge_lock_waits_and_delivers() {
    let engine = Arc::new(LiveEngine::new());
    engine
        .ring
        .write_chunk(&strict_stream(1_000_000, 56_000, true)); // ~10.5 MiB
    let mut buf = vec![0u8; BLOCK];
    let n = engine.serve_live(0, 0, &mut buf, 1000);
    assert_eq!(n, BLOCK);
    let base0 = base_spos(&engine);
    let e0 = ec(&engine);
    let sw = engine.ring.lock().s_write();
    assert_eq!(sw % TS_PKT_SZ as u64, 0);

    // The feeder arrives 150 ms later with exactly a read block worth of
    // stream (349 packets cover the 64 KiB read plus its straddling tail).
    let feed = strict_stream(90_000_000, (BLOCK / TS_PKT_SZ) + 1, true);
    let foff = sw - base0;
    let feeder = {
        let engine = engine.clone();
        let feed = feed.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(150));
            engine.ring.write_chunk(&feed);
        })
    };
    let t = Instant::now();
    let n = engine.serve_live(0, foff, &mut buf, 1000);
    let waited = t.elapsed();
    feeder.join().expect("feeder thread");

    assert_eq!(n, BLOCK);
    assert_eq!(buf, feed[..BLOCK], "live-edge data must be delivered exactly");
    assert!(waited >= Duration::from_millis(100), "read must have waited for the feeder ({waited:?})");
    assert!(waited < Duration::from_millis(500), "edge budget exceeded ({waited:?})");
    let tel = engine.ring.lock().telemetry();
    assert!(tel.edge_waits >= 1, "a cooperative edge wait was recorded");
    assert_eq!(tel.near_starve, 0);
    assert_eq!(tel.null_bytes, 0, "no NULL fill on the edge path");
    assert_eq!(ec(&engine), e0, "edge wait is not a re-anchor");
    assert_eq!(
        engine.anchor.lock().unwrap().last_fend,
        foff + BLOCK as u64,
        "chain advances normally after the edge wait"
    );
}

// ================================================================== test 10

/// Regression guard for the tier-1 PAT+SPS scan: the SPS-less 24 MiB stream
/// is the worst case (every PAT scans its full 1 MiB lookahead). Pre-fix this
/// was quadratic (~7.6 s in debug); a linear scan is milliseconds.
#[test]
fn d12_pat_sps_scan_is_linear() {
    let engine = Arc::new(LiveEngine::new());
    engine
        .ring
        .write_chunk(&strict_stream(1_000_000, 112_000, false));
    let mut buf = vec![0u8; BLOCK];
    let t = Instant::now();
    let n = engine.serve_live(0, 0, &mut buf, 1000);
    let dt = t.elapsed();
    assert_eq!(n, BLOCK);
    assert!(!all_null(&buf));
    assert_eq!(ec(&engine), 1);
    assert!(
        dt < Duration::from_secs(2),
        "cold re-anchor took {dt:?}; the PAT/SPS scan regressed to quadratic"
    );
}

// ============================================ raw-sample & CC coverage guards

/// `TsXformState::collect_raw` must sample every reference PID - PAT, video
/// and audio in production order, not stop after two - so `begin_splice_epoch`
/// can continue the CC of every PID across a splice: the first audio packet
/// after a splice continues `last + 1 (mod 16)` instead of repeating a stale
/// CC. (Regression guard for the pre-fix `ncc >= 2` scan cut-off.)
#[test]
fn collect_raw_pid_coverage() {
    use fuse_ntfs::engine::ts_xform::{RawSample, TsXformState, XformRef};

    let mut refr = XformRef::invalid();
    refr.valid = true;
    refr.pts = 5_000_000;
    refr.pcr_valid = true;
    refr.pcr = 5_000_000 * 300;
    refr.ccs[0] = (PID_PAT, 1);
    refr.ccs[1] = (PID_VID, 4);
    refr.ccs[2] = (PID_AUD, 7);
    refr.ncc = 3;

    // Window in production order: PAT, VIDEO (+PCR+PTS), AUDIO.
    let mut win = Vec::new();
    let mut pat = [0xFFu8; TS_PKT_SZ];
    pat[0] = 0x47;
    pat[1] = 0x40;
    pat[2] = 0x00;
    pat[3] = 0x12; // CC 2 (next after 1)
    win.extend_from_slice(&pat);
    let mut vid = [0xFFu8; TS_PKT_SZ];
    vid[0] = 0x47;
    vid[1] = 0x41;
    vid[2] = 0x00;
    vid[3] = 0x30 | 9;
    vid[4] = 0x07;
    vid[5] = 0x10;
    ts_write_pcr(3_000_000 * 300, &mut vid);
    vid[12] = 0x00;
    vid[13] = 0x00;
    vid[14] = 0x01;
    vid[15] = 0xE0;
    vid[18] = 0x80;
    vid[19] = 0x80;
    vid[20] = 0x05;
    let mut b = [0u8; 5];
    pts_to_bytes(3_000_000, &mut b);
    vid[21..26].copy_from_slice(&b);
    win.extend_from_slice(&vid);
    let mut aud = [0xFFu8; TS_PKT_SZ];
    aud[0] = 0x47;
    aud[1] = 0x41;
    aud[2] = 0x01;
    aud[3] = 0x13; // CC 3 (next after 2)
    win.extend_from_slice(&aud);

    let raw = TsXformState::collect_raw(&win, 0, 0, win.len() as u64);
    assert_eq!(raw.ncc, 3, "collect_raw must sample all three reference PIDs");
    assert!(
        raw.ccs.iter().take(raw.ncc).any(|s| s.0 == PID_AUD),
        "audio PID must be sampled"
    );

    let mut x = TsXformState::new();
    x.begin_splice_epoch(1, &refr, &raw);
    let mut blk = aud;
    x.apply_block(&mut blk, 0, false, |_, _| true);
    assert_eq!(ts_cc(&blk), 8, "audio CC must continue 7 -> 8 across the splice");
    let _ = RawSample::empty();
}

/// Verifies that at the position-preserving resume seam, the first audio packet
/// continues `last + 1 (mod 16)`.
#[test]
fn seam_audio_cc_contract() {
    let engine = Arc::new(LiveEngine::new());
    let (_base0, _e0, _sw) = return_fixture(&engine);
    let bm: u64 = 1_900_492;
    let mut rb = vec![0u8; READ];
    for i in 0..3u64 {
        let n = engine.serve_live(0, bm + i * READ as u64, &mut rb, 1000);
        assert_eq!(n, READ);
    }
    let last_aud = last_cc(&rb, bm + 2 * READ as u64, PID_AUD).expect("pre-seam audio CC");
    let n = engine.serve_live(0, bm + 3 * READ as u64, &mut rb, 1000);
    assert_eq!(n, READ);
    let first_aud = first_cc(&rb, bm + 3 * READ as u64, PID_AUD).expect("seam audio CC");
    assert_eq!(
        first_aud,
        (last_aud + 1) & 0x0F,
        "first audio CC after the seam must continue {last_aud} -> {}",
        (last_aud + 1) & 0x0F
    );
}
