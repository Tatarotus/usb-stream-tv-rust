//! Circular RAM ring buffer and MPEG-TS stream synchronization for Live TV.

use std::sync::{Condvar, Mutex};
use std::time::Instant;

pub const RINGSZ: usize = 32 * 1024 * 1024; // 32 MB (~90s @ 2.9 Mbps)
pub const HDRCACHESZ: usize = 512 * 1024; // 512 KB header cache
pub const LEADBACK: u64 = 12 * 1024 * 1024; // 12 MB (~40s behind live for jitter immunity)
pub const TS_PKT_SZ: usize = 188;

pub const NULL_PKT: [u8; TS_PKT_SZ] = {
    let mut pkt = [0xFFu8; TS_PKT_SZ];
    pkt[0] = 0x47;
    pkt[1] = 0x1F;
    pkt[2] = 0xFF;
    pkt[3] = 0x10;
    pkt
};

pub fn snap188(p: u64) -> u64 {
    p.saturating_sub(p % TS_PKT_SZ as u64)
}

pub fn fill_null(dst: &mut [u8]) {
    let mut off = 0;
    while off < dst.len() {
        let chunk = (dst.len() - off).min(TS_PKT_SZ);
        dst[off..off + chunk].copy_from_slice(&NULL_PKT[..chunk]);
        off += chunk;
    }
}

pub struct RingState {
    pub buffer: Box<[u8]>,
    pub hcache: Box<[u8]>,
    pub hcache_len: usize,
    pub s_write: u64,
    pub have_data: bool,
    pub base: u64,
    pub base_valid: bool,
    pub prev_fend: u64,
    pub consec_small: usize,
    /// File offset of the most recent rejected probe read, `u64::MAX` when the
    /// sequential anti-probe tracker is idle. Used to distinguish a genuine
    /// sequential SCSI re-scan (a 4 KiB/8 KiB continuation of a known probe)
    /// from an isolated transient probe.
    pub last_probe_off: u64,
    pub last_file_read_ms: u64,
    pub start_time: Instant,
}

impl Default for RingState {
    fn default() -> Self {
        Self::new()
    }
}

impl RingState {
    pub fn new() -> Self {
        Self {
            buffer: vec![0u8; RINGSZ].into_boxed_slice(),
            hcache: vec![0u8; HDRCACHESZ].into_boxed_slice(),
            hcache_len: 0,
            s_write: 0,
            have_data: false,
            base: 0,
            base_valid: false,
            prev_fend: u64::MAX,
            consec_small: 0,
            last_probe_off: u64::MAX,
            last_file_read_ms: 0,
            start_time: Instant::now(),
        }
    }

    pub fn now_ms(&self) -> u64 {
        self.start_time.elapsed().as_millis() as u64
    }

    /// Appends incoming bytes into the circular ring buffer.
    pub fn ring_write(&mut self, data: &[u8]) {
        // Guard against feeds larger than the ring itself: the circular buffer
        // physically cannot hold more than RINGSZ bytes in one pass, and the
        // wrap arithmetic below would panic on a bigger slice.
        let count = data.len().min(RINGSZ);
        let off = (self.s_write % RINGSZ as u64) as usize;

        if off + count <= RINGSZ {
            self.buffer[off..off + count].copy_from_slice(&data[..count]);
        } else {
            let first = RINGSZ - off;
            self.buffer[off..RINGSZ].copy_from_slice(&data[..first]);
            self.buffer[..count - first].copy_from_slice(&data[first..count]);
        }

        self.s_write = self.s_write.saturating_add(count as u64);
        self.have_data = true;
    }

    /// Copies `dst.len()` bytes from circular ring starting at `abs_pos`.
    pub fn ring_copy(&self, dst: &mut [u8], abs_pos: u64) {
        let count = dst.len();
        let off = (abs_pos % RINGSZ as u64) as usize;

        if off + count <= RINGSZ {
            dst.copy_from_slice(&self.buffer[off..off + count]);
        } else {
            let first = RINGSZ - off;
            dst[..first].copy_from_slice(&self.buffer[off..RINGSZ]);
            dst[first..].copy_from_slice(&self.buffer[..count - first]);
        }
    }

