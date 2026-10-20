//! Scaffold draft engine (`LiveEngine`) preserved from the original
//! `src/engine.rs`. Kept as a reference implementation and as the backend
//! currently wired in `main.rs`; the production engine root is
//! `engine::MediaEngine` (see `super`). This module deliberately keeps the
//! original code shape — its submodule declarations now live in the parent.

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::contract::{StreamBackend, LEADBACK, PROBE_ISOLATION_FOFF};
use crate::engine::ring::{Ring, EDGE_WAIT_BUDGET_MS, MAX_SLEEP_SLICE_MS, MIN_STREAM_START};
use crate::engine::state_machine::{OvwCtx, SmCtx, SmDecision};
use crate::observability::{Observer, Reason};
mod observation;

use crate::engine::ts_xform::{TsXformState, XformRef, RawSample};

mod anchor;
mod commit;
#[cfg(test)]
mod phase_b_tests;
#[cfg(test)]
mod test_seam;
pub use anchor::AnchorState;

pub struct LiveEngine {
    pub ring: Arc<Ring>,
    pub xform: Mutex<TsXformState>,
    pub anchor: Mutex<AnchorState>,
    pub sm: Mutex<crate::engine::state_machine::StateMachine>,
    pub obs: Observer,
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
            obs: Observer::disabled(),
        }
    }

    pub fn flush(&self) {
        // Same transaction order as reanchor/commit: xform -> anchor -> ring.
        // A reanchor cannot start between the invalidation and the reset.
        let mut x = self.xform.lock().unwrap_or_else(|e| e.into_inner());
        let mut anchor = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
        anchor.active = false;
        anchor.last_fend = u64::MAX;
        anchor.epoch_seq = anchor.epoch_seq.wrapping_add(1);
        anchor.gen = anchor.gen.wrapping_add(1);
        let seq = anchor.epoch_seq;
        #[cfg(test)]
        test_seam::park_flush_after_anchor();
        self.ring.lock().clear();
        self.ring.notify_all();
        x.reset_epoch(seq);
        self.sm.lock().unwrap_or_else(|e| e.into_inner()).reset_probe_state();
        drop(anchor);
        drop(x);
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

    /// NEW (TASK-002): noncommit NULLs, no progress; `observe_ms` = R5 observation.
    fn abandon_overwritten(&self, dst: &mut [u8], foff: u64, observe_ms: Option<u64>) -> usize {
        if let Some(now_ms) = observe_ms {
            let mut x = self.xform.lock().unwrap_or_else(|e| e.into_inner());
            x.invalidate_tail();
            let mut a = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
            a.last_read_ms = now_ms;
        }
        self.ring.lock().note_null(dst.len() as u64);
        crate::engine::ring::fill_null_at(dst, foff);
        dst.len()
    }
}

impl StreamBackend for LiveEngine {
    fn on_open(&self) {
        let mut anchor = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
        anchor.active = false;
        anchor.last_fend = u64::MAX;
        anchor.gen = anchor.gen.wrapping_add(1);
        let mut sm = self.sm.lock().unwrap_or_else(|e| e.into_inner());
        sm.reset_probe_state();
    }

