//! TASK-002 delivery scenarios: starved/future replies, NULL suffixes, the
//! writer-edge checkpoint guard and the fixture's production-parser check.
//!
//! Child module of the `integration_no_data_progress` target: shared fixtures
//! and helpers come from the parent through `super::*`.

use std::time::Duration;

use super::*;
use fuse_ntfs::contract::PROBE_ISOLATION_FOFF;
use fuse_ntfs::engine::ts_xform::{ts_pes_ts, ts_pusi, CKPT_MAX_DIST, SERVED_FRESH_BYTES};

// ===================================================== test 1: producer restart

/// A feeder restart must not be mistaken for progress: requests past the new
/// head are explicitly NULL (both a starved read below the probe floor and a
/// rejected future probe), the successful cursor/epoch/calibration state stay
/// frozen and nothing confirms the overwritten recovery. Progress resumes once
/// the reader asks for bytes the restarted producer has actually delivered.
#[test]
fn producer_restart_does_not_forge_progress() {
    let (engine, base, e0) = warm_playback(56_000 * TS);
    let fend = last_fend(&engine);
    let refs0 = refs(&engine);

    // The feeder reconnects and rewrites from a fresh head: `clear` drops the
    // old window and the new feed streams from offset 0 again.
    engine.ring.lock().clear();
    advance(&engine, 40_000 * TS); // ~7.5 MiB, above MIN_STREAM_START
    let sw = s_write(&engine);
    let head = sw - base; // file offset mapping exactly onto the new head
    assert!(head > 0 && head < PROBE_ISOLATION_FOFF);

    let mut buf = vec![0u8; READ];
    // (a) Starved read at the new head (below the 8 MiB probe floor).
    let n = engine.serve_live(0, head, &mut buf, 1000);
    assert_eq!(n, READ);
    assert!(all_null(&buf), "no data past the restarted head");
    // (b) Future probe beyond the head: rejected before any cursor update.
    let f_probe = PROBE_ISOLATION_FOFF + MI;
    let n = engine.serve_live(0, f_probe, &mut buf, 1000);
    assert_eq!(n, READ);
    assert!(all_null(&buf), "future probe is rejected");

    // Both replies were explicitly accounted as unsupported NULLs.
    assert_eq!(
        null_bytes(&engine),
        2 * READ as u64,
        "explicit NULL accounting"
    );
    assert_eq!(last_fend(&engine), fend, "restart must not forge progress");
    assert_eq!(ec(&engine), e0, "starvation must not re-anchor");
    assert_eq!(refs(&engine), refs0, "starvation must not calibrate");
    assert_eq!(
        sm_confirms(&engine),
        0,
        "starvation never confirms recovery"
    );

    // Progress resumes from the new producer head (resident real bytes).
    let f_data = head - 4 * BLOCK as u64;
    let n = engine.serve_live(0, f_data, &mut buf, 1000);
    assert_eq!(n, READ);
    assert!(!all_null(&buf), "restarted producer data is served");
    assert_eq!(last_fend(&engine), f_data + READ as u64);
    assert_eq!(ec(&engine), e0, "serving restarted data is not a re-anchor");
    assert_eq!(refs(&engine), refs0, "no calibration on plain delivery");
    assert_eq!(
        null_bytes(&engine),
        2 * READ as u64,
        "real reply adds no NULLs"
    );
}

// ============================================ test 2: real bytes + NULL suffix

/// A reply whose resident real bytes are followed by a NULL suffix (the writer
/// edge is reached mid-reply) reports the unsupported tail and keeps the
/// inherited successful-end bookkeeping: the cursor is the reply end, which
/// maps past the producer edge. Disclosure only; no new behaviour is claimed.
#[test]
fn real_bytes_plus_null_suffix_reports_explicit_nulls() {
    let (engine, base, e0) = warm_playback(33_500 * TS); // whole packets, ~6.3 MiB
    let sw = s_write(&engine);
    assert_eq!(sw % TS, 0, "fixture head is packet-aligned");
    let real = 16 * TS; // 3008 B: 16 whole resident packets
    let foff = sw - real - base; // mapped start = sw - real
    let served0 = served_bytes(&engine);

    let mut buf = vec![0u8; BLOCK];
    let n = engine.serve_live(0, foff, &mut buf, 1000);
    assert_eq!(n, BLOCK);
    assert!(!all_null(&buf), "resident prefix is real media");
    assert!(
        all_null(&buf[real as usize..]),
        "the tail past the writer edge is NULL"
    );
    // The NULL suffix is reported, the real prefix is counted as served.
    assert_eq!(null_bytes(&engine), BLOCK as u64 - real);
    assert_eq!(served_bytes(&engine) - served0, real);
    // Documented inherited semantics: the successful end is the whole reply
    // end even though the tail was never delivered.
    assert_eq!(last_fend(&engine), foff + BLOCK as u64);
    assert!(
        base + last_fend(&engine) > sw,
        "successful end maps past the producer edge"
    );
    assert_eq!(ec(&engine), e0, "data + NULL reply must not re-anchor");
}

