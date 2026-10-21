//! TASK-004 Phase A — flush/generation concurrency.
//!
//! Explicit synchronization only: `Barrier`/`Condvar`/mutex ownership and
//! observable counters; no `sleep()` is used as a synchronization primitive.
//! RED cases are ignored by default; run them with
//! `cargo test -p fuse_ntfs --test integration_epoch_flush -- --ignored`.

use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use fuse_ntfs::contract::{StreamBackend, TS_PKT_SZ};
use fuse_ntfs::engine::LiveEngine;

const READ_LEN: usize = TS_PKT_SZ * 64;
const PAT_PERIOD: usize = 20;

fn engine_with_ring(capacity: u64) -> LiveEngine {
    LiveEngine::with_capacity(capacity)
}

fn pat_packet(cc: u8) -> [u8; TS_PKT_SZ] {
    let mut p = [0xFFu8; TS_PKT_SZ];
    p[0] = 0x47;
    p[1] = 0x40;
    p[2] = 0x00;
    p[3] = 0x10 | (cc & 0x0F);
    p[4] = 0x00;
    p[5] = 0x00;
    p
}

fn sps_packet(cc: u8) -> [u8; TS_PKT_SZ] {
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

fn pes_packet(pid: u16, cc: u8) -> [u8; TS_PKT_SZ] {
    let mut p = [0xFFu8; TS_PKT_SZ];
    p[0] = 0x47;
    p[1] = (((pid >> 8) & 0x1F) as u8) | 0x40;
    p[2] = (pid & 0xFF) as u8;
    p[3] = 0x10 | (cc & 0x0F);
    p
}

fn mk_stream(n: usize) -> Vec<u8> {
    let mut v = Vec::with_capacity((n + n / PAT_PERIOD) * TS_PKT_SZ);
    let (mut vcc, mut acc, mut pcc, mut scc) = (0u8, 0u8, 0u8, 0u8);
    for i in 0..n {
        if i % PAT_PERIOD == 0 {
            v.extend_from_slice(&pat_packet(pcc));
            pcc = pcc.wrapping_add(1);
            v.extend_from_slice(&sps_packet(scc));
            scc = scc.wrapping_add(1);
        } else if i % 2 == 0 {
            v.extend_from_slice(&pes_packet(0x0100, vcc));
            vcc = vcc.wrapping_add(1);
        } else {
            v.extend_from_slice(&pes_packet(0x0101, acc));
            acc = acc.wrapping_add(1);
        }
    }
    v
}

fn wait_until<F: FnMut() -> bool>(mut f: F, what: &str) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if f() {
            return;
        }
        thread::yield_now();
    }
    panic!("timeout waiting for {what}");
}

/// Establish a live anchor on a ring that already holds more than
/// `MIN_STREAM_START` bytes. Returns the number of bytes the cold read served.
fn establish(engine: &LiveEngine, data: &[u8]) -> u64 {
    engine.ring.write_chunk(data);
    let mut dst = vec![0u8; READ_LEN];
    let n = engine.serve_live(0, 0, &mut dst, 0);
    assert_eq!(n, READ_LEN);
    let a = engine.anchor.lock().unwrap();
    assert!(a.active, "cold read must publish an anchor");
    assert_ne!(a.last_fend, u64::MAX, "cold read must commit its end");
    a.last_fend
}