    fn serve_live(&self, file_id: u32, foff: u64, dst: &mut [u8], _deadline_ms: u64) -> usize {
        if dst.is_empty() {
            let mut sm = self.sm.lock().unwrap_or_else(|e| e.into_inner());
            sm.clear_overwritten_pending(); // R2: empty requests never progress.
            return 0;
        }

        let (snapshot_gen, mut delivery_gen) = { let g = self.anchor.lock().unwrap_or_else(|e| e.into_inner()).gen; (g, g) };
        // 1. If ring is cold or empty, wait or fill null and return.
        {
            let mut cur_write = self.ring.lock().s_write();
            if cur_write < MIN_STREAM_START {
                let t0 = Instant::now();
                let _ = self.ring.wait_edge(MIN_STREAM_START, Duration::from_millis(1200));
                cur_write = self.ring.lock().s_write();
                if cur_write < MIN_STREAM_START {
                    crate::engine::ring::fill_null_at(dst, foff);
                    let ms = t0.elapsed().as_millis() as u64;
                    self.obs_cold(foff, dst.len(), ms, file_id);
                    return dst.len();
                }
            }
        }

        // Capture ring state.
        let (ring_old0, cur_write0, now_ms0) = {
            let rd = self.ring.lock();
            (rd.ring_old(), rd.s_write(), rd.now_ms())
        };

        // Cold-open detection: a fresh file or channel switch wipes xform state.
        let (was_cold, anchor_last_fend) = {
            let a = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
            (!a.active || a.file_id != file_id, a.last_fend)
        };

        // Map foff to mapped_spos and decide re-anchor.
        let anchor = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
        let mapped_spos0 = if anchor.active {
            if foff >= anchor.base_foff {
                anchor.base_spos + (foff - anchor.base_foff)
            } else {
                anchor.base_spos.saturating_sub(anchor.base_foff - foff)
            }
        } else {
            foff
        };

        let is_adjacent = anchor.active && anchor.last_fend != u64::MAX && foff == anchor.last_fend;
        let channel_switched = anchor.active && anchor.file_id != file_id;
        let seq_behind_ring = is_adjacent && mapped_spos0 < ring_old0;
        let is_washed_out = mapped_spos0 < ring_old0;
        let large_backward_jump = anchor.active
            && anchor.last_fend != u64::MAX
            && anchor.last_fend.saturating_sub(foff) > 32 * 1024 * 1024;
        let restart_at_zero = anchor.active
            && foff == 0
            && (now_ms0.saturating_sub(anchor.last_read_ms) > 1500 || anchor.last_fend > 131072);
        let is_restart = restart_at_zero || (large_backward_jump && is_washed_out);
        let mut need_reanchor = !anchor.active || channel_switched || seq_behind_ring || is_restart;
        let was_cold = was_cold || is_restart;

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

        // NEW (TASK-002): feed the bounded overwritten tracker (all reads).
        let ovw_base = sm_decision == SmDecision::Continue && !need_reanchor && !is_adjacent;
        let ovw_conf = {
            let mut sm = self.sm.lock().unwrap_or_else(|e| e.into_inner());
            sm.note_overwritten_request(OvwCtx {
                file_id,
                epoch: anchor.epoch_seq,
                foff,
                len: dst.len() as u64,
                now_ms: now_ms0,
                mapped_start: mapped_spos0,
                ring_old: ring_old0,
                base_eligible: ovw_base,
            })
        };

        match sm_decision {
            // TASK-005: a probe or source failure is never gadget authority.
            // The watchdog marker is an explicit operator request only.
            SmDecision::ProbeReject => {
                drop(anchor);
                return self.obs_abort(dst, foff, Reason::ProbeReject, None);
            }
            SmDecision::ConfirmedSeek => {
                need_reanchor = true;
            }
            SmDecision::ResumeAfterGap => {
                need_reanchor = true;
            }
            SmDecision::Continue => need_reanchor |= ovw_conf,
        }

        if need_reanchor {
            let old_fend = anchor.last_fend;
            // NEW (TASK-002): checked symmetric map of the successful end (R4).
            let old_pos = if ovw_conf && old_fend >= anchor.base_foff {
                anchor.base_spos.checked_add(old_fend - anchor.base_foff)
            } else if ovw_conf {
                anchor.base_spos.checked_sub(anchor.base_foff - old_fend)
            } else {
                Some(mapped_spos0)
            };
            let Some(old_pos) = old_pos else {
                drop(anchor);
                self.obs_noncommit(foff, dst.len() as u64, Reason::MappingUnderflow);
                return self.obs_abort(dst, foff, Reason::MappingUnderflow, Some(now_ms0));
            };
            let eff_lead = if cur_write0 > LEADBACK {
                LEADBACK
            } else {
                cur_write0.saturating_sub(2 * 1024 * 1024)
            };
            let target_spos = if channel_switched
                || !anchor.active
                || sm_decision == SmDecision::ConfirmedSeek
                || is_restart
                || seq_behind_ring
                || ovw_conf
            {
                cur_write0.saturating_sub(eff_lead)
            } else if sm_decision == SmDecision::ResumeAfterGap {
                if let Some(resume_pos) = target_spos_override {
                    resume_pos
                } else {
                    cur_write0.saturating_sub(eff_lead)
                }
            } else {
                mapped_spos0
            };

            let new_spos = self.snap_anchor(target_spos);

            // Re-anchor: the epoch advances only after the recovery reference
            // is validated. The whole transaction (generation check,
            // transformer update, anchor publication) is atomic under
            // xform+anchor, so a flush can never land between calibration and
            // publication, and a stale read cannot touch a newer transformer.
            let seq = anchor.epoch_seq.wrapping_add(1);
            drop(anchor);

            #[cfg(test)]
            test_seam::park_before_calibration();

            {
                let mut x = self.xform.lock().unwrap_or_else(|e| e.into_inner());
                let mut anchor = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
                if anchor.gen != snapshot_gen {
                    // A flush/open completed while this read was preparing.
                    // Touch neither the transformer nor the anchor.
                    drop(anchor);
                    drop(x);
                    return self.obs_abort_stale(dst, foff);
                }

                if was_cold {
                    x.reset_epoch(seq);
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
                        let rr = x.calibrate(old_pos, old_fend, &win, win_base, now_ms0);
                        if ovw_conf && !rr.valid {
                            // NEW (TASK-002): no usable reference -> noncommit.
                            self.obs_noncommit(foff, dst.len() as u64, Reason::ReferenceInvalid);
                            drop(anchor);
                            drop(x);
                            return self.obs_abort(dst, foff, Reason::ReferenceInvalid, Some(now_ms0));
                        }
                        rr
                    };
                    self.obs_calibration(r.valid);
                    let raw: RawSample = {
                        let win_base = new_spos.max(ring_old0);
                        let win_end = new_spos
                            .saturating_add(1024 * 1024)
                            .min(cur_write0)
                            .max(win_base);
                        let win_len = (win_end - win_base).min(1024 * 1024) as usize;
                        let mut win = vec![0u8; win_len];
                        {
                            let rd = self.ring.lock();
                            rd.read_window(win_base, &mut win);
                        }
                        TsXformState::collect_raw(
                            &win,
                            win_base,
                            new_spos,
                            new_spos.saturating_add(1024 * 1024),
                        )
                    };
                    x.begin_splice_epoch(seq, &r, &raw);
                }

                anchor.active = true;
                anchor.gen = anchor.gen.wrapping_add(1);
                delivery_gen = anchor.gen;
                anchor.file_id = file_id;
                anchor.epoch_seq = seq;
                anchor.base_foff = crate::engine::ring::snap188(foff);
                anchor.base_spos = new_spos;
                anchor.last_fend = foff;
                anchor.last_read_ms = now_ms0;
                let why = observation::commit_why(sm_decision, ovw_conf, channel_switched, is_restart);
                self.obs_commit(seq, anchor.base_foff, new_spos, why);
            }
        } else {
            drop(anchor);
        }

