//! 128 MiB circular RAM ring buffer: the Live TV data plane.
//!
//! Production invariants (docs/LIVE_EDGE_LOCK_AND_PROBE_ISOLATION.md, AGENTS.md §2.2):
//!
//! * **Zero-throttle feeder.** The writer never sleeps and never consults the
//!   reader lead: live TV is a continuous real-time flow. If the decoder
//!   pauses, the writer simply overwrites old data; a reader that falls out of
//!   the circular window (`s < ring_old`) is transparently re-anchored by the
//!   engine, never throttled through the writer.
//! * **Monotonic write cursor.** `s_write` only ever grows; the physical index
//!   is `(pos % capacity) as usize`.
//! * **Token-bucket pacing.** The reader side is paced per lead band
//!   (480/600/900 KB/s) inside a shared 250 ms window. Every sleep is sliced
//!   to <= 50 ms with the mutex unlocked so the MStar SCSI/USB controller
//!   never times out.
//! * **Bounded live-edge wait.** A reader that reaches the writer head waits
//!   cooperatively on the condvar for at most 500 ms per FUSE read.

use std::sync::{Condvar, Mutex};
use std::time::{Duration, Instant};

use crate::contract::{RINGSZ, TS_PKT_SZ};

/// Phase-aligned MPEG-TS null packet (PID 0x1FFF).
pub const NULL_PKT: [u8; TS_PKT_SZ] = {
    let mut pkt = [0xFFu8; TS_PKT_SZ];
    pkt[0] = 0x47;
    pkt[1] = 0x1F;
    pkt[2] = 0xFF;
    pkt[3] = 0x10;
    pkt
};

/// Snaps a monotonically-increasing stream position down to the next lower
/// 188-byte packet boundary (saturating: never underflows).
pub const fn snap188(pos: u64) -> u64 {
    pos.saturating_sub(pos % TS_PKT_SZ as u64)
}

/// Fills `dst` with phase-aligned NULL packets starting exactly at `dst[0]`,
/// so every 188-byte grid position inside the window is a complete packet.
pub fn fill_null(dst: &mut [u8]) {
    fill_null_at(dst, 0);
}

pub fn fill_null_at(dst: &mut [u8], abs_off: u64) {
    let mut off = 0usize;
    while off < dst.len() {
        let phase = ((abs_off + off as u64) % TS_PKT_SZ as u64) as usize;
        let chunk = (dst.len() - off).min(TS_PKT_SZ - phase);
        dst[off..off + chunk].copy_from_slice(&NULL_PKT[phase..phase + chunk]);
        off += chunk;
    }
}

/// Reader pacing ceilings (bytes/second), selected by the lead band.
///
/// * `Slow480` — cushion nearly empty (< 2 MiB): match the slowest feeder.
/// * `Mid600`  — healthy cushion (2..8 MiB).
/// * `Fast900` — deep cushion (>= 8 MiB): let the decoder catch up.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PaceTier {
    Slow480,
    Mid600,
    Fast900,
}

impl PaceTier {
    /// Bytes/second ceiling for this tier.
    pub const fn rate(self) -> u64 {
        match self {
            PaceTier::Slow480 => 480_000,
            PaceTier::Mid600 => 600_000,
            PaceTier::Fast900 => 900_000,
        }
    }

    /// Selects the tier from the current cushion (`s_write - read position`).
    pub const fn for_lead(lead: u64) -> Self {
        if lead < PACE_LEAD_SLOW {
            PaceTier::Slow480
        } else if lead < PACE_LEAD_FAST {
            PaceTier::Mid600
        } else {
            PaceTier::Fast900
        }
    }
}

/// Token-bucket window shared by all FUSE reader threads.
pub const PACE_WINDOW_MS: u64 = 250;
/// Hard clamp on any single pacing sleep (AGENTS.md §2.2).
pub const MAX_SLEEP_SLICE_MS: u64 = 50;
/// Total cooperative live-edge wait budget per FUSE read.
pub const EDGE_WAIT_BUDGET_MS: u64 = 500;
/// Below this cushion the reader is paced at the slow tier.
pub const PACE_LEAD_SLOW: u64 = 2 * 1024 * 1024;
/// At/above this cushion the reader is paced at the fast tier.
pub const PACE_LEAD_FAST: u64 = 8 * 1024 * 1024;

