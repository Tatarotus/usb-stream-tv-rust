//! TASK-004: delivery transform + generation-gated commit.
//!
//! Extracted from `serve_live` so `live_engine.rs` stays within the 500-line
//! budget. The invariant is unchanged: the transform and the `last_fend`
//! checkpoint are published only if the anchor generation captured before
//! delivery is still current.

use crate::contract::TS_PKT_SZ;

use super::LiveEngine;

impl LiveEngine {
    /// Applies the continuity transform and commits progress only when
    /// `gen0`/`epoch0` are still the published generation.
    ///
    /// Returns `(generation_ok, delivered_bytes)`. On a stale generation the
    /// destination is overwritten with NULL packets and no checkpoint or
    /// `last_fend` is written.
    #[allow(clippy::too_many_arguments)]
    pub(super) fn commit_delivery(
        &self,
        dst: &mut [u8],
        foff: u64,
        prefix: usize,
        to_copy: usize,
        first_copy_pos: u64,
        gen0: u64,
        epoch0: u64,
        now_ms0: u64,
    ) -> (bool, usize) {
        let ring_for_full = self.ring.clone();
        let mut full_at = move |abs: u64, pkt: &mut [u8; TS_PKT_SZ]| -> bool {
            let rd = ring_for_full.lock();
            if !rd.contains(abs, TS_PKT_SZ as u64) {
                return false;
            }
            rd.read_at(abs, pkt);
            true
        };

        let generation_ok;
        {
            let mut x = self.xform.lock().unwrap_or_else(|e| e.into_inner());
            let mut a = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
            generation_ok = a.gen == gen0 && a.active && a.epoch_seq == epoch0;
            if to_copy > 0 && generation_ok {
                x.apply_block(
                    &mut dst[prefix..prefix + to_copy],
                    first_copy_pos,
                    true,
                    &mut full_at,
                );
            } else if generation_ok {
                // Only the current generation may drop its own carry. A stale
                // read must never invalidate a newer transformer.
                x.invalidate_tail();
            }
            let now_ms = self.ring.lock().now_ms();
            let fend = foff.saturating_add(dst.len() as u64);
            // NEW (TASK-002): successful progress is gated on real bytes.
            if to_copy > 0 && generation_ok {
                x.note_ckpt(fend, now_ms);
                a.last_fend = fend;
            }
            if generation_ok {
                a.last_read_ms = now_ms0;
            }
        }

        if !generation_ok {
            crate::engine::ring::fill_null_at(dst, foff);
            self.ring.lock().note_null(dst.len() as u64);
        }
        (generation_ok, if generation_ok { to_copy } else { 0 })
    }
}
