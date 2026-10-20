//! Shared fixtures for the TASK-003 enabled/disabled parity tests.
//!
//! Child module of the `integration_observability` target (explicit path, so it
//! is not auto-discovered as a Cargo target): the parent supplies the schema
//! helpers, this module supplies the byte streams and the [`Pair`] harness.
//!
//! The fixtures are host-only MPEG-TS-like byte streams: valid enough for the
//! packet / PES parsers (sync bytes, PIDs, continuity counters, PES PTS) but NOT
//! a playable program, and they make no decoder-compatibility claim.
//!
//! The point of [`Pair`] is parity: two engines in identical state, one with an
//! observer attached and one disabled, driven by the same script with the
//! delivered bytes and the whole playback-state fingerprint compared after every
//! single step. Observation must never change what the TV receives.

use std::sync::Arc;
use std::time::Duration;

use fuse_ntfs::contract::{RINGSZ, TS_PKT_SZ};
use fuse_ntfs::engine::live_engine::LiveEngine;
use fuse_ntfs::engine::ring::{snap188, NULL_PKT};
use fuse_ntfs::observability::{Config, ManualClock, MemorySink, Mode, Observer, Worker};
use fuse_ntfs::StreamBackend;

pub const MI: u64 = 1024 * 1024;
pub const TS: u64 = TS_PKT_SZ as u64;
/// Coarse sequential read size (whole packets).
pub const BLOCK: usize = 65_536;
/// 188 * 100: phase-free read size (strict-suite parity).
pub const READ: usize = 18_800;
/// Successful cursor after warm playback (32 whole `BLOCK` requests).
pub const PLAY_END: u64 = 2 * MI;
/// Aligned bookmark inside the low-offset band (188 * 10_109).
pub const BM: u64 = 1_900_492;

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
/// tails are `0xFF` so real regions never compare equal to [`NULL_PKT`].
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
            p[1] |= 0x40; // payload unit start lives in TS byte 1
            p[4..13].copy_from_slice(&[0x00, 0x00, 0x01, 0xE0, 0x00, 0x00, 0x80, 0x80, 0x05]);
            write_pts(&mut p[13..18], base_pts + i as u64 * 1000);
        }
    }
    out
}

/// Streams `bytes` into `engine`'s ring in bounded chunks: wrap fixtures stay
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

