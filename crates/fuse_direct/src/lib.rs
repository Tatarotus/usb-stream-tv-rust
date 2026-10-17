//! Direct FUSE FAT32 disk serving engine for USB-Stream-TV.

pub mod fat32;
pub mod fuse_abi;
pub mod ring;
pub mod vod;

use std::fs::File;
use std::os::unix::fs::FileExt;
use std::sync::Arc;
use std::time::{Duration, Instant};

use fat32::{FatTemplate, BPS, FILE_SEC};
use ring::{fill_null, LiveRing};
use vod::VodEngine;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StreamMode {
    LiveFifo,
    VodHttp,
    VodLocal,
}

pub struct DiskServer {
    pub mode: StreamMode,
    pub template: FatTemplate,
    pub ring: Option<Arc<LiveRing>>,
    pub vod: Option<Arc<VodEngine>>,
    pub local_file: Option<File>,
}

impl DiskServer {
    pub fn new_live(template: FatTemplate, ring: Arc<LiveRing>) -> Self {
        Self {
            mode: StreamMode::LiveFifo,
            template,
            ring: Some(ring),
            vod: None,
            local_file: None,
        }
    }

    pub fn new_vod(template: FatTemplate, vod: Arc<VodEngine>) -> Self {
        Self {
            mode: StreamMode::VodHttp,
            template,
            ring: None,
            vod: Some(vod),
            local_file: None,
        }
    }

    pub fn new_local(template: FatTemplate, file: File) -> Self {
        Self {
            mode: StreamMode::VodLocal,
            template,
            ring: None,
            vod: None,
            local_file: Some(file),
        }
    }