/// `fill_null_at` rotates the NULL packet to the request's absolute phase, so
/// locate the first sync byte and validate the rotated stream.
fn assert_null_sequence(out: &[u8]) {
    let p0 = out.iter().position(|b| *b == 0x47).expect("NULL sync byte");
    assert!(p0 < TS_PKT_SZ);
    assert!(
        out[..p0].iter().all(|b| *b == 0xFF),
        "partial NULL prefix before first sync"
    );
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

/// A2.3: flush while a reader waits at the ring edge. The waiting read must
/// wake, observe the new generation and deliver NULLs, never old bytes.
#[test]
fn flush_during_edge_wait_yields_nulls_with_new_epoch() {
    let e = Arc::new(engine_with_ring(16 * 1024 * 1024));
    let data = mk_stream(42_000);
    let _last = establish(&e, &data);
    let base_epoch = e.anchor.lock().unwrap().epoch_seq;

    let before = e.ring.lock().telemetry().edge_waits;
    let e2 = Arc::clone(&e);
    let reader = thread::spawn(move || {
        let mut d = vec![0u8; READ_LEN];
        e2.serve_live(0, 6 * 1024 * 1024, &mut d, 0);
        d
    });
    wait_until(
        || e.ring.lock().telemetry().edge_waits > before,
        "reader at the live edge",
    );

    e.flush();
    let out = reader.join().expect("reader");

    assert_null_sequence(&out);
    let t = e.ring.lock().telemetry();
    assert_eq!(t.s_write, 0, "flush must clear the generation");
    assert_eq!(e.anchor.lock().unwrap().epoch_seq, base_epoch + 1);
    assert_eq!(e.xform.lock().unwrap().epoch_id(), base_epoch + 1);
}

/// A2 negative control: without a flush, an adjacent read commits normally.
#[test]
fn adjacent_read_without_flush_commits_expected_end() {
    let e = engine_with_ring(16 * 1024 * 1024);
    let data = mk_stream(42_000);
    let last = establish(&e, &data);
    let epoch = e.anchor.lock().unwrap().epoch_seq;

    let mut dst = vec![0u8; READ_LEN];
    e.serve_live(0, last, &mut dst, 0);

    let a = e.anchor.lock().unwrap();
    assert!(a.active);
    assert_eq!(a.last_fend, last + READ_LEN as u64);
    assert_eq!(a.epoch_seq, epoch, "no flush means no epoch change");
}

/// A2.1 regression (public contract): after a flush, a request holding an old
/// foff is served NULLs and must not resurrect `last_fend` or the old
/// generation. The concurrent in-flight interleaving is covered by the in-crate
/// `flush_after_pacing_before_commit_is_rejected` unit test: the atomic flush
/// now acquires `xform` first, so holding it here would block the flush itself.
#[test]
fn stale_commit_after_flush_is_rejected() {
    let e = engine_with_ring(16 * 1024 * 1024);
    let data = mk_stream(42_000);
    let last = establish(&e, &data);
    e.flush();

    let mut out = vec![0u8; READ_LEN];
    e.serve_live(0, last, &mut out, 0);
    assert_null_sequence(&out);

    let a = e.anchor.lock().unwrap();
    assert!(!a.active, "anchor must stay inactive after flush");
    assert_eq!(a.last_fend, u64::MAX, "stale request resurrected last_fend");
}

/// A2 negative control: repeated flushes advance the epoch monotonically and
/// leave no data from the old generation.
#[test]
fn flush_increments_epoch_monotonically_and_clears_generation() {
    let e = engine_with_ring(16 * 1024 * 1024);
    let data = mk_stream(42_000);
    establish(&e, &data);
    let base = e.anchor.lock().unwrap().epoch_seq;
    for step in 1..=2u64 {
        e.flush();
        assert_eq!(e.anchor.lock().unwrap().epoch_seq, base + step);
        assert_eq!(e.xform.lock().unwrap().epoch_id(), base + step);
        assert_eq!(e.ring.lock().s_write(), 0);
    }
}

// TASK-004/A2 item 2 (flush between calibration and anchor publication) is
// blocked without a test seam: the reader recomputes `seq`, releases the
// anchor and enters `xform.lock()` with no observable state change that a
// test can wait on before the flush. A `#[cfg(test)]` hook would NOT be
// visible to this external integration-test crate. The smallest usable seam
// is a feature-gated `#[cfg(feature = "test-hooks")] #[doc(hidden)] pub fn`
// (park after calibration), enabled only for this test binary with
// `--features test-hooks`; see TASK_004_A_HANDOFF.md (BLOCKED_BY_TEST_SEAM).