/// True when `buf` is byte-exactly a run of NULL packets at any stream phase.
pub fn all_null(buf: &[u8]) -> bool {
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

// ----------------------------------------------------------- state snapshot

/// Whole playback-state fingerprint. Wall-clock derived values (`now_ms`,
/// `last_read_ms`) are deliberately excluded: they are compared as behavior
/// through the delivered bytes, not as identical instants. Everything else the
/// engine tracks is included, so a single perturbed field fails the parity.
pub fn fingerprint(engine: &LiveEngine) -> Vec<u64> {
    let t = engine.ring.lock().telemetry();
    let a = engine.anchor.lock().unwrap_or_else(|e| e.into_inner());
    let x = engine.xform.lock().unwrap_or_else(|e| e.into_inner());
    let sm = engine.sm.lock().unwrap_or_else(|e| e.into_inner());
    vec![
        t.s_write,
        t.ring_old,
        t.null_bytes,
        t.served_bytes,
        t.near_starve,
        t.edge_waits,
        t.overwritten_blocks,
        u64::from(a.active),
        u64::from(a.file_id),
        a.base_foff,
        a.base_spos,
        a.last_fend,
        a.epoch_seq,
        u64::from(a.probe_consec),
        x.epoch_id(),
        x.ref_served,
        x.ref_ckpt,
        x.ref_scan,
        x.ref_none,
        sm.ovw_confirms,
        sm.probe_rejects,
        sm.confirmed_seeks,
        sm.resumes,
        sm.transitions,
    ]
}

pub fn s_write(engine: &LiveEngine) -> u64 {
    engine.ring.lock().s_write()
}

pub fn ring_old(engine: &LiveEngine) -> u64 {
    engine.ring.lock().ring_old()
}

pub fn base_spos(engine: &LiveEngine) -> u64 {
    engine
        .anchor
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .base_spos
}

pub fn last_fend(engine: &LiveEngine) -> u64 {
    engine
        .anchor
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .last_fend
}

pub fn epoch(engine: &LiveEngine) -> u64 {
    engine
        .anchor
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .epoch_seq
}

pub fn confirms(engine: &LiveEngine) -> u64 {
    engine
        .sm
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .ovw_confirms
}

// -------------------------------------------------------------------- pair

/// Two engines with identical scripts applied, one observed and one not.
pub struct Pair {
    pub on: Arc<LiveEngine>,
    pub off: Arc<LiveEngine>,
    sink: Arc<MemorySink>,
    clock: Arc<ManualClock>,
    worker: Worker,
    reads: usize,
}

impl Pair {
    /// Both engines start in the same state; `on` reports to a bounded sink
    /// (global rate/burst high enough that no line is lost to the global
    /// budget) and `off` keeps the default disabled observer.
    ///
    /// The per-class budget is deliberately 1/s in production shape, so
    /// [`Pair::read`] moves the observer clock into a new second before each
    /// read: exactly one result per read is then admissible and a missing hook
    /// cannot hide behind the class gate.
    pub fn start() -> Self {
        let sink = Arc::new(MemorySink::new());
        let clock = Arc::new(ManualClock::new(5_000));
        let started = Observer::start(
            Config {
                mode: Mode::Basic,
                rate_per_s: 512,
                burst: 512,
            },
            Arc::clone(&clock) as Arc<dyn fuse_ntfs::observability::Clock>,
            Box::new(Arc::clone(&sink)),
        )
        .expect("observer starts");
        Self {
            off: Arc::new(LiveEngine::new()),
            on: Arc::new(LiveEngine::with_observer(started.observer)),
            sink,
            clock,
            worker: started.worker,
            reads: 0,
        }
    }

    /// Feeds the same byte stream to both rings in bounded chunks.
    pub fn feed(&mut self, bytes: u64) {
        advance(&self.on, bytes);
        advance(&self.off, bytes);
        self.assert_state("feed");
    }

    /// One read on both engines. The returned length, the full payload bytes and
    /// the resulting state fingerprint must be identical.
    pub fn read(&mut self, foff: u64, len: usize) -> Vec<u8> {
        // New second for the class budget: this read's own result is admissible.
        self.clock.advance(1_001);
        let mut observed = vec![0u8; len];
        let mut disabled = vec![0u8; len];
        let n_on = self.on.serve_live(0, foff, &mut observed, 1_000);
        let n_off = self.off.serve_live(0, foff, &mut disabled, 1_000);
        assert_eq!(n_on, n_off, "read at {foff}: length differs");
        assert_eq!(n_on, len, "read at {foff}: payload under-filled");
        assert_eq!(observed, disabled, "read at {foff}: payload differs");
        self.assert_state(&format!("read at {foff}"));
        self.reads += 1;
        observed
    }

    /// Compares the full state fingerprint of both engines.
    pub fn assert_state(&self, what: &str) {
        assert_eq!(
            fingerprint(&self.on),
            fingerprint(&self.off),
            "playback state diverged after {what}"
        );
    }

    /// Feeder restart: identical `clear` + refeed on both rings.
    pub fn restart(&mut self, bytes: u64) {
        self.on.ring.lock().clear();
        self.off.ring.lock().clear();
        self.feed(bytes);
    }

    /// Cold anchor read plus sequential playback to `PLAY_END`, returning the
    /// anchor stream base and the epoch it was established with.
    pub fn warm(&mut self) -> (u64, u64) {
        self.feed(56_000 * TS); // ~10.04 MiB
        let cold = self.read(0, BLOCK);
        assert!(!all_null(&cold), "cold read must deliver real bytes");
        let base = base_spos(&self.on);
        assert_eq!(base % TS, 0, "anchor is 188-aligned");
        let e0 = epoch(&self.on);
        let mut foff = BLOCK as u64;
        while foff < PLAY_END {
            let buf = self.read(foff, BLOCK);
            assert!(!all_null(&buf), "playback read at {foff} must be real");
            foff += BLOCK as u64;
        }
        assert_eq!(last_fend(&self.on), PLAY_END, "warm cursor");
        (base, e0)
    }

    /// Full ring wrap whose oldest resident position lands exactly on
    /// `base + snap188(6 MiB)`: the low-offset bookmark band is wholly
    /// overwritten while the live band stays resident.
    pub fn wrap(&mut self, base: u64) -> (u64, u64) {
        let x = snap188(6 * MI);
        let (cap, sw0) = {
            let rd = self.on.ring.lock();
            (rd.capacity(), rd.s_write())
        };
        assert!(cap <= RINGSZ as u64, "ring capacity within contract");
        let delta = base + x + cap - sw0;
        assert!(delta > 0, "wrap needs more feed");
        self.feed(delta);
        let old = ring_old(&self.on);
        assert_eq!(old, base + x, "oldest resident position is base + x");
        assert_eq!(old % TS, 0, "oldest resident position is 188-aligned");
        assert!(old > base + PLAY_END, "bookmark band is washed out");
        (old, s_write(&self.on))
    }

    /// Advances the simulated clock by `secs` on both rings (checkpoint ageing).
    pub fn expire(&mut self, secs: u64) {
        let dt = Duration::from_secs(secs);
        self.on.ring.lock().shift_time_back(dt);
        self.off.ring.lock().shift_time_back(dt);
        self.assert_state("expire");
    }

    /// Resets the transform epoch on both engines (cold-reset reference state).
    pub fn reset_epoch(&mut self, id: u64) {
        self.on
            .xform
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .reset_epoch(id);
        self.off
            .xform
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .reset_epoch(id);
        self.assert_state("reset_epoch");
    }

    pub fn read_count(&self) -> usize {
        self.reads
    }

    /// Drains the observed lines: both engines are dropped first so the queue
    /// is closed, then the worker is shut down and the sink is read out.
    pub fn lines(self) -> Vec<String> {
        let Self {
            on,
            off,
            sink,
            worker,
            ..
        } = self;
        drop(on);
        drop(off);
        assert!(worker.shutdown(5_000), "observer drained");
        sink.lines()
    }
}
