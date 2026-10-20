//! TASK-002 no-data-progress coverage: starved/speculative replies must never
//! forge successful progress, and the new bounded wholly-overwritten recovery
//! must confirm only on genuinely consecutive eligible requests while still
//! referencing a real delivered position.
//!
//! Scenarios (see `docs/agent_tasks/TASK-002.md`, requirement 4/5):
//! 1. `producer_restart_does_not_forge_progress` — after a feeder restart the
//!    reader keeps requesting its old cursor: replies past the new head are
//!    explicitly NULL (starved read and future probe) and the successful
//!    cursor/epoch/calibration counters stay frozen; real progress resumes
//!    from the new resident head.
//! 2. `real_bytes_plus_null_suffix_reports_explicit_nulls` — a reply with real
//!    resident bytes followed by a NULL suffix reports the unsupported tail
//!    (`null_bytes`) and keeps the inherited, documented successful-end
//!    bookkeeping (`foff + dst.len()`), which maps past the writer edge.
//! 3. `mixed_range_crossing_ring_old_breaks_the_overwritten_chain` — a mixed
//!    reply (NULL prefix + real tail across `ring_old`) is never eligible, so a
//!    pending 2-block chain is discarded and a fresh run of three consecutive
//!    eligible blocks is required before the single recovery.
//! 4. `invalid_reference_recovery_does_not_commit` — a confirmed recovery with
//!    no usable delivered reference (Tier 1/2/3 all unavailable) takes the
//!    noncommit path: explicit NULLs, no epoch/cursor/anchor movement,
//!    `ref_none` reported.
//! 5. `checkpoint_non_fabrication_at_writer_edge` — independently of the
//!    successful-cursor guard, a NULL-only read at the writer edge must not
//!    stamp a checkpoint: public `calibrate` about that attempted end stays
//!    invalid even though the distance band would have selected a forged,
//!    freshly-stamped record.
//!
//! Fixtures are host-only byte streams: they are valid enough for the packet /
//! PES parsers (sync bytes, PIDs, continuity counters, PES PTS) but they are
//! NOT a playable program and make no decoder-compatibility claim.

use std::sync::Arc;

use fuse_ntfs::contract::{StreamBackend, RINGSZ, TS_PKT_SZ};
use fuse_ntfs::engine::live_engine::LiveEngine;
use fuse_ntfs::engine::ring::{snap188, NULL_PKT};
use fuse_ntfs::engine::ts_xform::{CKPT_N, CKPT_STEP};

const MI: u64 = 1024 * 1024;
const TS: u64 = TS_PKT_SZ as u64;
const BLOCK: usize = 65_536;
/// 188 * 100: phase-free read size (strict-suite parity).
const READ: usize = 18_800;
/// Successful cursor after warm playback (32 whole `BLOCK` requests).
const PLAY_END: u64 = 2 * MI;
/// Aligned bookmark inside the low-offset band (188 * 10_109).
const BM: u64 = 1_900_492;

const PID_VID: u16 = 0x0100;
const PID_AUD: u16 = 0x0101;

// ------------------------------------------------------------- byte fixtures

/// Writes a 33-bit PTS in the 5-byte MPEG marker encoding.
fn write_pts(b: &mut [u8], pts: u64) {
    let p = pts & ((1u64 << 33) - 1);
    b[0] = 0x21 | (((p >> 29) as u8) & 0x0E);
    b[1] = (p >> 22) as u8;
    b[2] = 0x01 | (((p >> 14) as u8) & 0xFE);
    b[3] = (p >> 7) as u8;
    b[4] = 0x01 | (((p << 1) as u8) & 0xFE);
}

/// Host-only MPEG-TS fixture: 188-byte packets on the video PID (every third
/// packet on the audio PID), per-PID sequential continuity counters, and a PES
/// header carrying `base_pts + i * 1000` on payload-unit-start packets. Payload
/// tails are `0xFF` so real regions never compare equal to `NULL_PKT`.
fn stream(base_pts: u64, n_packets: usize) -> Vec<u8> {
    let mut out = vec![0xFFu8; n_packets * TS_PKT_SZ];
    let mut cc = [0u8; 0x2000];
    for i in 0..n_packets {
        let pid = if i % 3 == 2 { PID_AUD } else { PID_VID };
        let p = &mut out[i * TS_PKT_SZ..(i + 1) * TS_PKT_SZ];
        p[0] = 0x47;
        p[1] = (pid >> 8) as u8;
        p[2] = pid as u8;
        p[3] = 0x10 | cc[usize::from(pid)];
        cc[usize::from(pid)] = (cc[usize::from(pid)] + 1) & 0x0F;
        if i % 5 == 0 || i % 5 == 3 {
            p[1] |= 0x40; // payload unit start lives in TS byte 1 (R3)
            p[4..13].copy_from_slice(&[0x00, 0x00, 0x01, 0xE0, 0x00, 0x00, 0x80, 0x80, 0x05]);
            write_pts(&mut p[13..18], base_pts + i as u64 * 1000);
        }
    }
    out
}

