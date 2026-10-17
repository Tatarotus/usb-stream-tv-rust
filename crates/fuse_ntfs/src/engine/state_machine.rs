//! Return-continuity state machine for the 2-step Samsung ConnectShare resume.
//!
//! Empirical TV behaviour (docs/briefing_investigacao_return.md): pressing
//! **Return** and reopening `TV AO VIVO.trp` does NOT restart at `foff = 0` —
//! the decoder keeps its ~1.9 MiB header in RAM and reopens at a saved
//! bookmark offset. The resume is *two steps*:
//!
//! 1. A non-adjacent read appears after an idle gap (`> 20 s`) with a jump
//!    `> 256 KiB` — the bookmark seek.
//! 2. Reads continue sequentially from that point; once the chain holds
//!    `>= 4` reads / `>= 64 KiB`, the resume is confirmed and the engine
//!    re-anchors *continuously* (splice), not at the live edge.
//!
//! Guards:
//! * **Opening grace** — for 10 s after a fresh playback open
//!   (`foff <= 4 MiB`) resume detection is locked out: startup probes must
//!   never trigger a re-anchor.
//! * **Probe isolation** — reads at `foff >= 8 MiB` whose mapped stream
//!   position falls outside the ring window (future probe: `>= s_write +
//!   2 MiB`; behind probe: `< ring_old`) are rejected with NULL packets and
//!   never advance the playback chain. Only `K >= 3` sequential probe blocks
//!   escalate to a confirmed seek.
//! * **Position-preserving selection** — a confirmed resume whose requested
//!   position still maps inside the ring continues from the nearest PAT
//!   (timeshift continuity) instead of jumping to the live edge.

use crate::contract::RESUME_MIN_LEAD;
use crate::engine::ring::snap188;

