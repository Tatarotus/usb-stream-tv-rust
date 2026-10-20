//! Fragment delivery loop of [`super::LiveEngine::serve_live`].
//!
//! Extracted from `serve_live` so `live_engine.rs` keeps its 500-line budget
//! (ITER#2 fix #4 added the residency re-check inside the copy step). The
//! behaviour is unchanged: overwritten ranges are NULL-filled, an edge point
//! waits within the existing budget, and a resident range is residency-checked
//! and copied in a single critical section so stale bytes can never be served.

use std::time::{Duration, Instant};

use crate::engine::ring::{fill_null_at, EDGE_WAIT_BUDGET_MS};

use super::LiveEngine;

/// Outcome of the fragment delivery loop.
pub(super) struct Delivery {
    /// Bytes actually copied from the ring (payload, not NULL filler).
    pub(super) to_copy: usize,
    /// Bytes emitted before the first copied byte (NULL prefix).
    pub(super) prefix: usize,
    /// Absolute ring position of the first copied byte.
    pub(super) first_copy_pos: u64,
    /// Total time spent waiting at the live edge.
    pub(super) wait_ms: u64,
    /// True when the loop stopped short and NULL-filled the remainder.
    pub(super) starved: bool,
}

impl LiveEngine {
    /// Fills `dst` from the ring starting at `mapped_spos`, waiting at the live
    /// edge within [`EDGE_WAIT_BUDGET_MS`] and NULL-filling anything that is
    /// behind the ring or not yet written.
    pub(super) fn deliver_fragments(&self, dst: &mut [u8], mapped_spos: u64) -> Delivery {
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
                fill_null_at(&mut dst[filled..filled + n], pos);
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
                    fill_null_at(&mut dst[filled..], pos);
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
                    fill_null_at(&mut dst[filled..], pos);
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
        Delivery {
            to_copy,
            prefix,
            first_copy_pos,
            wait_ms,
            starved,
        }
    }
}
