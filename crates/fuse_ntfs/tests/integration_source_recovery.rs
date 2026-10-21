//! TASK-005: source/reader failure is not gadget authority.
//!
//! These host-only tests use synthetic MPEG-TS bytes and the in-memory
//! observer. They never create `/data/local/tmp/resync_usb`, mount a
//! filesystem, touch `/dev/fuse`, or run a watchdog/mode script.

use std::sync::Arc;

use fuse_ntfs::contract::{StreamBackend, PROBE_ISOLATION_FOFF, TS_PKT_SZ};
use fuse_ntfs::engine::ring;
use fuse_ntfs::engine::LiveEngine;
use fuse_ntfs::observability::{Config, ManualClock, MemorySink, Mode, Observer};

const BLOCK: usize = 64 * 1024;
const PAT_PERIOD: usize = 20;

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

fn pes_packet(pid: u16, cc: u8, seed: u8) -> [u8; TS_PKT_SZ] {
    let mut p = [0xFFu8; TS_PKT_SZ];
    p[0] = 0x47;
    p[1] = (((pid >> 8) & 0x1F) as u8) | 0x40;
    p[2] = (pid & 0xFF) as u8;
    p[3] = 0x10 | (cc & 0x0F);
    for (i, b) in p.iter_mut().enumerate().skip(4) {
        *b = if (i + seed as usize) % 7 == 0 {
            0x47
        } else {
            0x55
        };
    }
    p
}

fn make_stream(len: usize, seed: u8) -> Vec<u8> {
    let packets = (len / TS_PKT_SZ).max(PAT_PERIOD);
    let mut v = Vec::with_capacity(packets * TS_PKT_SZ);
    let (mut pcc, mut scc, mut vcc, mut acc) = (0u8, 0u8, 0u8, 0u8);
    for i in 0..packets {
        if i % PAT_PERIOD == 0 {
            v.extend_from_slice(&pat_packet(pcc));
            pcc = pcc.wrapping_add(1);
            v.extend_from_slice(&sps_packet(scc));
            scc = scc.wrapping_add(1);
        } else if i % 2 == 0 {
            v.extend_from_slice(&pes_packet(0x0100, vcc, seed.wrapping_add(i as u8)));
            vcc = vcc.wrapping_add(1);
        } else {
            v.extend_from_slice(&pes_packet(0x0101, acc, seed.wrapping_add(i as u8)));
            acc = acc.wrapping_add(1);
        }
    }
    v
}

fn is_null(buf: &[u8], abs: u64) -> bool {
    let mut expected = vec![0u8; buf.len()];
    ring::fill_null_at(&mut expected, abs);
    buf == expected.as_slice()
}

#[derive(Clone, Copy, Debug)]
struct Snapshot {
    active: bool,
    base_foff: u64,
    base_spos: u64,
    last_fend: u64,
    epoch: u64,
}

fn snapshot(engine: &LiveEngine) -> Snapshot {
    let a = engine.anchor.lock().unwrap_or_else(|e| e.into_inner());
    Snapshot {
        active: a.active,
        base_foff: a.base_foff,
        base_spos: a.base_spos,
        last_fend: a.last_fend,
        epoch: a.epoch_seq,
    }
}

impl Snapshot {
    fn mapped(self, foff: u64) -> u64 {
        if foff >= self.base_foff {
            self.base_spos + (foff - self.base_foff)
        } else {
            self.base_spos.saturating_sub(self.base_foff - foff)
        }
    }
}