/// Streams `bytes` into the ring in bounded chunks: wrap fixtures stay
/// O(chunk) in memory instead of allocating a whole-ring capture.
fn advance(engine: &LiveEngine, bytes: u64) {
    if bytes == 0 {
        return;
    }
    let chunk = stream(0, 11_000); // 2_068_000 B of whole packets
    let mut left = bytes;
    while left > 0 {
        let take = left.min(chunk.len() as u64) as usize;
        engine.ring.write_chunk(&chunk[..take]);
        left -= take as u64;
    }
}

// ------------------------------------------------------------- ring / anchor

fn s_write(engine: &LiveEngine) -> u64 {
    engine.ring.lock().s_write()
}

fn ring_old(engine: &LiveEngine) -> u64 {
    engine.ring.lock().ring_old()
}

fn null_bytes(engine: &LiveEngine) -> u64 {
    engine.ring.lock().telemetry().null_bytes
}

fn served_bytes(engine: &LiveEngine) -> u64 {
    engine.ring.lock().telemetry().served_bytes
}

fn last_fend(engine: &LiveEngine) -> u64 {
    engine.anchor.lock().unwrap().last_fend
}

fn base_spos(engine: &LiveEngine) -> u64 {
    engine.anchor.lock().unwrap().base_spos
}

fn ec(engine: &LiveEngine) -> u64 {
    engine.anchor.lock().unwrap().epoch_seq
}

/// Public reference-selection counters (`served`, `ckpt`, `scan`, `none`).
fn refs(engine: &LiveEngine) -> (u64, u64, u64, u64) {
    let x = engine.xform.lock().unwrap();
    (x.ref_served, x.ref_ckpt, x.ref_scan, x.ref_none)
}

fn sm_confirms(engine: &LiveEngine) -> u64 {
    engine.sm.lock().unwrap().ovw_confirms
}

fn ckpt_slot(fend: u64) -> usize {
    (((fend - 1) / CKPT_STEP) as usize) % CKPT_N
}

/// True when `buf` is byte-exactly a run of NULL packets at any stream phase.
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

/// Sequential `BLOCK` reads from the first block after the cold anchor read up
/// to `end`, asserting that every reply delivers real media.
fn play_to(engine: &LiveEngine, end: u64) {
    let mut buf = vec![0u8; BLOCK];
    let mut foff = BLOCK as u64;
    while foff < end {
        let n = engine.serve_live(0, foff, &mut buf, 1000);
        assert_eq!(n, BLOCK, "playback read at {foff}");
        assert!(!all_null(&buf), "playback read at {foff} must be real");
        foff += BLOCK as u64;
    }
}

/// Warm playback fixture: `initial_bytes` are fed, the cold read at offset 0
/// establishes the anchor, and playback advances the cursor to `PLAY_END`.
/// Returns `(engine, base_spos, epoch)`.
fn warm_playback(initial_bytes: u64) -> (Arc<LiveEngine>, u64, u64) {
    let engine = Arc::new(LiveEngine::new());
    advance(&engine, initial_bytes);
    let mut buf = vec![0u8; BLOCK];
    let n = engine.serve_live(0, 0, &mut buf, 1000);
    assert_eq!(n, BLOCK, "cold read");
    assert!(!all_null(&buf), "cold read must deliver real bytes");
    let base = base_spos(&engine);
    let e0 = ec(&engine);
    assert_eq!(base % TS, 0, "anchor is 188-aligned");
    play_to(&engine, PLAY_END);
    assert_eq!(
        last_fend(&engine),
        PLAY_END,
        "successful cursor after playback"
    );
    (engine, base, e0)
}

/// Warm playback plus a full ring wrap whose oldest resident position lands on
/// `base + snap188(6 MiB)`: the low-offset bookmark band is wholly overwritten
/// while the live band stays resident. Returns `(engine, base, ring_old, sw, e0)`.
fn wrapped_fixture() -> (Arc<LiveEngine>, u64, u64, u64, u64) {
    let (engine, base, e0) = warm_playback(56_000 * TS); // ~10.04 MiB
    let x = snap188(6 * MI);
    let (cap, sw0) = {
        let rd = engine.ring.lock();
        (rd.capacity(), rd.s_write())
    };
    assert!(
        cap as usize <= RINGSZ,
        "ring capacity is at most the contract size"
    );
    let delta = base + x + cap - sw0;
    assert!(delta > 0, "wrap needs more feed");
    advance(&engine, delta);
    let old = ring_old(&engine);
    assert_eq!(
        old,
        base + x,
        "oldest resident position is exactly base + x"
    );
    assert_eq!(old % TS, 0, "oldest resident position is 188-aligned");
    assert!(old > base + PLAY_END, "bookmark band is washed out");
    let sw = s_write(&engine);
    (engine, base, old, sw, e0)
}

// The nine scenarios live in two cohesive child modules of this integration
// target (explicit paths, so they are not auto-discovered Cargo targets). Both
// use `super::*` for the shared fixtures and helpers above: nothing is
// duplicated and the Cargo target is unchanged.

#[path = "no_data_progress/delivery.rs"]
mod delivery;

#[path = "no_data_progress/recovery.rs"]
mod recovery;