// ================================= test 5: checkpoint non-fabrication
//                                            (independent of the cursor guard)

/// A NULL-only read must not stamp a checkpoint. The fixture stays outside
/// recovery eligibility (mapped start at the writer edge, nothing overwritten)
/// and deliberately puts the attempted end outside the 256 KiB served proximity
/// while staying inside the 4 MiB checkpoint-distance limit. After the authentic
/// checkpoints are expired, public `calibrate` about that attempted end must
/// remain invalid: under the previous unconditional `note_ckpt` the forged,
/// freshly-stamped record would have been selected here.
#[test]
fn checkpoint_non_fabrication_at_writer_edge() {
    const DELTA: u64 = 2 * MI + 256 * 1024; // attempted-end distance band
    let (engine, base, _e0) = warm_playback(6 * MI); // clears the cold-start floor
    let fend = last_fend(&engine);
    let refs0 = refs(&engine);

    // Put the writer edge at `base + PLAY_END + DELTA` so the starved read's
    // end sits 256 KiB..4 MiB away from the genuine served position.
    let edge = base + PLAY_END + DELTA;
    assert!(
        edge > s_write(&engine),
        "fixture head already past the edge"
    );
    advance(&engine, edge - s_write(&engine));
    assert_eq!(s_write(&engine), edge);

    engine
        .ring
        .lock()
        .shift_time_back(Duration::from_secs(7200)); // expire ckpts

    // Starved read with the mapped start exactly at the writer edge. This test
    // deliberately makes no successful-cursor claim: it isolates checkpoint
    // fabrication from the cursor guard that other tests cover.
    let foff = edge - base;
    let mut buf = vec![0u8; READ];
    let n = engine.serve_live(0, foff, &mut buf, 1000);
    assert_eq!(n, READ);
    assert!(all_null(&buf), "writer-edge read is NULL-only");
    assert_eq!(refs(&engine), refs0, "NULL-only read does not calibrate");

    // Distance band of the attempted end relative to the genuine served state:
    // > SERVED_FRESH_BYTES (Tier 1 cannot apply) and <= CKPT_MAX_DIST (a forged
    // fresh record would have qualified).
    let attempted_end = foff + READ as u64;
    let old_pos = base + attempted_end;
    let served_lo = base + PLAY_END - BLOCK as u64;
    let served_hi = base + PLAY_END;
    assert!(
        old_pos - served_hi > SERVED_FRESH_BYTES,
        "attempted end is outside the served proximity"
    );
    assert!(
        old_pos - served_lo <= CKPT_MAX_DIST,
        "attempted end is inside the checkpoint distance limit"
    );
    assert_ne!(
        ckpt_slot(attempted_end),
        ckpt_slot(fend),
        "attempted end is a different checkpoint record"
    );
    assert!(
        attempted_end > fend,
        "attempted end is not the successful last end"
    );

    // Ask calibration about the attempted end (not the successful end).
    let now = engine.ring.lock().now_ms();
    let (r, after) = {
        let mut x = engine.xform.lock().unwrap();
        let r = x.calibrate(old_pos, attempted_end, &[], 0, now);
        (r, (x.ref_served, x.ref_ckpt, x.ref_scan, x.ref_none))
    };
    assert!(!r.valid, "a NULL-only read must not fabricate a reference");
    assert_eq!(after.1, refs0.1, "no checkpoint may be selected");
    assert_eq!(after.0, refs0.0, "no served tier");
    assert_eq!(after.2, refs0.2, "no ring-scan tier");
    assert_eq!(after.3, refs0.3 + 1, "reported as invalid reference");
}

// ==================== test 6: fixture read by the production TS parser

/// R3: the fixture is only evidence if the production parser reads it.
/// Payload-unit-start lives in TS byte 1 (byte 3 carries scrambling and
/// adaptation), and the PES header must yield the intended timestamps.
#[test]
fn fixture_is_parsed_by_production_ts_parser() {
    let s = stream(500_000, 12);
    let video = &s[0..TS_PKT_SZ];
    assert!(ts_pusi(video), "payload-unit-start must be TS byte 1");
    assert_eq!(ts_pes_ts(video), Some((500_000, None)), "video PES PTS");
    assert_eq!(
        ts_pes_ts(&s[8 * TS_PKT_SZ..9 * TS_PKT_SZ]),
        Some((508_000, None))
    );
}