    /// Scans backward from `s_write - LEADBACK` for a valid PAT packet (PID 0x0000).
    /// Requires consecutive 0x47 sync bytes, payload_unit_start_indicator, and table_id == 0.
    pub fn snap_open_base(&self) -> u64 {
        if self.s_write == 0 {
            return 0;
        }

        let target = if self.s_write > LEADBACK {
            snap188(self.s_write - LEADBACK)
        } else {
            0
        };

        let ro = self.s_write.saturating_sub(RINGSZ as u64);
        let safe_target = target.max(ro);

        // Scan up to 64 KiB for PAT (0x47, PID 0x0000, pusi=1, table_id=0)
        let mut cur = safe_target;
        while cur + (2 * TS_PKT_SZ as u64) <= self.s_write && cur < safe_target + 64 * 1024 {
            let off = (cur % RINGSZ as u64) as usize;
            if self.buffer[off] == 0x47 {
                let off_next = (off + TS_PKT_SZ) % RINGSZ;
                if self.buffer[off_next] == 0x47 {
                    let b1 = self.buffer[(off + 1) % RINGSZ];
                    let b2 = self.buffer[(off + 2) % RINGSZ];
                    let pid = (u16::from(b1 & 0x1F) << 8) | u16::from(b2);
                    let pusi = (b1 & 0x40) != 0;

                    if pid == 0x0000 && pusi {
                        // Check table_id (byte 5 if pointer_field == 0)
                        let ptr_field = self.buffer[(off + 4) % RINGSZ] as usize;
                        if ptr_field < 180 {
                            let table_id = self.buffer[(off + 5 + ptr_field) % RINGSZ];
                            if table_id == 0x00 {
                                return cur;
                            }
                        }
                    }
                }
            }
            cur += TS_PKT_SZ as u64;
        }

        safe_target
    }
}

pub struct LiveRing {
    pub state: Mutex<RingState>,
    pub cv: Condvar,
}

impl Default for LiveRing {
    fn default() -> Self {
        Self::new()
    }
}

impl LiveRing {
    pub fn new() -> Self {
        Self {
            state: Mutex::new(RingState::new()),
            cv: Condvar::new(),
        }
    }

    pub fn write_chunk(&self, data: &[u8]) {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.ring_write(data);
        self.cv.notify_all();
    }

    pub fn wait_data(&self, deadline: Instant) -> bool {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        while !state.have_data {
            let now = Instant::now();
            if now >= deadline {
                return false;
            }
            let timeout = deadline - now;
            let (new_state, result) = self
                .cv
                .wait_timeout(state, timeout)
                .unwrap_or_else(|e| e.into_inner());
            state = new_state;
            if result.timed_out() {
                return state.have_data;
            }
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_snap188() {
        assert_eq!(snap188(0), 0);
        assert_eq!(snap188(187), 0);
        assert_eq!(snap188(188), 188);
        assert_eq!(snap188(375), 188);
        assert_eq!(snap188(376), 376);
    }

    #[test]
    fn test_null_packet_pattern() {
        let mut buf = [0u8; 376];
        fill_null(&mut buf);
        assert_eq!(buf[0], 0x47);
        assert_eq!(buf[1], 0x1F);
        assert_eq!(buf[2], 0xFF);
        assert_eq!(buf[3], 0x10);
        assert_eq!(buf[188], 0x47);
        assert_eq!(buf[189], 0x1F);
    }

    #[test]
    fn test_ring_buffer_wrap() {
        let mut state = RingState::new();
        let chunk1 = vec![0xAA; 1024];
        let chunk2 = vec![0xBB; 1024];

        state.ring_write(&chunk1);
        assert_eq!(state.s_write, 1024);

        state.ring_write(&chunk2);
        assert_eq!(state.s_write, 2048);

        let mut read_buf = vec![0u8; 1024];
        state.ring_copy(&mut read_buf, 0);
        assert_eq!(read_buf, chunk1);

        state.ring_copy(&mut read_buf, 1024);
        assert_eq!(read_buf, chunk2);
    }
}