        // Recompute mapped_spos after possible re-anchor.
        let (mapped_spos, epoch0) = {
            let anchor = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
            let ms = if foff >= anchor.base_foff {
                anchor.base_spos + (foff - anchor.base_foff)
            } else {
                anchor.base_spos.saturating_sub(anchor.base_foff - foff)
            };
            (ms, anchor.epoch_seq)
        };

        // 3 & 4. Fragment delivery loop with edge wait.
        let budget_end = Instant::now() + Duration::from_millis(EDGE_WAIT_BUDGET_MS);
        let (mut filled, mut to_copy, mut prefix) = (0usize, 0usize, 0usize);
        let mut first_copy_pos = mapped_spos;
        let mut wait_ms = 0u64;
        let mut starved = false;
        while filled < dst.len() {
            let pos = mapped_spos + filled as u64;
            let (ring_old, s_write) = {
                let rd = self.ring.lock();
                (rd.ring_old(), rd.s_write())
            };
            let left = (dst.len() - filled) as u64;
            if pos < ring_old {
                let n = (ring_old - pos).min(left) as usize;
                crate::engine::ring::fill_null_at(&mut dst[filled..filled + n], pos);
                filled += n;
                prefix += n;
                continue;
            }
            if pos >= s_write {
                let now = Instant::now();
                let mut waited = false;
                if now < budget_end {
                    let remain = budget_end - now;
                    self.ring.lock().note_edge_wait();
                    let t0 = Instant::now();
                    waited = self.ring.wait_edge(pos + 1, remain);
                    wait_ms = wait_ms.saturating_add(t0.elapsed().as_millis() as u64);
                }
                if !waited {
                    crate::engine::ring::fill_null_at(&mut dst[filled..], pos);
                    let mut rd = self.ring.lock();
                    rd.note_starve();
                    rd.note_null((dst.len() - filled) as u64);
                    self.obs_underrun((dst.len() - filled) as u64);
                    starved = true;
                    break;
                }
                continue;
            }
            let n = (s_write - pos).min(left) as usize;
            {
                // The residency snapshot above released the ring lock; the
                // feeder may have overwritten this range in the meantime.
                // Re-check and copy in one critical section so stale bytes are
                // never served (ITER#2 fix #4). On overwrite, stop the
                // contiguous run and NULL-fill the remainder, exactly like the
                // `pos < ring_old` branch.
                let mut rd = self.ring.lock();
                if !rd.copy_resident(pos, &mut dst[filled..filled + n]) {
                    rd.note_starve();
                    rd.note_null((dst.len() - filled) as u64);
                    drop(rd);
                    crate::engine::ring::fill_null_at(&mut dst[filled..], pos);
                    self.obs_underrun((dst.len() - filled) as u64);
                    starved = true;
                    break;
                }
            }
            if to_copy == 0 {
                first_copy_pos = pos;
            }
            filled += n;
            to_copy += n;
        }