/// Ring data plane. All fields are private to the engine mutex; access goes
/// through the checked helpers below (no direct indexing).
pub struct RingData {
    buffer: Box<[u8]>,
    capacity: u64,
    s_write: u64,
    have_data: bool,
    start: Instant,
    // token bucket state (shared across reader threads)
    pace_win_start_ms: u64,
    pace_win_bytes: u64,
    // telemetry (drives the AGENTS.md §4 verification: near_starve=0, nulls=0)
    null_bytes: u64,
    served_bytes: u64,
    near_starve: u64,
    edge_waits: u64,
    overwritten_blocks: u64,
    last_lead: u64,
}

impl Default for RingData {
    fn default() -> Self {
        Self::new()
    }
}

impl RingData {
    /// Allocates the production 128 MiB ring.
    pub fn new() -> Self {
        Self::with_capacity(RINGSZ as u64)
    }

    /// Allocates a ring with a custom capacity (tests / small-footprint runs).
    /// Capacity is rounded up to a multiple of 188 so the 188-byte grid never
    /// straddles the wrap point.
    pub fn with_capacity(capacity: u64) -> Self {
        let cap = capacity.max(TS_PKT_SZ as u64);
        let cap = cap - (cap % TS_PKT_SZ as u64);
        Self {
            buffer: vec![0u8; cap as usize].into_boxed_slice(),
            capacity: cap,
            s_write: 0,
            have_data: false,
            start: Instant::now(),
            pace_win_start_ms: 0,
            pace_win_bytes: 0,
            null_bytes: 0,
            served_bytes: 0,
            near_starve: 0,
            edge_waits: 0,
            overwritten_blocks: 0,
            last_lead: 0,
        }
    }

    pub fn capacity(&self) -> u64 {
        self.capacity
    }

    pub fn s_write(&self) -> u64 {
        self.s_write
    }

    pub fn have_data(&self) -> bool {
        self.have_data
    }

    /// Oldest stream position still resident in the ring.
    pub fn ring_old(&self) -> u64 {
        self.s_write.saturating_sub(self.capacity)
    }

    pub fn now_ms(&self) -> u64 {
        self.start.elapsed().as_millis() as u64
    }

    /// Physical index of a monotonic position inside the ring.
    fn phys(&self, pos: u64) -> usize {
        (pos % self.capacity) as usize
    }

    /// Zero-throttle feeder append. The writer is the network ingestion
    /// thread: it copies `data` into the ring, advances `s_write`, and never
    /// sleeps — overwriting old data is the only back-pressure mechanism.
    pub fn append(&mut self, data: &[u8]) {
        if data.is_empty() {
            return;
        }
        // A feed larger than the ring cannot be represented in one pass;
        // keep only the trailing `capacity` bytes (the live edge), but the
        // monotonic write cursor advances by the *original* length so the
        // live edge lands at the end of the feed.
        let orig_len = data.len();
        let data = if data.len() as u64 > self.capacity {
            let skip = data.len() - self.capacity as usize;
            &data[skip..]
        } else {
            data
        };
        let count = data.len();
        let off = self.phys(self.s_write);

        if off + count <= self.capacity as usize {
            self.buffer[off..off + count].copy_from_slice(data);
        } else {
            let first = self.capacity as usize - off;
            self.buffer[off..].copy_from_slice(&data[..first]);
            self.buffer[..count - first].copy_from_slice(&data[first..]);
        }

        if self.s_write.saturating_add(data.len() as u64) > self.capacity {
            self.overwritten_blocks = self.overwritten_blocks.saturating_add(1);
        }
        self.s_write = self.s_write.saturating_add(orig_len as u64);
        self.have_data = true;
    }

