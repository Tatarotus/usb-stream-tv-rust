//! TASK-004 Phase B — in-crate interleaving tests (#2 and #4).
//!
//! These tests need to pause a reader between calibration and anchor
//! publication; that state is not observable through an external integration
//! test crate. They are unit tests, compiled only with `--lib`.
//!
//! Run with `--test-threads=1` (the project's audit suite already does): the
//! pause seam is a single global and must not be armed by two tests at once.

use std::thread;

use crate::contract::{StreamBackend, TS_PKT_SZ};

use super::*;

const READ_LEN: usize = TS_PKT_SZ * 64;
const PAT_PERIOD: usize = 20;

fn pat(cc: u8) -> [u8; TS_PKT_SZ] {
    let mut p = [0xFFu8; TS_PKT_SZ];
    p[0] = 0x47;
    p[1] = 0x40;
    p[2] = 0x00;
    p[3] = 0x10 | (cc & 0x0F);
    p[4] = 0x00;
    p[5] = 0x00;
    p
}

fn sps(cc: u8) -> [u8; TS_PKT_SZ] {
    let mut p = [0xFFu8; TS_PKT_SZ];
    p[0] = 0x47;
    p[1] = 0x41;
    p[2] = 0x00;
    p[3] = 0x10 | (cc & 0x0F);
    p[4] = 0x00;
    p[5] = 0x00;
    p[6] = 0x00;
    p[7] = 0x01;
    p[8] = 0x67;
    p
}

fn pes(pid: u16, cc: u8, fill: u8) -> [u8; TS_PKT_SZ] {
    let mut p = [fill; TS_PKT_SZ];
    p[0] = 0x47;
    p[1] = (((pid >> 8) & 0x1F) as u8) | 0x40;
    p[2] = (pid & 0xFF) as u8;
    p[3] = 0x10 | (cc & 0x0F);
    p
}

fn mk_stream(n: usize, fill: u8) -> Vec<u8> {
    let mut v = Vec::with_capacity((n + n / PAT_PERIOD) * TS_PKT_SZ);
    let (mut pcc, mut scc, mut vcc, mut acc) = (0u8, 0u8, 0u8, 0u8);
    for i in 0..n {
        if i % PAT_PERIOD == 0 {
            v.extend_from_slice(&pat(pcc));
            pcc = pcc.wrapping_add(1);
            v.extend_from_slice(&sps(scc));
            scc = scc.wrapping_add(1);
        } else if i % 2 == 0 {
            v.extend_from_slice(&pes(0x0100, vcc, fill));
            vcc = vcc.wrapping_add(1);
        } else {
            v.extend_from_slice(&pes(0x0101, acc, fill));
            acc = acc.wrapping_add(1);
        }
    }
    v
}

fn establish(e: &LiveEngine, data: &[u8]) -> u64 {
    e.ring.write_chunk(data);
    let mut dst = vec![0u8; READ_LEN];
    e.serve_live(0, 0, &mut dst, 0);
    let a = e.anchor.lock().unwrap();
    assert!(a.active, "cold anchor");
    a.last_fend
}

fn assert_null(out: &[u8]) {
    let p0 = out.iter().position(|b| *b == 0x47).expect("NULL sync byte");
    assert!(p0 < TS_PKT_SZ);
    assert!(out[..p0].iter().all(|b| *b == 0xFF));
    for (j, got) in out.iter().enumerate().skip(p0) {
        let want = match (j - p0) % TS_PKT_SZ {
            0 => 0x47,
            1 => 0x1F,
            2 => 0xFF,
            3 => 0x10,
            _ => 0xFF,
        };
        assert_eq!(*got, want, "non-NULL byte at offset {j}");
    }
}

