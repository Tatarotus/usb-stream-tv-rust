//! Scaffold draft engine (`LiveEngine`) preserved from the original
//! `src/engine.rs`. Kept as a reference implementation and as the backend
//! currently wired in `main.rs`; the production engine root is
//! `engine::MediaEngine` (see `super`). This module deliberately keeps the
//! original code shape — its submodule declarations now live in the parent.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::contract::{StreamBackend, TS_PKT_SZ, LEADBACK, PROBE_ISOLATION_FOFF};
use crate::engine::ring::{Ring, EDGE_WAIT_BUDGET_MS, MAX_SLEEP_SLICE_MS};
use crate::engine::state_machine::{SmCtx, SmDecision};

use crate::engine::ts_xform::{TsXformState, XformRef, RawSample};

#[derive(Debug, Clone)]
pub struct AnchorState {
    pub active: bool,
    pub file_id: u32,
    pub base_foff: u64,
    pub base_spos: u64,
    pub last_fend: u64,
    pub epoch_seq: u64,
    pub probe_consec: u8,
    pub last_probe_off: u64,
}

impl Default for AnchorState {
    fn default() -> Self {
        Self {
            active: false,
            file_id: 0,
            base_foff: 0,
            base_spos: 0,
            last_fend: 0,
            epoch_seq: 0,
            probe_consec: 0,
            last_probe_off: u64::MAX,
        }
    }
}

pub struct LiveEngine {
    pub ring: Arc<Ring>,
    pub xform: Mutex<TsXformState>,
    pub anchor: Mutex<AnchorState>,
    pub sm: Mutex<crate::engine::state_machine::StateMachine>,
}

impl Default for LiveEngine {
    fn default() -> Self {
        Self::new()
    }
}

impl LiveEngine {
    pub fn new() -> Self {
        Self {
            ring: Arc::new(Ring::new()),
            xform: Mutex::new(TsXformState::new()),
            anchor: Mutex::new(AnchorState::default()),
            sm: Mutex::new(crate::engine::state_machine::StateMachine::new()),
        }
    }

    pub fn flush(&self) {
        let seq = {
            let mut anchor = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
            anchor.active = false;
            anchor.epoch_seq = anchor.epoch_seq.wrapping_add(1);
            anchor.epoch_seq
        };
        self.xform
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .reset_epoch(seq);
        self.sm
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .reset_gap_watch();
    }

    pub fn snap_anchor(&self, target_spos: u64) -> u64 {
        let (ring_old, s_write) = {
            let rd = self.ring.lock();
            (rd.ring_old(), rd.s_write())
        };
        if s_write <= ring_old {
            return target_spos.min(s_write);
        }
        use crate::engine::snapper::{D12_BACK, D12_FWD, SCAR_SCAN, SCAR_SKIP};
        let start = target_spos.saturating_sub(D12_BACK).max(ring_old);
        let end = target_spos
            .saturating_add(D12_FWD + SCAR_SCAN + SCAR_SKIP)
            .min(s_write);
        if end <= start {
            return target_spos.min(s_write);
        }
        let win_len = (end - start) as usize;
        let mut win = vec![0u8; win_len];
        {
            let rd = self.ring.lock();
            rd.read_window(start, &mut win);
        }
        let base = start;
        let mut snap = crate::engine::snapper::d12_snap(&win, base, target_spos, s_write);
        let (pos, _) = crate::engine::snapper::validate_scar(&win, base, snap.pos, s_write);
        snap.pos = pos;
        crate::engine::snapper::leadback_guard(snap.pos, s_write)
    }
}