        // Telemetry.
        {
            let mut rd = self.ring.lock();
            rd.note_served(to_copy as u64);
        }
        let why = [Reason::Sampled, Reason::Cold][was_cold as usize];
        let why = if starved { Reason::Starve } else { why };

        // 5. Calculate pacing sleep (only forward sequential reads).
        let sleep_ms = if foff >= anchor_last_fend {
            let (cur_write, now) = {
                let rd = self.ring.lock();
                (rd.s_write(), rd.now_ms())
            };
            let lead = cur_write.saturating_sub(mapped_spos);
            let mut rd = self.ring.lock();
            rd.pace_note(dst.len() as u64, lead, now)
        } else {
            0
        };

        // 6. Pacing precedes the commit: a flush during this sleep is still gated.
        let mut need = sleep_ms;
        while need > 0 {
            let s = need.min(MAX_SLEEP_SLICE_MS);
            std::thread::sleep(Duration::from_millis(s));
            need -= s;
        }
        #[cfg(test)]
        test_seam::park_before_commit();
        // 7. Apply Caminho C transform, gated on the generation captured
        // before delivery; a stale read must not commit checkpoint/last_fend.
        let (generation_ok, delivered) = self.commit_delivery(
            dst,
            foff,
            prefix,
            to_copy,
            first_copy_pos,
            delivery_gen,
            epoch0,
            now_ms0,
        );

        let why = if generation_ok {
            why
        } else {
            Reason::ReferenceInvalid
        };
        self.obs_delivered(
            foff,
            mapped_spos,
            delivered,
            dst.len(),
            wait_ms,
            file_id,
            why,
            epoch0,
        );
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