/// Interleaving #2: flush before the atomic reanchor transaction (generation
/// check + transformer update + anchor publication). The parked reader must not
/// publish an anchor, checkpoint, or transformer change for the old generation.
#[test]
fn flush_between_calibration_and_publication_is_rejected() {
    let e = LiveEngine::new();
    let data = mk_stream(42_000, 0x55);
    establish(&e, &data);
    let epoch_before = e.anchor.lock().unwrap().epoch_seq;

    test_seam::arm();
    thread::scope(|s| {
        let reader = s.spawn(|| {
            let mut d = vec![0u8; READ_LEN];
            e.serve_live(1, 0, &mut d, 0);
            d
        });
        test_seam::wait_reached();
        e.flush();
        let (flush_epoch, flush_splices) = {
            let x = e.xform.lock().unwrap();
            (x.epoch_id(), x.splices)
        };
        test_seam::release();
        let out = reader.join().expect("reader");
        assert_null(&out);
        let x = e.xform.lock().unwrap();
        assert_eq!(x.epoch_id(), flush_epoch, "stale read changed the epoch");
        assert_eq!(
            x.splices, flush_splices,
            "stale read changed the splice state"
        );
    });

    let a = e.anchor.lock().unwrap();
    assert!(!a.active, "stale reanchor must not publish");
    assert_eq!(a.last_fend, u64::MAX, "no stale checkpoint");
    assert_eq!(a.epoch_seq, epoch_before + 1, "flush epoch only");
    assert_eq!(e.ring.lock().s_write(), 0, "old generation discarded");
    assert_eq!(
        e.xform.lock().unwrap().epoch_id(),
        a.epoch_seq,
        "transformer and anchor epochs must agree"
    );
}

/// A flush must be one transaction: anchor invalidation, ring clear and
/// transformer reset cannot be interleaved with a new reanchor. The flush seam
/// parks after invalidation with xform+anchor held; the reader must block until
/// the reset completes and then observe only the new (empty) generation.
#[test]
fn flush_is_atomic_with_a_concurrent_reanchor() {
    let e = LiveEngine::new();
    let data = mk_stream(42_000, 0x55);
    establish(&e, &data);
    let epoch_before = e.anchor.lock().unwrap().epoch_seq;

    test_seam::arm();
    thread::scope(|s| {
        let flusher = s.spawn(|| e.flush());
        test_seam::wait_reached();
        let (tx, rx) = std::sync::mpsc::channel();
        let er = &e;
        let reader = s.spawn(move || {
            let mut d = vec![0u8; READ_LEN];
            er.serve_live(0, 0, &mut d, 0);
            let _ = tx.send(());
            d
        });
        let early = rx.recv_timeout(Duration::from_millis(500)).is_ok();
        test_seam::release();
        assert!(!early, "reader completed inside the flush transaction");
        flusher.join().expect("flush");
        let out = reader.join().expect("reader");
        assert_null(&out);
    });

    let a = e.anchor.lock().unwrap();
    assert!(!a.active);
    assert_eq!(a.last_fend, u64::MAX);
    assert_eq!(a.epoch_seq, epoch_before + 1, "flush epoch only");
    assert_eq!(e.ring.lock().s_write(), 0, "ring cleared by flush");
    assert_eq!(
        e.xform.lock().unwrap().epoch_id(),
        a.epoch_seq,
        "flush left the transformer on another epoch"
    );
}

/// Flush after pacing but before the delivery commit (the F4 window). The
/// read must reject the prepared bytes instead of returning an old generation.
#[test]
fn flush_after_pacing_before_commit_is_rejected() {
    let e = LiveEngine::new();
    let data = mk_stream(42_000, 0x55);
    let last = establish(&e, &data);

    test_seam::arm();
    thread::scope(|s| {
        let reader = s.spawn(|| {
            let mut d = vec![0u8; READ_LEN];
            e.serve_live(0, last, &mut d, 0);
            d
        });
        test_seam::wait_reached();
        e.flush();
        test_seam::release();
        let out = reader.join().expect("reader");
        assert_null(&out);
    });

    let a = e.anchor.lock().unwrap();
    assert!(!a.active);
    assert_eq!(a.last_fend, u64::MAX, "flushed generation has no commit");
}