impl StreamBackend for LiveEngine {
    fn serve_live(&self, file_id: u32, foff: u64, dst: &mut [u8], _deadline_ms: u64) -> usize {
        if dst.is_empty() {
            return 0;
        }

        // 1. If ring is cold or empty, fill null and return.
        {
            let rd = self.ring.lock();
            if !rd.have_data() {
                drop(rd);
                crate::engine::ring::fill_null_at(dst, foff);
                return dst.len();
            }
        }

        // Capture ring state.
        let (ring_old0, cur_write0, now_ms0) = {
            let rd = self.ring.lock();
            (rd.ring_old(), rd.s_write(), rd.now_ms())
        };

        // Cold-open detection: a fresh file or channel switch wipes xform state.
        let was_cold = {
            let a = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
            !a.active || a.file_id != file_id
        };

        // Map foff to mapped_spos and decide re-anchor.
        let mut anchor = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
        let mapped_spos0 = if anchor.active {
            if foff >= anchor.base_foff {
                anchor.base_spos + (foff - anchor.base_foff)
            } else {
                anchor.base_spos.saturating_sub(anchor.base_foff - foff)
            }
        } else {
            foff
        };

        let channel_switched = anchor.active && anchor.file_id != file_id;
        let out_of_window = !anchor.active || mapped_spos0 < ring_old0 || mapped_spos0 > cur_write0;
        let mut need_reanchor = channel_switched || out_of_window;

        let (sm_decision, target_spos_override) = {
            let mut sm = self.sm.lock().unwrap_or_else(|e| e.into_inner());
            if was_cold {
                sm.note_open(now_ms0);
            }
            let ctx = SmCtx {
                anchor_valid: anchor.active,
                prev_fend: if anchor.active { anchor.last_fend } else { u64::MAX },
                probe_pos: if foff >= PROBE_ISOLATION_FOFF { Some(mapped_spos0) } else { None },
                s_write: cur_write0,
                ring_old: ring_old0,
            };
            let decision = sm.evaluate(foff, dst.len() as u64, now_ms0, &ctx);
            let resume_pos = if decision == SmDecision::ResumeAfterGap {
                crate::engine::state_machine::select_resume_anchor(mapped_spos0, ring_old0, cur_write0)
            } else {
                None
            };
            (decision, resume_pos)
        };

        match sm_decision {
            SmDecision::ProbeReject => {
                drop(anchor);
                let mut rd = self.ring.lock();
                rd.note_null(dst.len() as u64);
                drop(rd);
                crate::engine::ring::fill_null_at(dst, foff);
                return dst.len();
            }
            SmDecision::ConfirmedSeek => {
                need_reanchor = true;
            }
            SmDecision::ResumeAfterGap => {
                need_reanchor = true;
            }
            SmDecision::Continue => {}
        }

        if need_reanchor {
            let old_pos = if anchor.active { mapped_spos0 } else { foff };
            let old_fend = anchor.last_fend;
            let target_spos = if channel_switched
                || !anchor.active
                || sm_decision == SmDecision::ConfirmedSeek
            {
                cur_write0.saturating_sub(LEADBACK)
            } else if let Some(resume_pos) = target_spos_override {
                resume_pos
            } else {
                mapped_spos0
            };

            let new_spos = self.snap_anchor(target_spos);

            // Re-anchor.
            anchor.epoch_seq = anchor.epoch_seq.wrapping_add(1);
            let seq = anchor.epoch_seq;
            drop(anchor);

            if was_cold {
                self.xform
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .reset_epoch(seq);
            } else {
                // Warm re-anchor / resume splice: calibrate and begin splice epoch
                let r: XformRef = {
                    let win_base = old_pos
                        .saturating_sub(crate::engine::ts_xform::RING_SCAN_BACK)
                        .max(ring_old0);
                    let win_end = old_pos.saturating_add(188).min(cur_write0).max(win_base);
                    let win_len = (win_end - win_base).min(crate::engine::ts_xform::RING_SCAN_BACK + 188) as usize;
                    let mut win = vec![0u8; win_len];
                    {
                        let rd = self.ring.lock();
                        rd.read_window(win_base, &mut win);
                    }
                    self.xform
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .calibrate(old_pos, old_fend, &win, win_base, now_ms0)
                };
                let raw: RawSample = {
                    let win_base = new_spos.max(ring_old0);
                    let win_end = new_spos.saturating_add(1024 * 1024).min(cur_write0).max(win_base);
                    let win_len = (win_end - win_base).min(1024 * 1024) as usize;
                    let mut win = vec![0u8; win_len];
                    {
                        let rd = self.ring.lock();
                        rd.read_window(win_base, &mut win);
                    }
                    TsXformState::collect_raw(&win, win_base, new_spos, new_spos.saturating_add(1024 * 1024))
                };
                self.xform
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .begin_splice_epoch(seq, &r, &raw);
            }

            let mut anchor = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
            anchor.active = true;
            anchor.file_id = file_id;
            anchor.base_foff = crate::engine::ring::snap188(foff);
            anchor.base_spos = new_spos;
            anchor.last_fend = foff;
            drop(anchor);
        } else {
            drop(anchor);
        }

        // Recompute mapped_spos after possible re-anchor.
        let anchor = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
        let mapped_spos = if foff >= anchor.base_foff {
            anchor.base_spos + (foff - anchor.base_foff)
        } else {
            anchor.base_spos.saturating_sub(anchor.base_foff - foff)
        };
        drop(anchor);

        // Refresh ring state.
        let (mut ring_old, mut cur_write, _now) = {
            let rd = self.ring.lock();
            (rd.ring_old(), rd.s_write(), rd.now_ms())
        };

        // 3. Cooperative live-edge wait.
        let needed_end = mapped_spos.saturating_add(dst.len() as u64);
        if needed_end > cur_write {
            {
                let mut rd = self.ring.lock();
                rd.note_edge_wait();
            }
            let _ = self
                .ring
                .wait_edge(cur_write, Duration::from_millis(EDGE_WAIT_BUDGET_MS));
            let rd = self.ring.lock();
            cur_write = rd.s_write();
            ring_old = rd.ring_old();
        }

        // 4. Copy resident bytes into dst and null-pad remainder.
        let start = mapped_spos.max(ring_old);
        let dst_len_u64 = dst.len() as u64;
        let prefix = start.saturating_sub(mapped_spos).min(dst_len_u64) as usize;
        if prefix > 0 {
            crate::engine::ring::fill_null_at(&mut dst[..prefix], mapped_spos);
        }
        let rem_len = dst.len() - prefix;
        let avail = cur_write.saturating_sub(start).min(rem_len as u64) as usize;
        let to_copy = if avail < rem_len {
            let end_abs = start.saturating_add(avail as u64);
            let tail_phase = (end_abs % TS_PKT_SZ as u64) as usize;
            avail.saturating_sub(tail_phase)
        } else {
            avail
        };
        if to_copy > 0 {
            let rd = self.ring.lock();
            rd.read_at(start, &mut dst[prefix..prefix + to_copy]);
        }
        let filled = prefix + to_copy;
        let nulls = (prefix as u64) + (dst.len().saturating_sub(filled) as u64);
        if filled < dst.len() {
            crate::engine::ring::fill_null_at(&mut dst[filled..], mapped_spos + filled as u64);
        }

        // Telemetry.
        {
            let mut rd = self.ring.lock();
            rd.note_served(to_copy as u64);
            if nulls > 0 {
                rd.note_null(nulls);
                rd.note_starve();
            }
        }

        // 5. Calculate pacing sleep.
        let lead = cur_write.saturating_sub(mapped_spos);
        let sleep_ms = {
            let mut rd = self.ring.lock();
            let now = rd.now_ms();
            rd.pace_note(dst.len() as u64, lead, now)
        };

        // 6. Apply Caminho C continuity transform.
        let ring_for_full = self.ring.clone();
        let valid_lo = start;
        let valid_hi = start + to_copy as u64;
        let mut full_at = move |abs: u64, pkt: &mut [u8; TS_PKT_SZ]| -> bool {
            if abs < valid_lo || abs.saturating_add(TS_PKT_SZ as u64) > valid_hi {
                return false;
            }
            let rd = ring_for_full.lock();
            rd.read_at(abs, pkt);
            true
        };
        {
            let mut x = self.xform.lock().unwrap_or_else(|e| e.into_inner());
            if to_copy > 0 {
                x.apply_block(&mut dst[prefix..filled], start, true, &mut full_at);
            } else {
                x.invalidate_tail();
            }
            let fend = foff.saturating_add(dst.len() as u64);
            let now_ms = self.ring.lock().now_ms();
            x.note_ckpt(fend, now_ms);
            let mut a = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
            a.last_fend = fend;
        }

        // 7. Sleep unlocked if sleep_ms > 0 (clamped <= 50ms).
        if sleep_ms > 0 {
            std::thread::sleep(Duration::from_millis(sleep_ms.min(MAX_SLEEP_SLICE_MS)));
        }

        dst.len()
    }

    fn serve_vod(
        &self,
        _url: &str,
        _foff: u64,
        dst: &mut [u8],
        _deadline_ms: u64,
    ) -> Result<usize, std::io::Error> {
        crate::engine::ring::fill_null(dst);
        Ok(dst.len())
    }
}