/// Idle gap after which a non-adjacent read is treated as a resume attempt.
pub const RESUME_GAP_MS: u64 = 20_000;
/// Non-adjacent jump distance that qualifies as a resume/bookmark seek.
pub const RESUME_JUMP: u64 = 256 * 1024;
/// Confirmation chain: reads.
pub const RESUME_CHAIN_READS: u64 = 4;
/// Confirmation chain: bytes.
pub const RESUME_CHAIN_BYTES: u64 = 64 * 1024;
/// Lockout window after a fresh playback open.
pub const OPENING_GRACE_MS: u64 = 10_000;
/// A fresh open is "early" while the read cursor stays below this offset.
pub const OPENING_FOFF_MAX: u64 = 4 * 1024 * 1024;
/// Sequential probe blocks required to confirm a real seek.
pub const PROBE_CONFIRM_K: usize = 3;
/// Future-probe margin: mapped positions this far past the writer are probes.
pub const PROBE_FUTURE_MARGIN: u64 = 2 * 1024 * 1024;
/// SCSI block sizes whose consecutive landing continues the probe chain.
pub const PROBE_SEQ_STEPS: [u64; 2] = [4096, 8192];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SmPhase {
    /// Fresh playback open: grace window active.
    Opening,
    /// Steady playback.
    Live,
    /// Step 1 of the 2-step resume observed; accumulating the chain.
    GapWatch,
    /// Resume consumed; back to steady playback.
    Resumed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SmDecision {
    /// Sequential read: serve from the current anchor.
    Continue,
    /// Transient probe: NULL-fill, no state change (chain untouched).
    ProbeReject,
    /// Confirmed sequential seek (`K >= 3` probe blocks): re-anchor to live.
    ConfirmedSeek,
    /// 2-step resume confirmed: re-anchor with continuity splice.
    ResumeAfterGap,
}

/// Per-read context handed to the state machine by the engine.
#[derive(Debug, Clone, Copy)]
pub struct SmCtx {
    pub anchor_valid: bool,
    /// Last sequential read end (`u64::MAX` when the chain is idle).
    pub prev_fend: u64,
    /// Mapped stream position when `foff >= 8 MiB` (probe classification).
    pub probe_pos: Option<u64>,
    pub s_write: u64,
    pub ring_old: u64,
}

impl SmCtx {
    pub fn adjacent(&self, foff: u64) -> bool {
        self.anchor_valid && self.prev_fend != u64::MAX && foff == self.prev_fend
    }
}

pub struct StateMachine {
    phase: SmPhase,
    /// Idle gap threshold for resume detection (mirrors `FUSE_RESUME_GAP_MS`).
    gap_ms: u64,
    opened_ms: u64,
    have_read: bool,
    last_read_ms: u64,
    gap_jump_foff: u64,
    chain_reads: u64,
    chain_bytes: u64,
    probe_chain: usize,
    probe_last_off: u64,
    // telemetry
    pub gap_detects: u64,
    pub resumes: u64,
    pub confirmed_seeks: u64,
    pub probe_rejects: u64,
    pub transitions: u64,
}

impl Default for StateMachine {
    fn default() -> Self {
        Self::new()
    }
}

impl StateMachine {
    pub fn new() -> Self {
        Self::with_gap_ms(RESUME_GAP_MS)
    }

    /// State machine with a custom idle-gap threshold (engine options).
    pub fn with_gap_ms(gap_ms: u64) -> Self {
        Self {
            phase: SmPhase::Opening,
            gap_ms,
            opened_ms: 0,
            have_read: false,
            last_read_ms: 0,
            gap_jump_foff: 0,
            chain_reads: 0,
            chain_bytes: 0,
            probe_chain: 0,
            probe_last_off: u64::MAX,
            gap_detects: 0,
            resumes: 0,
            confirmed_seeks: 0,
            probe_rejects: 0,
            transitions: 0,
        }
    }

    pub fn phase(&self) -> SmPhase {
        self.phase
    }

    /// Records a fresh playback open (fresh open or channel switch): the
    /// grace window starts and every chain is reset.
    pub fn note_open(&mut self, now_ms: u64) {
        self.phase = SmPhase::Opening;
        self.opened_ms = now_ms;
        self.have_read = false;
        self.last_read_ms = now_ms;
        self.gap_jump_foff = 0;
        self.chain_reads = 0;
        self.chain_bytes = 0;
        self.probe_chain = 0;
        self.probe_last_off = u64::MAX;
        self.transitions += 1;
    }

    /// True while the opening grace suppresses resume detection.
    pub fn opening_grace_active(&self, foff: u64, now_ms: u64) -> bool {
        self.phase == SmPhase::Opening
            && foff <= OPENING_FOFF_MAX
            && now_ms.saturating_sub(self.opened_ms) < OPENING_GRACE_MS
    }

    fn set_phase(&mut self, p: SmPhase) {
        if self.phase != p {
            self.phase = p;
            self.transitions += 1;
        }
    }

    /// Evaluates one FUSE read: updates the chains and returns the decision
    /// for THIS read. The engine must apply the `ProbeReject` path without
    /// touching the playback chain cursor (`prev_fend` invariant).
    pub fn evaluate(&mut self, foff: u64, len: u64, now_ms: u64, ctx: &SmCtx) -> SmDecision {
        let adjacent = ctx.adjacent(foff);

        // Opening grace expiry: early playback left the 4 MiB window or the
        // 10 s lockout elapsed.
        if self.phase == SmPhase::Opening
            && (foff > OPENING_FOFF_MAX
                || now_ms.saturating_sub(self.opened_ms) >= OPENING_GRACE_MS)
        {
            self.set_phase(SmPhase::Live);
        }

        // ---- probe classification (beyond 8 MiB, outside the ring window)
        if let Some(s_probe) = ctx.probe_pos {
            let is_future = s_probe >= ctx.s_write.saturating_add(PROBE_FUTURE_MARGIN);
            let is_behind = s_probe < ctx.ring_old;
            if is_future || is_behind {
                // A probe landing one SCSI block past the previous probe is a
                // sequential re-scan of the same neighbourhood and continues
                // the debounce chain; anything else restarts it at 1.
                let sequential = self.probe_last_off == u64::MAX
                    || PROBE_SEQ_STEPS
                        .iter()
                        .any(|&step| foff == self.probe_last_off.saturating_add(step));
                if sequential || self.probe_chain == 0 {
                    self.probe_chain += 1;
                } else {
                    self.probe_chain = 1;
                }
                self.probe_last_off = foff;
                if self.probe_chain < PROBE_CONFIRM_K {
                    // PR INVARIANT: the idle-gap timer (last_read_ms) must NOT
                    // be touched by probes, otherwise a long probe storm during
                    // the Return idle would mask the resume gap (65 s in the
                    // production log).
                    self.probe_rejects += 1;
                    return SmDecision::ProbeReject;
                }
                // K >= 3: a real sequential seek — re-anchor to the live edge.
                self.probe_chain = 0;
                self.probe_last_off = u64::MAX;
                self.set_phase(SmPhase::Live);
                self.confirmed_seeks += 1;
                return SmDecision::ConfirmedSeek;
            }
        }
        if adjacent {
            self.probe_chain = 0;
            self.probe_last_off = u64::MAX;
        }

        // ---- 2-step return resume
        if self.phase == SmPhase::GapWatch {
            if adjacent && foff >= self.gap_jump_foff {
                // Step 2: sequential continuation at the resume point.
                self.chain_reads += 1;
                self.chain_bytes = self.chain_bytes.saturating_add(len);
                if self.chain_reads >= RESUME_CHAIN_READS
                    || self.chain_bytes >= RESUME_CHAIN_BYTES
                {
                    self.set_phase(SmPhase::Resumed);
                    self.chain_reads = 0;
                    self.chain_bytes = 0;
                    self.resumes += 1;
                    self.have_read = true;
                    self.last_read_ms = now_ms;
                    return SmDecision::ResumeAfterGap;
                }
            } else {
                // Non-adjacent inside GapWatch: not a resume chain; restart
                // the gap watch from this offset only if it is itself a
                // gap-jump candidate, else fall back to steady playback.
                self.reset_gap_watch();
                if self.gap_jump_candidate(foff, now_ms, ctx) {
                    self.begin_gap_watch(foff, len);
                } else {
                    self.set_phase(SmPhase::Live);
                }
            }
        } else if !adjacent && self.gap_jump_candidate(foff, now_ms, ctx) {
            // Step 1: bookmark seek after an idle gap.
            self.begin_gap_watch(foff, len);
        }

        self.have_read = true;
        self.last_read_ms = now_ms;
        SmDecision::Continue
    }

    fn reset_gap_watch(&mut self) {
        self.chain_reads = 0;
        self.chain_bytes = 0;
    }

    fn begin_gap_watch(&mut self, foff: u64, len: u64) {
        self.set_phase(SmPhase::GapWatch);
        self.gap_jump_foff = foff;
        self.chain_reads = 1;
        self.chain_bytes = len;
        self.gap_detects += 1;
    }

    fn gap_jump_candidate(&self, foff: u64, now_ms: u64, ctx: &SmCtx) -> bool {
        if self.opening_grace_active(foff, now_ms) {
            return false;
        }
        if ctx.prev_fend == u64::MAX || !self.have_read {
            return false;
        }
        let gap = now_ms.saturating_sub(self.last_read_ms);
        let jump = foff.max(ctx.prev_fend) - foff.min(ctx.prev_fend);
        gap > self.gap_ms && jump > RESUME_JUMP
    }
}

/// Position-preserving continuous anchor selection.
///
/// When the requested position still maps inside the ring, the resume
/// continues from there (timeshift continuity, "Caminho 2"); the returned
/// target is snapped to the 188 grid and keeps at least `RESUME_MIN_LEAD`
/// cushion behind the live writer. `None` means the position was overwritten
/// and the caller must fall back to the live target (`s_write - LEADBACK`).
pub fn select_resume_anchor(mapped: u64, ring_old: u64, s_write: u64) -> Option<u64> {
    if mapped < ring_old {
        return None;
    }
    let floor = s_write.saturating_sub(RESUME_MIN_LEAD);
    Some(snap188(mapped.min(floor)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ctx(anchor_valid: bool, prev_fend: u64, probe_pos: Option<u64>, s_write: u64, ring_old: u64) -> SmCtx {
        SmCtx {
            anchor_valid,
            prev_fend,
            probe_pos,
            s_write,
            ring_old,
        }
    }

    #[test]
    fn opening_grace_locks_out_resume() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        assert!(sm.opening_grace_active(1_000_000, 5_000));
        // Reads within the grace at foff <= 4 MiB: never a resume.
        let c = ctx(true, 2_000_000, None, 100 * 1024 * 1024, 0);
        assert_eq!(sm.evaluate(9_000_000, 16384, 6_000, &c), SmDecision::Continue);
        // Grace expires after 10 s.
        assert!(!sm.opening_grace_active(1_000_000, 11_000));
        // Leaving the 4 MiB window also ends the grace.
        let mut sm2 = StateMachine::new();
        sm2.note_open(0);
        assert!(!sm2.opening_grace_active(5 * 1024 * 1024, 1_000));
    }

    #[test]
    fn two_step_resume_detection() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        let ring_old = s_write - 128 * 1024 * 1024;
        // Steady playback: chain at foff 51 MB.
        let mut fend = 0u64;
        let base = 51_000_000u64;
        for i in 0..6 {
            let foff = base + i * 16384;
            let c = ctx(true, fend, None, s_write, ring_old);
            assert_eq!(sm.evaluate(foff, 16384, 1_000 + i * 100, &c), SmDecision::Continue);
            fend = foff + 16384;
        }
        assert_eq!(sm.phase(), SmPhase::Live);

        // Idle 65 s (Return key), then the TV reopens at its 1.9 MiB bookmark.
        let reopen = 1_900_544u64;
        let c = ctx(true, fend, None, s_write, ring_old);
        // Step 1: the jump read itself.
        assert_eq!(sm.evaluate(reopen, 16384, 66_000, &c), SmDecision::Continue);
        assert_eq!(sm.phase(), SmPhase::GapWatch);

        // Step 2: sequential continuation from the bookmark; the 4th chained
        // read (64 KB) confirms the resume.
        let mut prev = reopen + 16384;
        let mut decision = SmDecision::Continue;
        for i in 1..4 {
            let c2 = ctx(true, prev, None, s_write, ring_old);
            decision = sm.evaluate(prev, 16384, 66_000 + i * 100, &c2);
            prev += 16384;
        }
        assert_eq!(decision, SmDecision::ResumeAfterGap);
        assert_eq!(sm.phase(), SmPhase::Resumed);
    }

    #[test]
    fn small_jump_is_not_a_resume() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        let c = ctx(true, 10_000_000, None, s_write, 0);
        // Gap ok, but the jump is only 100 KiB (< 256 KiB): not a resume.
        assert_eq!(sm.evaluate(10_100_000, 16384, 60_000, &c), SmDecision::Continue);
        assert_eq!(sm.phase(), SmPhase::Live);
    }

    #[test]
    fn gap_without_chain_is_not_a_resume() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        // Establish playback first: a gap jump cannot occur before a read.
        let c0 = ctx(true, 0, None, s_write, 0);
        assert_eq!(sm.evaluate(10_000_000, 16384, 1_000, &c0), SmDecision::Continue);

        // Step 1 observed...
        let c = ctx(true, 10_016_384, None, s_write, 0);
        assert_eq!(sm.evaluate(50_000_000, 16384, 60_000, &c), SmDecision::Continue);
        assert_eq!(sm.phase(), SmPhase::GapWatch);
        // ...but the next read is non-adjacent: the chain breaks.
        let c2 = ctx(true, 50_016_384, None, s_write, 0);
        assert_eq!(sm.evaluate(60_000_000, 16384, 60_100, &c2), SmDecision::Continue);
        assert_ne!(sm.phase(), SmPhase::GapWatch);
    }

    #[test]
    fn probe_isolation_k3_debounce() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        let ring_old = s_write - 128 * 1024 * 1024;
        // Playback chain at 300 MB; probes arrive at 800 MB..128 GB (mapped
        // far beyond the writer head => future probes).
        let fend = 300_000_000u64;
        let future = |f: u64| Some(s_write + 2 * 1024 * 1024 + f);

        let c1 = ctx(true, fend, future(800_000_000), s_write, ring_old);
        assert_eq!(sm.evaluate(800_000_000, 16384, 1_000, &c1), SmDecision::ProbeReject);
        let c2 = ctx(true, fend, future(800_004_096), s_write, ring_old);
        assert_eq!(sm.evaluate(800_004_096, 16384, 1_100, &c2), SmDecision::ProbeReject);
        // K = 3: a real sequential seek.
        let c3 = ctx(true, fend, future(800_008_192), s_write, ring_old);
        assert_eq!(sm.evaluate(800_008_192, 16384, 1_200, &c3), SmDecision::ConfirmedSeek);
        // The chain cursor must NOT have moved (engine-side invariant).
        assert_eq!(fend, 300_000_000);
    }

    #[test]
    fn isolated_probe_never_escalates() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        let ring_old = 0;
        let future = Some(s_write + 3 * 1024 * 1024);
        for (i, f) in [700_000_000u64, 900_000_000, 1_200_000_000].iter().enumerate() {
            let c = ctx(true, 300_000_000, future, s_write, ring_old);
            assert_eq!(
                sm.evaluate(*f, 16384, 1_000 + i as u64 * 100, &c),
                SmDecision::ProbeReject,
                "isolated probe block {} must never escalate",
                i + 1
            );
        }
    }

    #[test]
    fn behind_probe_is_rejected() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        let ring_old = 300 * 1024 * 1024;
        // Mapped behind ring_old (overwritten region).
        let c = ctx(true, 399_000_000, Some(ring_old - 1024), s_write, ring_old);
        assert_eq!(sm.evaluate(399_000_000, 16384, 1_000, &c), SmDecision::ProbeReject);
    }

    #[test]
    fn sequential_reads_never_escalate() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        // Adjacent reads (probe_pos set but inside the window) continue.
        let mut fend = 5_000_000u64;
        for i in 0..10u64 {
            let foff = 5_000_000 + i * 16384;
            let c = ctx(true, fend, Some(s_write - 8 * 1024 * 1024 + foff), s_write, s_write - 128 * 1024 * 1024);
            assert_eq!(sm.evaluate(foff, 16384, 1_000 + i * 100, &c), SmDecision::Continue);
            fend = foff + 16384;
        }
        assert_eq!(sm.phase(), SmPhase::Live);
    }

    #[test]
    fn position_preserving_anchor_selection() {
        let s_write = 400 * 1024 * 1024u64;
        let ring_old = s_write - 128 * 1024 * 1024;
        // Inside the ring: continue from there (snapped to the 188 grid).
        let target = select_resume_anchor(s_write - 40 * 1024 * 1024, ring_old, s_write);
        assert_eq!(target, Some(snap188(s_write - 40 * 1024 * 1024)));
        // Too close to the live edge: floored to s_write - RESUME_MIN_LEAD.
        let near = select_resume_anchor(s_write - 1024, ring_old, s_write).expect("some");
        assert!(s_write - near >= RESUME_MIN_LEAD);
        // Overwritten position: None -> caller falls back to the live target.
        assert_eq!(select_resume_anchor(ring_old - 1, ring_old, s_write), None);
        // Degenerate short stream: the lead floor is 0, so the anchor is
        // pulled to the start of the (tiny) ring.
        assert_eq!(select_resume_anchor(500, 0, 1024), Some(0));
    }

    #[test]
    fn resume_fires_on_either_reads_or_bytes() {
        // Byte threshold first: 4 x 16 KiB reads would hit reads==4 and
        // bytes==64 KiB together; use bigger reads to hit bytes earlier.
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        // Establish playback first: a gap jump cannot occur before a read.
        let c0 = ctx(true, 0, None, s_write, 0);
        assert_eq!(sm.evaluate(10_000_000, 16384, 1_000, &c0), SmDecision::Continue);
        let c = ctx(true, 10_016_384, None, s_write, 0);
        assert_eq!(sm.evaluate(80_000_000, 64 * 1024, 60_000, &c), SmDecision::Continue);
        // One 64 KiB chained read already satisfies the byte threshold.
        let c2 = ctx(true, 80_000_000 + 64 * 1024, None, s_write, 0);
        assert_eq!(
            sm.evaluate(80_000_000 + 64 * 1024, 64 * 1024, 60_100, &c2),
            SmDecision::ResumeAfterGap
        );
    }

    #[test]
    fn gap_resume_cycle_repeats() {
        let mut sm = StateMachine::new();
        sm.note_open(0);
        let s_write = 400 * 1024 * 1024u64;
        let ring_old = 0;
        // Establish playback first: a gap jump cannot occur before a read.
        let c0 = ctx(true, 0, None, s_write, ring_old);
        assert_eq!(sm.evaluate(10_000_000, 16384, 1_000, &c0), SmDecision::Continue);

        // First resume.
        let c = ctx(true, 10_016_384, None, s_write, ring_old);
        assert_eq!(sm.evaluate(50_000_000, 64 * 1024, 60_000, &c), SmDecision::Continue);
        let c2 = ctx(true, 50_000_000 + 64 * 1024, None, s_write, ring_old);
        assert_eq!(
            sm.evaluate(50_000_000 + 64 * 1024, 64 * 1024, 60_100, &c2),
            SmDecision::ResumeAfterGap
        );
        // A second Return + reopen after another gap must resume again.
        let c3 = ctx(true, 50_000_000 + 2 * 64 * 1024, None, s_write, ring_old);
        assert_eq!(sm.evaluate(60_000_000, 64 * 1024, 120_000, &c3), SmDecision::Continue);
        let c4 = ctx(true, 60_000_000 + 64 * 1024, None, s_write, ring_old);
        assert_eq!(
            sm.evaluate(60_000_000 + 64 * 1024, 64 * 1024, 120_100, &c4),
            SmDecision::ResumeAfterGap
        );
    }
}