/// Behind-window probes are bounded NULL replies. They do not move the
/// successful cursor/epoch and they never emit the resync-marker event.
#[test]
fn behind_probes_are_non_destructive_and_never_request_resync() {
    let sink = Arc::new(MemorySink::new());
    let clock = Arc::new(ManualClock::new(20_000));
    let started = Observer::start(
        Config {
            mode: Mode::Basic,
            rate_per_s: 64,
            burst: 64,
        },
        clock,
        Box::new(Arc::clone(&sink)),
    )
    .expect("observer starts");
    let engine = Arc::new(LiveEngine::with_observer(started.observer));

    engine.ring.write_chunk(&make_stream(10 * 1024 * 1024, 1));
    let mut buf = vec![0u8; BLOCK];
    assert_eq!(engine.serve_live(0, 0, &mut buf, 1000), BLOCK);
    assert!(!is_null(&buf, 0), "initial open delivers real ring bytes");

    let before = snapshot(&engine);
    engine.ring.write_chunk(&make_stream(150 * 1024 * 1024, 2));
    let ring_old = engine.ring.lock().ring_old();
    let behind_foff = PROBE_ISOLATION_FOFF + TS_PKT_SZ as u64;
    assert!(
        before.mapped(behind_foff) < ring_old,
        "probe must map behind ring_old (mapped {}, ring_old {ring_old})",
        before.mapped(behind_foff)
    );

    for i in 0..8u64 {
        let foff = behind_foff + i * BLOCK as u64;
        let n = engine.serve_live(0, foff, &mut buf, 1000);
        assert_eq!(n, BLOCK, "behind probe {i}");
        assert!(
            is_null(&buf, before.mapped(foff)),
            "behind probe {i} must be phase-aware NULL"
        );
        let after = snapshot(&engine);
        assert_eq!(after.active, before.active);
        assert_eq!(after.base_foff, before.base_foff);
        assert_eq!(after.base_spos, before.base_spos);
        assert_eq!(after.last_fend, before.last_fend);
        assert_eq!(after.epoch, before.epoch);
    }
    assert_eq!(engine.sm.lock().unwrap().probe_rejects, 8);

    drop(engine);
    assert!(started.worker.shutdown(2_000), "worker drained");
    let lines = sink.lines();
    assert!(
        !lines
            .iter()
            .any(|l| l.contains("event=resync_request_result")),
        "routine probes must not request a marker: {lines:?}"
    );
    assert!(
        !lines.iter().any(|l| l.contains("reason=resync_request")),
        "routine probes must not carry a resync reason: {lines:?}"
    );
}

/// A writer that stops advancing is normal source starvation: the reader gets
/// bounded NULLs after the edge wait, but the successful cursor and epoch do
/// not advance and no gadget recovery is requested.
#[test]
fn source_starvation_is_in_band_and_does_not_move_cursor() {
    let engine = LiveEngine::new();
    engine.ring.write_chunk(&make_stream(10 * 1024 * 1024, 3));
    let mut buf = vec![0u8; BLOCK];
    assert_eq!(engine.serve_live(0, 0, &mut buf, 1000), BLOCK);
    assert!(!is_null(&buf, 0), "initial open delivers real ring bytes");

    let before = snapshot(&engine);
    // The writer edge in FILE coordinates: the anchor maps `base_foff` to
    // `base_spos`, so the live edge is `s_write - base_spos + base_foff`.
    // Using the raw ring cursor as a file offset only lands in-band when the
    // anchor happens to sit at its minimum lead.
    let foff = {
        let a = engine.anchor.lock().unwrap();
        engine
            .ring
            .lock()
            .s_write()
            .saturating_sub(a.base_spos)
            .saturating_add(a.base_foff)
    };
    assert!(foff > before.last_fend, "read must land at the writer edge");

    let n = engine.serve_live(0, foff, &mut buf, 1000);
    assert_eq!(n, BLOCK);
    assert!(is_null(&buf, before.mapped(foff)), "starved reply is NULL");

    let after = snapshot(&engine);
    assert_eq!(after.active, before.active);
    assert_eq!(after.base_foff, before.base_foff);
    assert_eq!(after.base_spos, before.base_spos);
    assert_eq!(
        after.last_fend, before.last_fend,
        "NULL-only starvation must not advance successful progress"
    );
    assert_eq!(after.epoch, before.epoch, "starvation is not a re-anchor");
    assert!(
        engine.ring.lock().telemetry().near_starve > 0,
        "the edge wait is accounted as starvation"
    );
}
