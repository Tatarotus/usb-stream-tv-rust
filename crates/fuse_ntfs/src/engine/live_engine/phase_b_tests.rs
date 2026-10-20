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