/// Interleaving #4: flush during reanchor, then new-generation data. The
/// parked reader aborts without touching the transformer, and the next read
/// must serve the new stream, not the old one.
#[test]
fn new_generation_after_reanchor_flush_is_served() {
    let e = LiveEngine::new();
    let old = mk_stream(42_000, 0x55);
    establish(&e, &old);
    let epoch_before = e.anchor.lock().unwrap().epoch_seq;

    test_seam::arm();
    thread::scope(|s| {
        let reader = s.spawn(|| {
            let mut d = vec![0u8; READ_LEN];
            e.serve_live(1, 0, &mut d, 0);
            d
        });
        test_seam::wait_reached();
        e.flush();
        let new = mk_stream(42_000, 0x5A);
        e.ring.write_chunk(&new);
        let (flush_epoch, flush_splices) = {
            let x = e.xform.lock().unwrap();
            (x.epoch_id(), x.splices)
        };
        test_seam::release();
        let out = reader.join().expect("reader");
        assert_null(&out);
        {
            let x = e.xform.lock().unwrap();
            assert_eq!(x.epoch_id(), flush_epoch, "stale read changed the epoch");
            assert_eq!(
                x.splices, flush_splices,
                "stale read changed the splice state"
            );
        }

        let mut d = vec![0u8; READ_LEN];
        e.serve_live(1, 0, &mut d, 0);
        assert!(
            d.iter().any(|b| *b == 0x5A),
            "new generation was not served"
        );
        assert!(
            !d.iter().any(|b| *b == 0x55),
            "old generation leaked into the new read"
        );
        let a = e.anchor.lock().unwrap();
        assert!(a.active);
        assert!(a.epoch_seq > epoch_before + 1);
    });
}

// ================================================ V2 concurrency (TASK-008)
//
// The qualified-return "request observation" must never calibrate a stale epoch
// and must stay distinct from a successful-media checkpoint when a competing
// delivery or a generation-changing flush lands while the observation is parked
// before its commit (the F4 window).

/// Builds a washed-out, idle (> RESUME_GAP_MS), behind-cursor state and returns
/// the file offset a qualified return would observe.
fn qualified_return_fixture(e: &LiveEngine) -> u64 {
    establish(e, &mk_stream(42_000, 0x55));

    // Play forward to a ~4 MiB cursor.
    let mut dst = vec![0u8; READ_LEN];
    let mut foff = 0u64;
    while foff < 4 * 1024 * 1024 {
        e.serve_live(0, foff, &mut dst, 0);
        foff += READ_LEN as u64;
    }
    let cursor = e.anchor.lock().unwrap().last_fend;
    assert!(cursor >= 4 * 1024 * 1024, "playback cursor built");

    // Wash the whole playback region out with ~169 MiB of new stream (the ring
    // holds RINGSZ = 128 MiB, so the write head must pass capacity + the
    // playback region), then idle beyond both RETURN_IDLE_MS and
    // RESUME_GAP_MS (20 s).
    e.ring.write_chunk(&mk_stream(900_000, 0x77));
    e.ring.lock().shift_time_back(Duration::from_secs(21));

    // More than RESUME_JUMP behind the cursor, inside the washed-out region.
    let f = cursor - 1024 * 1024;
    let base = e.anchor.lock().unwrap().base_spos;
    let ring_old = e.ring.lock().ring_old();
    assert!(base + f < ring_old, "observation maps behind ring_old");
    f
}

/// V2a: a flush lands while the qualified observation is parked before its
/// commit. The observation must publish nothing: no cursor, no saved calibration
/// reference, no epoch/transformer change beyond the flush itself.
#[test]
fn qualified_observation_flush_race_publishes_no_stale_cursor() {
    let e = LiveEngine::new();
    let f = qualified_return_fixture(&e);
    let epoch_before = e.anchor.lock().unwrap().epoch_seq;
    let gen_before = e.anchor.lock().unwrap().gen;

    test_seam::arm();
    thread::scope(|s| {
        let reader = s.spawn(|| {
            let mut d = vec![0u8; READ_LEN];
            e.serve_live(0, f, &mut d, 0);
            d
        });
        test_seam::wait_reached();
        e.flush();
        test_seam::release();
        let out = reader.join().expect("reader");
        assert_null(&out);
    });

    let a = e.anchor.lock().unwrap();
    assert!(!a.active, "flush invalidated the anchor");
    assert_eq!(
        a.last_fend,
        u64::MAX,
        "a stale observation must not publish a chain cursor"
    );
    assert_eq!(
        a.saved_delivery_fend,
        u64::MAX,
        "a stale observation must not save a calibration reference"
    );
    assert_eq!(a.epoch_seq, epoch_before + 1, "flush epoch only");
    assert_eq!(a.gen, gen_before + 1, "flush generation only");
}

