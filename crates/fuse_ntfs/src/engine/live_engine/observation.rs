//! Observation-only wiring for [`LiveEngine`] (TASK-003, corrected in R3).
//!
//! Constructors and numeric event snapshots live here so the playback engine
//! keeps its original shape and line budget. Nothing in this module reads or
//! writes playback state beyond copying values already held by the caller (or
//! briefly sampling them under the existing mutexes) into an [`Event`];
//! `Observer::emit` only stamps and enqueues with `try_send`.
//!
//! The measured quantities are deliberately literal: `real_bytes` counts bytes
//! copied from the ring (which can themselves contain NULL-filled payload), and
//! `null_bytes` counts synthetic NULL filler written by this read. Neither is a
//! claim about decodable video.

use super::LiveEngine;
use crate::engine::state_machine::SmDecision;
use crate::observability::{Component, Event, EventKind, Observer, Reason};

impl LiveEngine {
    /// Builds an engine that reports to `obs`. Playback behavior is unchanged;
    /// `LiveEngine::new()` keeps using a disabled observer.
    pub fn with_observer(obs: Observer) -> Self {
        let mut engine = Self::new();
        engine.obs = obs;
        engine
    }

    /// Live-edge starvation inside one delivery loop.
    pub(super) fn obs_underrun(&self, null_bytes: u64) {
        let ev = Event::new(Component::Engine, EventKind::RingUnderrun)
            .reason(Reason::Starve)
            .null_bytes(null_bytes);
        self.obs.emit(ev);
    }

    /// A re-anchor that was actually committed, with the reason the existing
    /// decision logic already produced.
    pub(super) fn obs_commit(&self, epoch: u64, base_foff: u64, base_spos: u64, why: Reason) {
        let ev = Event::new(Component::Engine, EventKind::ReanchorCommit)
            .reason(why)
            .anchor_epoch(epoch)
            .foff(base_foff)
            .mapped_spos(base_spos);
        self.obs.emit(ev);
    }

    /// Overwritten read abandoned without progress (no fabricated adjacency).
    /// The caller delivered `len` bytes of synthetic NULL filler for it.
    pub(super) fn obs_noncommit(&self, foff: u64, len: u64, why: Reason) {
        let ev = Event::new(Component::Engine, EventKind::OverwrittenNoncommit)
            .reason(why)
            .foff(foff)
            .len(len)
            .null_bytes(len);
        self.obs.emit(ev);
    }

    /// Warm re-anchor calibration result (reported only when unusable).
    pub(super) fn obs_calibration(&self, valid: bool) {
        if valid {
            return;
        }
        let ev =
            Event::new(Component::Engine, EventKind::CalibrationInvalid).reason(Reason::Unknown);
        self.obs.emit(ev);
    }

    /// Post-delivery result for a cold/no-data read: NULL filler only, no
    /// mapping and no copied ring bytes.
    pub(super) fn obs_cold(&self, foff: u64, len: usize, wait_ms: u64, file_id: u32) {
        let epoch = self.anchor.lock().unwrap_or_else(|e| e.into_inner()).epoch_seq;
        self.emit_read(foff, u64::MAX, Reason::Cold, len, 0, len, wait_ms, file_id, epoch);
    }

    /// Post-delivery result for one successful delivery attempt: real bytes
    /// copied from the ring versus synthetic NULL filler, the mapping, the
    /// bounded wait and the anchor epoch/file identity in effect.
    pub(super) fn obs_delivered(
        &self,
        foff: u64,
        mapped: u64,
        real: usize,
        len: usize,
        wait_ms: u64,
        file_id: u32,
        why: Reason,
        epoch: u64,
    ) {
        self.emit_read(foff, mapped, why, len, real, len - real, wait_ms, file_id, epoch);
    }

    /// Aborts this read with the unchanged private [`Self::abandon_overwritten`]
    /// (NULL filler, no progress) and records the NULL-only result. No filler,
    /// mapping or decision code is moved here; only the emit is added.
    pub(super) fn obs_abort(
        &self,
        dst: &mut [u8],
        foff: u64,
        why: Reason,
        observe_ms: Option<u64>,
    ) -> usize {
        let n = self.abandon_overwritten(dst, foff, observe_ms);
        let file_id = self.anchor_file_id();
        let epoch = self.anchor.lock().unwrap_or_else(|e| e.into_inner()).epoch_seq;
        self.emit_read(foff, u64::MAX, why, n, 0, n, 0, file_id, epoch);
        n
    }

    /// Aborts a read whose generation was superseded. Unlike [`Self::obs_abort`],
    /// this must not call `abandon_overwritten`: that would invalidate the
    /// transformer tail of the *current* generation. It only fills NULLs,
    /// accounts them and emits the read record.
    pub(super) fn obs_abort_stale(&self, dst: &mut [u8], foff: u64) -> usize {
        let n = dst.len();
        crate::engine::ring::fill_null_at(dst, foff);
        self.ring.lock().note_null(n as u64);
        let file_id = self.anchor_file_id();
        let epoch = self.anchor.lock().unwrap_or_else(|e| e.into_inner()).epoch_seq;
        self.emit_read(foff, u64::MAX, Reason::ReferenceInvalid, n, 0, n, 0, file_id, epoch);
        n
    }

    fn anchor_file_id(&self) -> u32 {
        let anchor = self.anchor.lock().unwrap_or_else(|e| e.into_inner());
        anchor.file_id
    }

    /// Emits the one post-delivery `live_read_progress` record. Lock order:
    /// anchor first, then ring, never nested.
    fn emit_read(
        &self,
        foff: u64,
        mapped: u64,
        why: Reason,
        len: usize,
        real: usize,
        null: usize,
        wait_ms: u64,
        file_id: u32,
        epoch: u64,
    ) {
        let (s_write, ring_old) = {
            let rd = self.ring.lock();
            (rd.s_write(), rd.ring_old())
        };
        let ev = Event::new(Component::Engine, EventKind::LiveReadProgress)
            .reason(why)
            .foff(foff)
            .len(len as u64)
            .mapped_spos(mapped)
            .s_write(s_write)
            .ring_old(ring_old)
            .real_bytes(real as u64)
            .null_bytes(null as u64)
            .wait_ms(wait_ms)
            .anchor_epoch(epoch)
            .file_id(u64::from(file_id));
        self.obs.emit(ev);
    }
}

/// Classifies a committed re-anchor from values the engine already computed.
/// This never changes the decision, only how it is labelled.
pub(super) fn commit_why(sm: SmDecision, ovw: bool, channel: bool, restart: bool) -> Reason {
    if channel {
        Reason::ChannelSwitch
    } else if restart {
        Reason::Restart
    } else if ovw {
        Reason::OverwrittenRecovery
    } else if sm == SmDecision::ConfirmedSeek {
        Reason::ConfirmedSeek
    } else if sm == SmDecision::ResumeAfterGap {
        Reason::ResumeAfterGap
    } else {
        Reason::Reanchor
    }
}