    /// Serves a raw sector or byte slice from the virtual FAT32 disk.
    pub fn serve_disk(&self, dst: &mut [u8], offset: u64, deadline: Instant) {
        let mut done = 0;
        let mut cur_offset = offset;

        while done < dst.len() {
            let sec = cur_offset / BPS;
            let sec_off = (cur_offset % BPS) as usize;

            // Sector < 16424 is boot/FAT/root-directory metadata:
            // serve exactly 1 sector (512 B) at a time so partial
            // sector reads stay sector-aligned.
            if sec < FILE_SEC {
                let chunk = (dst.len() - done).min(BPS as usize - sec_off);
                let mut sec_buf = [0u8; 512];
                self.template.read_metadata_sector(sec, &mut sec_buf);
                dst[done..done + chunk].copy_from_slice(&sec_buf[sec_off..sec_off + chunk]);
                done += chunk;
                cur_offset += chunk as u64;
                continue;
            }

            // Sector >= 16424 is file data. The virtual FAT32 file occupies
            // contiguous clusters from FILE_SEC (cluster 3) onward, so the
            // whole remaining slice maps linearly: coalesce it into ONE
            // request instead of walking it in 512 B chunks.
            let chunk = dst.len() - done;
            let foff = cur_offset - (FILE_SEC * BPS);

            match self.mode {
                StreamMode::VodLocal => {
                    if let Some(ref f) = self.local_file {
                        if f.read_exact_at(&mut dst[done..done + chunk], foff).is_err() {
                            fill_null(&mut dst[done..done + chunk]);
                        }
                    } else {
                        fill_null(&mut dst[done..done + chunk]);
                    }
                }
                StreamMode::VodHttp => {
                    if let Some(ref v) = self.vod {
                        if v.serve_slice(&mut dst[done..done + chunk], foff, deadline).is_err() {
                            fill_null(&mut dst[done..done + chunk]);
                        }
                    } else {
                        fill_null(&mut dst[done..done + chunk]);
                    }
                }
                StreamMode::LiveFifo => {
                    if let Some(ref r) = self.ring {
                        // ---- Per-read state under ONE brief lock ----
                        // Fresh-playback re-anchor + anti-probe debounce are
                        // evaluated once per FUSE_READ, never per block.
                        let reject_probe = {
                            let mut state = r.state.lock().unwrap_or_else(|e| e.into_inner());
                            let now_ms = state.now_ms();

                            // Fresh playback detection on offset 0
                            if foff == 0
                                && (!state.base_valid
                                    || state.base + (ring::RINGSZ as u64) < state.s_write
                                    || now_ms.saturating_sub(state.last_file_read_ms) > 1500)
                                && state.have_data
                                && state.s_write > 0
                            {
                                state.base = state.snap_open_base();
                                state.base_valid = true;
                                state.prev_fend = u64::MAX;
                                state.consec_small = 0;

                                let copy_sz = 65536.min(ring::HDRCACHESZ);
                                if state.s_write > state.base {
                                    let av = (state.s_write - state.base) as usize;
                                    let hc_len = copy_sz.min(av);
                                    for i in 0..hc_len {
                                        let off = ((state.base + i as u64) % ring::RINGSZ as u64) as usize;
                                        state.hcache[i] = state.buffer[off];
                                    }
                                    state.hcache_len = hc_len;
                                }
                            }
                            state.last_file_read_ms = now_ms;

                            // Sequential anti-probe debounce (evaluated ONCE per
                            // FUSE_READ). Note: foff and prev_fend are both in the
                            // file offset (FOFF) domain.
                            let mut probe_rejected = false;

                            if foff >= 8 * 1024 * 1024 && state.base_valid {
                                let active_end = if state.prev_fend == u64::MAX {
                                    0
                                } else {
                                    state.prev_fend
                                };
                                let is_probe = foff != state.prev_fend
                                    && foff > active_end + 2 * 1024 * 1024;
                                if is_probe {
                                    // A probe landing exactly one or two TS-sized
                                    // blocks (4 KiB / 8 KiB) past the previous probe
                                    // is a sequential SCSI re-scan of the same
                                    // neighborhood: it continues the debounce chain.
                                    // Anything else restarts the K counter at 1, so
                                    // isolated/repeated probes never escalate to a
                                    // re-anchor.
                                    let sequential = state.last_probe_off == u64::MAX
                                        || foff == state.last_probe_off + 4096
                                        || foff == state.last_probe_off + 8192;
                                    if sequential || state.consec_small == 0 {
                                        state.consec_small += 1;
                                    } else {
                                        state.consec_small = 1;
                                    }
                                    state.last_probe_off = foff;

                                    if state.consec_small < 3 {
                                        // Transient probe (K < 3): reject with NULL packets.
                                        // prev_fend MUST NOT be updated on this path.
                                        probe_rejected = true;
                                    } else {
                                        // Confirmed sequential seek (K >= 3): re-anchor.
                                        state.base = state.snap_open_base();
                                        state.base_valid = true;
                                        state.prev_fend = u64::MAX;
                                        state.consec_small = 0;
                                        state.last_probe_off = u64::MAX;
                                    }
                                } else if foff == state.prev_fend {
                                    state.consec_small = 0;
                                }
                            }

                            probe_rejected
                        };

                        if reject_probe {
                            // Whole-read rejection: NULL packets, prev_fend untouched.
                            fill_null(&mut dst[done..done + chunk]);
                            done += chunk;
                            cur_offset += chunk as u64;
                            continue;
                        }

                        let mut waited_live_edge = false;
                        let mut fo = 0;
                        while fo < chunk {
                            let f = foff + fo as u64;
                            let block_step = (chunk - fo).min(4096);
                            let mut state = r.state.lock().unwrap_or_else(|e| e.into_inner());

                            if !state.have_data {
                                drop(state);
                                fill_null(&mut dst[done + fo..done + fo + block_step]);
                                fo += block_step;
                                continue;
                            }

                            let ring_old = state.s_write.saturating_sub(ring::RINGSZ as u64);

                            // Serve from initial header cache if near stream start
                            if f < state.hcache_len as u64 {
                                let backing = state.base + f;
                                if state.base_valid && backing < ring_old {
                                    // Stale hcache backing — fall through to ring_old NULL path
                                } else {
                                    let hc = state.hcache_len - f as usize;
                                    let to_copy = hc.min(block_step);
                                    dst[done + fo..done + fo + to_copy]
                                        .copy_from_slice(&state.hcache[f as usize..f as usize + to_copy]);
                                    drop(state);
                                    fo += to_copy;
                                    continue;
                                }
                            }

                            if !state.base_valid {
                                drop(state);
                                fill_null(&mut dst[done + fo..done + fo + block_step]);
                                fo += block_step;
                                continue;
                            }

                            let start = state.base + f;
                            if start < ring_old {
                                // Overwritten by ring buffer: return NULL packets
                                drop(state);
                                fill_null(&mut dst[done + fo..done + fo + block_step]);
                                fo += block_step;
                                continue;
                            }

                            // Live edge: reader caught up to the live writer.
                            // Wait at most ONCE per FUSE_READ, clamped to 50 ms
                            // and to the remaining request deadline.
                            if start >= state.s_write && state.have_data {
                                if !waited_live_edge {
                                    waited_live_edge = true;
                                    let wait_dur = deadline
                                        .saturating_duration_since(Instant::now())
                                        .min(Duration::from_millis(50));
                                    let (ns, _timed_out) = r
                                        .cv
                                        .wait_timeout(state, wait_dur)
                                        .unwrap_or_else(|e| e.into_inner());
                                    state = ns;
                                }
                                // Timeout expired or the writer never advanced
                                // past `start`: NULL packets for this block.
                                if start >= state.s_write {
                                    drop(state);
                                    fill_null(&mut dst[done + fo..done + fo + block_step]);
                                    fo += block_step;
                                    continue;
                                }
                            }

                            let avail = (state.s_write - start) as usize;
                            let to_copy = avail.min(block_step);
                            state.ring_copy(&mut dst[done + fo..done + fo + to_copy], start);
                            state.prev_fend = foff + (fo + to_copy) as u64;
                            drop(state);
                            fo += to_copy;
                        }
                    } else {
                        fill_null(&mut dst[done..done + chunk]);
                    }
                }
            }

            done += chunk;
            cur_offset += chunk as u64;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ring::TS_PKT_SZ;

    #[test]
    fn test_serve_disk_boot_and_fat() {
        let tmpl = FatTemplate::new();
        let ring = Arc::new(LiveRing::new());
        let server = DiskServer::new_live(tmpl, ring);

        let mut buf = [0u8; 1024];
        let deadline = Instant::now() + std::time::Duration::from_secs(1);
        server.serve_disk(&mut buf, 0, deadline);

        // First 512 bytes: Boot sector
        assert_eq!(&buf[0..3], &[0xEB, 0x58, 0x90]);
        assert_eq!(&buf[510..512], &[0x55, 0xAA]);

        // Second 512 bytes: FSInfo sector
        assert_eq!(&buf[512..516], b"RRaA");
        assert_eq!(&buf[1022..1024], &[0x55, 0xAA]);
    }

    #[test]
    fn test_serve_disk_live_ring() {
        let tmpl = FatTemplate::new();
        let ring = Arc::new(LiveRing::new());

        // Preload ring with synthetic TS packets
        let mut sample_ts = vec![0u8; TS_PKT_SZ * 100];
        for i in 0..100 {
            let off = i * TS_PKT_SZ;
            sample_ts[off] = 0x47;
            sample_ts[off + 1] = 0x40; // pusi=1, pid=0x0000 (PAT)
            sample_ts[off + 2] = 0x00;
            sample_ts[off + 3] = 0x10;
            sample_ts[off + 4] = 0x00; // pointer_field = 0
            sample_ts[off + 5] = 0x00; // table_id = 0x00 (PAT)
            sample_ts[off + 6] = i as u8;
        }
        ring.write_chunk(&sample_ts);

        let server = DiskServer::new_live(tmpl, ring);
        let mut buf = vec![0u8; TS_PKT_SZ * 4];
        let file_offset = FILE_SEC * BPS; // start of tv_stream.img data
        let deadline = Instant::now() + std::time::Duration::from_secs(1);

        server.serve_disk(&mut buf, file_offset, deadline);

        // Verify valid TS sync byte 0x47 received
        assert_eq!(buf[0], 0x47);
        assert_eq!(buf[TS_PKT_SZ], 0x47);
        assert_eq!(buf[TS_PKT_SZ * 2], 0x47);
        assert_eq!(buf[TS_PKT_SZ * 3], 0x47);
    }

    #[test]
    fn test_anti_probe_sequential_tracking() {
        const RING_BYTES: usize = 13 * 1024 * 1024;
        let mut sample_ts = vec![0u8; RING_BYTES];
        let mut off = 0;
        while off < sample_ts.len() {
            sample_ts[off] = 0x47;
            sample_ts[off + 1] = 0x40; // pusi=1, pid=0x0000 (PAT)
            sample_ts[off + 2] = 0x00;
            sample_ts[off + 3] = 0x10;
            sample_ts[off + 4] = 0x00; // pointer_field = 0
            sample_ts[off + 5] = 0x00; // table_id = 0x00 (PAT)
            off += TS_PKT_SZ;
        }

        let ring = Arc::new(LiveRing::new());
        ring.write_chunk(&sample_ts);

        {
            let mut s = ring.state.lock().unwrap_or_else(|e| e.into_inner());
            s.base = 0;
            s.base_valid = true;
            s.prev_fend = 8388748;
            s.consec_small = 0;
            s.last_probe_off = u64::MAX;
        }

        let server = DiskServer::new_live(FatTemplate::new(), Arc::clone(&ring));
        let file_offset = FILE_SEC * BPS;
        let deadline = Instant::now() + std::time::Duration::from_secs(1);
        let mut buf = vec![0u8; TS_PKT_SZ];

        for i in 1..=4u64 {
            let probe = 8 * 1024 * 1024 + 2 * 1024 * 1024 + i * 1024 * 1024;
            buf.fill(0);
            server.serve_disk(&mut buf, file_offset + probe, deadline);

            assert_eq!(&buf[1..4], &[0x1F, 0xFF, 0x10]);
            let s = ring.state.lock().unwrap_or_else(|e| e.into_inner());
            assert_eq!(s.consec_small, 1);
            assert_eq!(s.last_probe_off, probe);
            assert_eq!(s.base, 0);
            assert_eq!(s.prev_fend, 8388748);
        }

        server.serve_disk(&mut buf, file_offset + 8388748, deadline);
        assert_eq!(buf[0], 0x47);
        {
            let s = ring.state.lock().unwrap_or_else(|e| e.into_inner());
            assert_eq!(s.consec_small, 0);
            assert_eq!(s.last_probe_off, 14 * 1024 * 1024);
            assert_eq!(s.prev_fend, 8388748 + TS_PKT_SZ as u64);
        }

        let chain_start = 13 * 1024 * 1024;
        server.serve_disk(&mut buf, file_offset + chain_start, deadline);
        {
            let s = ring.state.lock().unwrap_or_else(|e| e.into_inner());
            assert_eq!(s.consec_small, 1);
            assert_eq!(s.base, 0);
        }

        server.serve_disk(&mut buf, file_offset + chain_start + 4096, deadline);
        {
            let s = ring.state.lock().unwrap_or_else(|e| e.into_inner());
            assert_eq!(s.consec_small, 2);
            assert_eq!(s.base, 0);
        }

        buf.fill(0);
        server.serve_disk(&mut buf, file_offset + chain_start + 8192, deadline);
        {
            let s = ring.state.lock().unwrap_or_else(|e| e.into_inner());
            assert_eq!(s.consec_small, 0);
            assert_eq!(s.last_probe_off, u64::MAX);
            assert_eq!(s.prev_fend, u64::MAX);
            assert_eq!(
                s.base,
                ring::snap188(RING_BYTES as u64 - ring::LEADBACK)
            );
            assert!(s.base_valid);
        }
    }
}