/// V2 control: without a competing event the same fixture publishes the
/// REQUEST cursor (chain advances so the next read is adjacent) while keeping
/// the genuine delivered end in `saved_delivery_fend` for calibration, and
/// without touching the epoch. This proves the two race tests above are not
/// vacuous: the observation path is genuinely taken.
#[test]
fn qualified_observation_without_race_publishes_request_cursor() {
    let e = LiveEngine::new();
    let f = qualified_return_fixture(&e);
    let a0 = {
        let a = e.anchor.lock().unwrap();
        (a.last_fend, a.epoch_seq)
    };

    let mut d = vec![0u8; READ_LEN];
    e.serve_live(0, f, &mut d, 0);
    assert_null(&d);

    let a = e.anchor.lock().unwrap();
    assert_eq!(
        a.last_fend,
        f + READ_LEN as u64,
        "the request observation advances the chain cursor"
    );
    assert_eq!(
        a.saved_delivery_fend, a0.0,
        "the genuine delivered end is preserved for calibration"
    );
    assert_eq!(a.epoch_seq, a0.1, "the observation never splices");
}

/// V2b: a competing SUCCESSFUL delivery (cold restart re-anchor) lands while the
/// qualified observation is parked. The observation must not calibrate the new
/// epoch from its stale request offset; the cursor and `saved_delivery_fend`
/// must follow the successful delivery only.
#[test]
fn qualified_observation_competing_delivery_stays_distinct() {
    let e = LiveEngine::new();
    let f = qualified_return_fixture(&e);
    let epoch_before = e.anchor.lock().unwrap().epoch_seq;

    test_seam::arm();
    thread::scope(|s| {
        let reader = s.spawn(|| {
            let mut d = vec![0u8; READ_LEN];
            e.serve_live(0, f, &mut d, 0);
            d
        });
        test_seam::wait_reached();
        // Competing successful delivery: a cold restart read re-anchors to the
        // live band and bumps the anchor generation.
        let mut live = vec![0u8; READ_LEN];
        e.serve_live(0, 0, &mut live, 0);
        test_seam::release();
        let out = reader.join().expect("reader");
        assert_null(&out);
        assert!(
            live.iter().any(|b| *b == 0x77),
            "the competing read delivered new-stream media"
        );
    });

    let a = e.anchor.lock().unwrap();
    assert!(a.active, "the successful delivery published its anchor");
    assert_eq!(
        a.last_fend, READ_LEN as u64,
        "chain cursor follows the successful delivery"
    );
    assert_eq!(
        a.saved_delivery_fend,
        u64::MAX,
        "no stale request-observation reference survives a successful delivery"
    );
    assert!(
        a.epoch_seq > epoch_before,
        "the competing delivery re-anchored to the live band"
    );
}

/// V2c: an ORDINARY successful delivery (in-window, same generation and epoch,
/// no re-anchor) must clear a pending request-observation reference, so
/// `saved_delivery_fend` cannot outlive the observation and later calibrate a
/// stale end. The re-anchoring race above would mask this path.
#[test]
fn ordinary_delivery_clears_pending_observation_reference() {
    let e = LiveEngine::new();
    let f = qualified_return_fixture(&e);
    let epoch0 = e.anchor.lock().unwrap().epoch_seq;

    // The qualified observation publishes the request cursor and saves the
    // genuine delivered end.
    let mut d = vec![0u8; READ_LEN];
    e.serve_live(0, f, &mut d, 0);
    assert_null(&d);
    assert_ne!(
        e.anchor.lock().unwrap().saved_delivery_fend,
        u64::MAX,
        "the observation saved a calibration reference"
    );

    // An ordinary in-window read, not adjacent and not a jump candidate.
    let (base_spos, base_foff) = {
        let a = e.anchor.lock().unwrap();
        (a.base_spos, a.base_foff)
    };
    let ring_old = e.ring.lock().ring_old();
    let x = ring_old + 1024 * 1024 - base_spos + base_foff;
    let mut live = vec![0u8; READ_LEN];
    e.serve_live(0, x, &mut live, 0);
    assert!(
        live.iter().any(|b| *b == 0x77),
        "the ordinary read delivered media"
    );

    let a = e.anchor.lock().unwrap();
    assert_eq!(a.epoch_seq, epoch0, "no re-anchor on the ordinary delivery");
    assert_eq!(
        a.last_fend,
        x + READ_LEN as u64,
        "the cursor follows the successful delivery"
    );
    assert_eq!(
        a.saved_delivery_fend,
        u64::MAX,
        "a successful delivery clears the request-observation reference"
    );
}