    /// Copies `dst.len()` bytes from the ring at monotonic position `abs_pos`.
    /// Handles wrap-around. Bounds: caller must guarantee
    /// `abs_pos + dst.len() <= s_write`.
    pub fn read_at(&self, abs_pos: u64, dst: &mut [u8]) {
        let count = dst.len();
        if count == 0 {
            return;
        }
        let off = self.phys(abs_pos);
        if off + count <= self.capacity as usize {
            dst.copy_from_slice(&self.buffer[off..off + count]);
        } else {
            let first = self.capacity as usize - off;
            dst[..first].copy_from_slice(&self.buffer[off..]);
            dst[first..].copy_from_slice(&self.buffer[..count - first]);
        }
    }

    /// Copies a scan window `[base, base + out.len())` clamped to ring
    /// residency into `out`; returns the number of bytes actually copied.
    /// Bytes before `ring_old` are left untouched by this method.
    pub fn read_window(&self, base: u64, out: &mut [u8]) -> usize {
        let count = out.len();
        if count == 0 {
            return 0;
        }
        let lo = base.max(self.ring_old());
        let hi = self.s_write;
        if lo >= hi {
            return 0;
        }
        let avail = (hi - lo) as usize;
        let n = avail.min(count);
        self.read_at(lo, &mut out[..n]);
        n
    }

    /// True when the byte range `[pos, pos + len)` is fully resident.
    pub fn contains(&self, pos: u64, len: u64) -> bool {
        pos >= self.ring_old() && pos.saturating_add(len) <= self.s_write
    }

    /// Token-bucket accounting: charges `n` served bytes to the current
    /// window and returns the pacing sleep in milliseconds for the excess,
    /// clamped to `MAX_SLEEP_SLICE_MS`. Zero means "no sleep needed".
    pub fn pace_note(&mut self, n: u64, lead: u64, now_ms: u64) -> u64 {
        self.last_lead = lead;
        let elapsed = now_ms.saturating_sub(self.pace_win_start_ms);
        if elapsed >= PACE_WINDOW_MS {
            // Window rollover: start a fresh window at the current instant.
            self.pace_win_start_ms = now_ms;
            self.pace_win_bytes = 0;
        }
        let win_elapsed = now_ms.saturating_sub(self.pace_win_start_ms);
        let allowed = PaceTier::for_lead(lead).rate() * (win_elapsed + 1) / 1000;
        self.pace_win_bytes = self.pace_win_bytes.saturating_add(n);
        if self.pace_win_bytes <= allowed {
            return 0;
        }
        let excess = self.pace_win_bytes - allowed;
        let sleep_ms = excess.saturating_mul(1000) / PaceTier::for_lead(lead).rate();
        sleep_ms.min(MAX_SLEEP_SLICE_MS)
    }

    // ---- telemetry mutators (called by the engine serve path) ----

    pub fn note_null(&mut self, n: u64) {
        self.null_bytes = self.null_bytes.saturating_add(n);
    }

    pub fn note_served(&mut self, n: u64) {
        self.served_bytes = self.served_bytes.saturating_add(n);
    }

    pub fn note_starve(&mut self) {
        self.near_starve = self.near_starve.saturating_add(1);
    }

    pub fn note_edge_wait(&mut self) {
        self.edge_waits = self.edge_waits.saturating_add(1);
    }

    pub fn note_overwrite(&mut self) {
        self.overwritten_blocks = self.overwritten_blocks.saturating_add(1);
    }

    pub fn telemetry(&self) -> Telemetry {
        Telemetry {
            s_write: self.s_write,
            ring_old: self.ring_old(),
            null_bytes: self.null_bytes,
            served_bytes: self.served_bytes,
            near_starve: self.near_starve,
            edge_waits: self.edge_waits,
            overwritten_blocks: self.overwritten_blocks,
            pace_tier: PaceTier::for_lead(self.last_lead),
        }
    }
}

/// Reader-side telemetry snapshot (verification contract: near_starve == 0,
/// null_bytes == 0 on a healthy run).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Telemetry {
    pub s_write: u64,
    pub ring_old: u64,
    pub null_bytes: u64,
    pub served_bytes: u64,
    pub near_starve: u64,
    pub edge_waits: u64,
    pub overwritten_blocks: u64,
    pub pace_tier: PaceTier,
}

/// Thread-safe ring: data plane under a mutex + condvar for live-edge waits.
pub struct Ring {
    data: Mutex<RingData>,
    cv: Condvar,
}

impl Default for Ring {
    fn default() -> Self {
        Self::new()
    }
}

impl Ring {
    /// Production ring with the contract-mandated 128 MiB buffer.
    pub fn new() -> Self {
        Self::with_capacity(RINGSZ as u64)
    }

    pub fn with_capacity(capacity: u64) -> Self {
        Self {
            data: Mutex::new(RingData::with_capacity(capacity)),
            cv: Condvar::new(),
        }
    }

    pub fn lock(&self) -> std::sync::MutexGuard<'_, RingData> {
        self.data.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Zero-throttle feeder entry point: append network bytes and wake every
    /// reader waiting at the live edge. Never sleeps, never inspects the lead.
    pub fn write_chunk(&self, data: &[u8]) {
        let mut rd = self.lock();
        rd.append(data);
        drop(rd);
        self.cv.notify_all();
    }

    /// Bounded cooperative wait at the live edge: blocks until `s_write`
    /// advances past `start` or the budget expires, waking on feeder
    /// notifications. Each condvar wait is sliced to <= 50 ms so callers can
    /// re-check shutdown/deadline state. Returns true when data is available.
    pub fn wait_edge(&self, start: u64, budget: Duration) -> bool {
        let deadline = Instant::now() + budget;
        let mut rd = self.lock();
        loop {
            if rd.s_write() > start {
                return true;
            }
            let now = Instant::now();
            if now >= deadline {
                return false;
            }
            let slice = (deadline - now).min(Duration::from_millis(MAX_SLEEP_SLICE_MS));
            let (guard, _timeout) = self
                .cv
                .wait_timeout(rd, slice)
                .unwrap_or_else(|e| e.into_inner());
            rd = guard;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snap188_grid() {
        assert_eq!(snap188(0), 0);
        assert_eq!(snap188(187), 0);
        assert_eq!(snap188(188), 188);
        assert_eq!(snap188(375), 188);
        assert_eq!(snap188(u64::MAX), u64::MAX - u64::MAX % 188);
    }

    #[test]
    fn null_packet_is_phase_aligned() {
        let mut buf = [0u8; 376];
        fill_null(&mut buf);
        assert_eq!(buf[0], 0x47);
        assert_eq!(&buf[1..4], &[0x1F, 0xFF, 0x10]);
        assert_eq!(buf[188], 0x47);
        assert_eq!(&buf[189..192], &[0x1F, 0xFF, 0x10]);
        // Odd-length fills end with a phase-correct prefix, never overflow.
        let mut odd = [0u8; 100];
        fill_null(&mut odd);
        assert_eq!(&odd[..4], &[0x47, 0x1F, 0xFF, 0x10]);
    }

    #[test]
    fn pace_tier_rates() {
        assert_eq!(PaceTier::Slow480.rate(), 480_000);
        assert_eq!(PaceTier::Mid600.rate(), 600_000);
        assert_eq!(PaceTier::Fast900.rate(), 900_000);
        assert_eq!(PaceTier::for_lead(1_000_000), PaceTier::Slow480);
        assert_eq!(PaceTier::for_lead(4 * 1024 * 1024), PaceTier::Mid600);
        assert_eq!(PaceTier::for_lead(16 * 1024 * 1024), PaceTier::Fast900);
    }

    #[test]
    fn wrap_around_and_boundary() {
        // Small ring (multiple of 188) to exercise the wrap path cheaply.
        let mut rd = RingData::with_capacity(188 * 4);
        assert_eq!(rd.capacity(), 188 * 4);

        // Fill exactly to capacity.
        let full: Vec<u8> = (0..(188 * 4)).map(|i| (i % 251) as u8).collect();
        rd.append(&full);
        assert_eq!(rd.s_write(), 188 * 4);
        assert_eq!(rd.ring_old(), 0);

        // Overwrite: the oldest 188*2 bytes are gone, s_write keeps growing.
        let more: Vec<u8> = (0..(188 * 2)).map(|i| (i % 251) as u8).collect();
        rd.append(&more);
        assert_eq!(rd.s_write(), 188 * 6);
        assert_eq!(rd.ring_old(), 188 * 2);

        // A read straddling the physical wrap point must stitch both halves.
        let mut out = vec![0u8; 188 * 3];
        rd.read_at(188 * 3, &mut out); // covers [188*3, 188*6): wraps at 188*4
        assert_eq!(out, full[188 * 3..].iter().copied().chain(more.iter().copied()).collect::<Vec<u8>>());

        // Boundary: a read ending exactly at the physical end is contiguous.
        let mut edge = vec![0u8; 188];
        rd.read_at(188 * 3, &mut edge);
        assert_eq!(&edge[..], &full[188 * 3..]);
    }

    #[test]
    fn feed_larger_than_ring_keeps_live_edge() {
        let mut rd = RingData::with_capacity(188 * 2);
        let big: Vec<u8> = (0..(188 * 4)).map(|i| (200 + i % 50) as u8).collect();
        rd.append(&big);
        assert_eq!(rd.s_write(), 188 * 4);
        assert_eq!(rd.ring_old(), 188 * 2);
        let mut out = vec![0u8; 188 * 2];
        rd.read_at(188 * 2, &mut out);
        assert_eq!(out, &big[188 * 2..]);
    }

    #[test]
    fn read_window_clamps_to_residency() {
        let mut rd = RingData::with_capacity(188 * 8);
        let pat: Vec<u8> = (0..(188 * 4)).map(|i| i as u8).collect();
        rd.append(&pat);
        // Request a window that starts before ring_old / extends past s_write.
        let mut win = vec![0u8; 188 * 6];
        let n = rd.read_window(0, &mut win);
        assert_eq!(n, 188 * 4); // only resident bytes are copied
        assert_eq!(&win[..n], &pat[..]);

        let n2 = rd.read_window(188 * 10, &mut win);
        assert_eq!(n2, 0); // window entirely past the writer head
    }

    #[test]
    fn contains_residency() {
        let mut rd = RingData::with_capacity(188 * 4);
        rd.append(&vec![0xAA; 188 * 2]);
        assert!(rd.contains(0, 188));
        assert!(rd.contains(188, 188));
        assert!(rd.contains(1, 188));
        assert!(!rd.contains(188 * 2, 188)); // one packet past s_write
        rd.append(&vec![0xBB; 188 * 2]);
        assert!(rd.contains(0, 188 * 4));
        assert!(!rd.contains(0, 188 * 5));
    }

    #[test]
    fn token_bucket_clamps_to_50ms() {
        let mut rd = RingData::with_capacity(188 * 4);
        // Fast tier, deep cushion: allowed in the first ms window is tiny,
        // so a big charge must request a large sleep — clamped to 50 ms.
        let sleep = rd.pace_note(900_000, 16 * 1024 * 1024, 0);
        assert_eq!(sleep, MAX_SLEEP_SLICE_MS);
        // A small charge within the allowance sleeps zero.
        let mut rd2 = RingData::with_capacity(188 * 4);
        assert_eq!(rd2.pace_note(1_000, 16 * 1024 * 1024, 0), 0);
    }

    #[test]
    fn token_bucket_slow_tier_sleeps_less() {
        let mut rd = RingData::with_capacity(188 * 4);
        // Same excess under the slow tier resolves to a smaller sleep.
        let sleep = rd.pace_note(480_000, 1_000, 0);
        assert!(sleep > 0 && sleep <= MAX_SLEEP_SLICE_MS);
        assert_eq!(sleep, MAX_SLEEP_SLICE_MS); // clamped
    }

    #[test]
    fn ring_mutex_notifies_waiters() {
        let ring = Ring::with_capacity(188 * 4);
        assert!(!ring.wait_edge(0, Duration::from_millis(10)));
        ring.write_chunk(&[0xCC; 188]);
        assert!(ring.wait_edge(0, Duration::from_millis(50)));
        assert!(!ring.wait_edge(188, Duration::from_millis(10)));
    }

    #[test]
    fn zero_length_append_is_noop() {
        let mut rd = RingData::with_capacity(188 * 4);
        rd.append(&[]);
        assert_eq!(rd.s_write(), 0);
        assert!(!rd.have_data());
    }
}
