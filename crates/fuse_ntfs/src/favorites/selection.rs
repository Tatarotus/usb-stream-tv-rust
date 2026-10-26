//! Selection preparation and commit contract with source identity.
//!
//! A control ACK alone never proves which bytes are in the feeder. This
//! controller therefore keeps `request_generation` (user intent) separate
//! from `source_epoch` (one concrete source instance) and requires a framed
//! data frame carrying both identities before publishing.
//!
//! The active tuple is published atomically (from this controller's point of
//! view): session, manifest, file, channel, generation, epoch, anchor and
//! valid range. A stale generation/epoch or a frame from another channel is
//! rejected and never replaces the active selection.
//!
//! Structural MPEG-TS checks are not proof of source provenance or decoder
//! readiness. Runtime integration must bind frames to an authenticated source
//! transport and verify PAT/PMT/IDR/bootstrap readiness separately. These types
//! cannot detect old-channel payload maliciously relabeled by a caller.

use std::fmt;

use crate::contract::TS_PKT_SZ;

/// Fixed upper bound for each retained active/pending prefix. At most one of
/// each is owned, independently of the size of the virtual NTFS file.
pub const DEFAULT_MAX_STAGING_BYTES: usize = 8 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SelectionState {
    Idle,
    Preparing,
    Ready,
    Active,
}

impl fmt::Display for SelectionState {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let name = match self {
            Self::Idle => "IDLE",
            Self::Preparing => "PREPARING",
            Self::Ready => "READY",
            Self::Active => "ACTIVE",
        };
        f.write_str(name)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SelectionRequest {
    pub session_id: String,
    pub manifest_id: String,
    pub file_id: u32,
    pub channel_id: String,
    /// Monotonic intent id. Not interchangeable with `source_epoch`.
    pub request_generation: u64,
    /// Concrete source instance reserved for this preparation.
    pub source_epoch: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DataFrame {
    pub session_id: String,
    pub manifest_id: String,
    pub file_id: u32,
    pub channel_id: String,
    pub request_generation: u64,
    pub source_epoch: u64,
    /// Contiguous per-epoch frame sequence, starting at 1. The retained prefix
    /// starts at byte 0; each accepted frame appends to that prefix.
    pub sequence: u64,
    pub payload: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Published {
    pub session_id: String,
    pub manifest_id: String,
    pub file_id: u32,
    pub channel_id: String,
    pub request_generation: u64,
    pub source_epoch: u64,
    pub anchor: u64,
    pub valid_range: (u64, u64),
    pub data_digest: [u8; 32],
    pub data_bytes: u64,
    pub sequence: u64,
    pub published_at_ms: u64,
}

/// Immutable worker snapshot. Expensive hashing/splicing can run without the
/// runtime mutex; publication rechecks its exact generation/epoch/sequence/range.
pub(crate) struct CommitSnapshot {
    pub request: SelectionRequest,
    pub sequence: u64,
    pub range: (u64, u64),
    pub payload: Vec<u8>,
}

impl CommitSnapshot {
    pub(crate) fn publication(&self, digest: [u8; 32], now_ms: u64) -> Published {
        Published {
            session_id: self.request.session_id.clone(),
            manifest_id: self.request.manifest_id.clone(),
            file_id: self.request.file_id,
            channel_id: self.request.channel_id.clone(),
            request_generation: self.request.request_generation,
            source_epoch: self.request.source_epoch,
            anchor: self.range.0,
            valid_range: self.range,
            data_digest: digest,
            data_bytes: self.range.1 - self.range.0,
            sequence: self.sequence,
            published_at_ms: now_ms,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SelectionError {
    EmptyField(&'static str),
    NoPending,
    StaleGeneration { expected: u64, got: u64 },
    StaleEpoch { expected: u64, got: u64 },
    IdentityMismatch(&'static str),
    InvalidRange,
    InvalidTransport(&'static str),
    SequenceNotAdvancing { last: u64, got: u64 },
    SequenceGap { expected: u64, got: u64 },
    StagingLimitExceeded { limit: usize, incoming: usize },
    RangeNotBacked { range: (u64, u64), retained: u64 },
    SourceNotReady,
    DataNotVerified,
    TabletNotReady,
    CounterOverflow,
}

#[derive(Debug, Clone)]
struct VerifiedData {
    sequence: u64,
    payload: Vec<u8>,
}

#[derive(Debug, Clone)]
struct Pending {
    request: SelectionRequest,
    source_ready: bool,
    valid_range: Option<(u64, u64)>,
    ready_at_ms: Option<u64>,
    data: Option<VerifiedData>,
    tablet_ack: bool,
}

impl Pending {
    fn is_ready(&self) -> bool {
        self.source_ready && self.backed_range().is_ok()
    }

    fn backed_range(&self) -> Result<(u64, u64), SelectionError> {
        let data = self.data.as_ref().ok_or(SelectionError::DataNotVerified)?;
        let range = self.valid_range.ok_or(SelectionError::SourceNotReady)?;
        if range.0 >= range.1 || range.1 > data.payload.len() as u64 {
            return Err(SelectionError::RangeNotBacked {
                range,
                retained: data.payload.len() as u64,
            });
        }
        Ok(range)
    }
}

#[derive(Debug)]
pub struct SelectionController {
    next_generation: u64,
    next_epoch: u64,
    pending: Option<Pending>,
    active: Option<Published>,
    active_data: Option<VerifiedData>,
    max_staging_bytes: usize,
}

impl Default for SelectionController {
    fn default() -> Self {
        Self::new()
    }
}

impl SelectionController {
    pub fn new() -> Self {
        Self::with_staging_limit(DEFAULT_MAX_STAGING_BYTES)
    }

    /// A smaller staging limit is useful for constrained clients and tests.
    /// Callers cannot raise the fixed maximum through configuration.
    pub fn with_staging_limit(limit: usize) -> Self {
        Self {
            next_generation: 0,
            next_epoch: 0,
            pending: None,
            active: None,
            active_data: None,
            max_staging_bytes: limit.clamp(TS_PKT_SZ, DEFAULT_MAX_STAGING_BYTES),
        }
    }

    pub fn state(&self) -> SelectionState {
        match &self.pending {
            Some(pending) if pending.is_ready() => SelectionState::Ready,
            Some(_) => SelectionState::Preparing,
            None if self.active.is_some() => SelectionState::Active,
            None => SelectionState::Idle,
        }
    }

    /// True only when the pending generation is source-ready *and* the tablet
    /// acknowledged it. Runtime integrations use this to avoid splicing a
    /// transition that can never be published yet.
    pub fn ready_for_commit(&self) -> bool {
        self.pending
            .as_ref()
            .map(|pending| pending.is_ready() && pending.tablet_ack)
            .unwrap_or(false)
    }

    pub fn active(&self) -> Option<&Published> {
        self.active.as_ref()
    }

    /// Bytes actually retained for the published range. Publishing metadata
    /// does not discard their only backing copy.
    pub fn active_payload(&self) -> Option<&[u8]> {
        let published = self.active.as_ref()?;
        let data = self.active_data.as_ref()?;
        data.payload
            .get(published.valid_range.0 as usize..published.valid_range.1 as usize)
    }

    pub fn pending(&self) -> Option<&SelectionRequest> {
        self.pending.as_ref().map(|p| &p.request)
    }

    /// Retained bytes for the pending epoch. Runtime integrations use this to
    /// validate and splice the transition *before* publishing; it never
    /// exposes a source that has not been committed as active.
    pub fn pending_payload(&self) -> Option<&[u8]> {
        self.pending
            .as_ref()
            .and_then(|pending| pending.data.as_ref())
            .map(|data| data.payload.as_slice())
    }

    /// Requests preparation for a file. Repeating the same intent is
    /// idempotent; a different intent supersedes the previous generation.
    pub fn request(
        &mut self,
        session_id: &str,
        manifest_id: &str,
        file_id: u32,
        channel_id: &str,
    ) -> Result<SelectionRequest, SelectionError> {
        if session_id.trim().is_empty() {
            return Err(SelectionError::EmptyField("session_id"));
        }
        if manifest_id.trim().is_empty() {
            return Err(SelectionError::EmptyField("manifest_id"));
        }
        if channel_id.trim().is_empty() {
            return Err(SelectionError::EmptyField("channel_id"));
        }

        if let Some(pending) = &self.pending {
            let same = pending.request.session_id == session_id
                && pending.request.manifest_id == manifest_id
                && pending.request.file_id == file_id
                && pending.request.channel_id == channel_id;
            if same {
                return Ok(pending.request.clone());
            }
        }

        let next_generation = self
            .next_generation
            .checked_add(1)
            .ok_or(SelectionError::CounterOverflow)?;
        let next_epoch = self
            .next_epoch
            .checked_add(1)
            .ok_or(SelectionError::CounterOverflow)?;
        self.next_generation = next_generation;
        self.next_epoch = next_epoch;
        let request = SelectionRequest {
            session_id: session_id.to_string(),
            manifest_id: manifest_id.to_string(),
            file_id,
            channel_id: channel_id.to_string(),
            request_generation: self.next_generation,
            source_epoch: self.next_epoch,
        };
        self.pending = Some(Pending {
            request: request.clone(),
            source_ready: false,
            valid_range: None,
            ready_at_ms: None,
            data: None,
            tablet_ack: false,
        });
        Ok(request)
    }

    fn pending_mut(&mut self, generation: u64, epoch: u64) -> Result<&mut Pending, SelectionError> {
        let pending = self.pending.as_mut().ok_or(SelectionError::NoPending)?;
        if pending.request.request_generation != generation {
            return Err(SelectionError::StaleGeneration {
                expected: pending.request.request_generation,
                got: generation,
            });
        }
        if pending.request.source_epoch != epoch {
            return Err(SelectionError::StaleEpoch {
                expected: pending.request.source_epoch,
                got: epoch,
            });
        }
        Ok(pending)
    }

    /// Records the runtime's assertion that the source is decodable. This
    /// controller cannot establish PAT/PMT/IDR readiness itself. The range uses
    /// byte coordinates in the retained prefix, not virtual-file capacity.
    pub fn mark_source_ready(
        &mut self,
        generation: u64,
        epoch: u64,
        valid_range: (u64, u64),
        now_ms: u64,
    ) -> Result<(), SelectionError> {
        if valid_range.1 <= valid_range.0
            || !valid_range.0.is_multiple_of(TS_PKT_SZ as u64)
            || !valid_range.1.is_multiple_of(TS_PKT_SZ as u64)
        {
            return Err(SelectionError::InvalidRange);
        }
        let pending = self.pending_mut(generation, epoch)?;
        if pending.valid_range != Some(valid_range) {
            pending.tablet_ack = false;
        }
        pending.source_ready = true;
        pending.valid_range = Some(valid_range);
        pending.ready_at_ms = Some(now_ms);
        Ok(())
    }

    fn validate_transport(payload: &[u8]) -> Result<(), SelectionError> {
        if payload.len() < TS_PKT_SZ
            || !payload
                .chunks(TS_PKT_SZ)
                .all(|packet| packet.len() == TS_PKT_SZ)
        {
            return Err(SelectionError::InvalidTransport(
                "payload is not a whole number of TS packets",
            ));
        }
        for packet in payload.as_chunks::<TS_PKT_SZ>().0 {
            if packet[0] != 0x47 {
                return Err(SelectionError::InvalidTransport(
                    "missing MPEG-TS sync byte",
                ));
            }
            if packet[1] & 0x80 != 0 {
                return Err(SelectionError::InvalidTransport(
                    "transport error indicator set",
                ));
            }
            if packet[3] & 0xc0 != 0 {
                return Err(SelectionError::InvalidTransport(
                    "scrambled transport packet",
                ));
            }
            let adaptation_control = (packet[3] >> 4) & 3;
            if adaptation_control == 0 {
                return Err(SelectionError::InvalidTransport(
                    "reserved adaptation field control",
                ));
            }
            if adaptation_control & 2 != 0 {
                let length = packet[4] as usize;
                if (adaptation_control == 2 && length != 183)
                    || (adaptation_control == 3 && length > 182)
                {
                    return Err(SelectionError::InvalidTransport(
                        "invalid adaptation field length",
                    ));
                }
                if length > 0 {
                    let flags = packet[5];
                    let end = 5 + length;
                    let mut cursor = 6;
                    for (flag, bytes) in [(0x10, 6), (0x08, 6), (0x04, 1)] {
                        if flags & flag != 0 {
                            let field_start = cursor;
                            cursor += bytes;
                            if cursor > end {
                                return Err(SelectionError::InvalidTransport(
                                    "truncated adaptation optional field",
                                ));
                            }
                            if bytes == 6 {
                                let extension = ((packet[field_start + 4] as u16 & 1) << 8)
                                    | packet[field_start + 5] as u16;
                                if packet[field_start + 4] & 0x7e != 0x7e || extension >= 300 {
                                    return Err(SelectionError::InvalidTransport(
                                        "invalid PCR/OPCR field",
                                    ));
                                }
                            }
                        }
                    }
                    for flag in [0x02, 0x01] {
                        if flags & flag != 0 {
                            if cursor >= end {
                                return Err(SelectionError::InvalidTransport(
                                    "missing adaptation subfield length",
                                ));
                            }
                            let sublength = packet[cursor] as usize;
                            cursor += 1 + sublength;
                            if cursor > end {
                                return Err(SelectionError::InvalidTransport(
                                    "truncated adaptation subfield",
                                ));
                            }
                        }
                    }
                }
            }
        }
        Ok(())
    }

    /// Accepts one framed media chunk. The frame must carry this pending
    /// request's session, manifest, file, channel, generation and epoch.
    pub fn accept_data_frame(&mut self, frame: &DataFrame) -> Result<(), SelectionError> {
        let pending = self.pending.as_mut().ok_or(SelectionError::NoPending)?;
        let req = &pending.request;
        if frame.request_generation != req.request_generation {
            return Err(SelectionError::StaleGeneration {
                expected: req.request_generation,
                got: frame.request_generation,
            });
        }
        if frame.source_epoch != req.source_epoch {
            return Err(SelectionError::StaleEpoch {
                expected: req.source_epoch,
                got: frame.source_epoch,
            });
        }
        if frame.session_id != req.session_id {
            return Err(SelectionError::IdentityMismatch("session"));
        }
        if frame.manifest_id != req.manifest_id {
            return Err(SelectionError::IdentityMismatch("manifest"));
        }
        if frame.file_id != req.file_id {
            return Err(SelectionError::IdentityMismatch("file_id"));
        }
        if frame.channel_id != req.channel_id {
            return Err(SelectionError::IdentityMismatch("channel_id"));
        }
        let retained = pending.data.as_ref().map_or(0, |data| data.payload.len());
        if frame.payload.len() > self.max_staging_bytes.saturating_sub(retained) {
            return Err(SelectionError::StagingLimitExceeded {
                limit: self.max_staging_bytes,
                incoming: frame.payload.len(),
            });
        }
        Self::validate_transport(&frame.payload)?;
        let expected = match &pending.data {
            Some(last) => last
                .sequence
                .checked_add(1)
                .ok_or(SelectionError::CounterOverflow)?,
            None => 1,
        };
        if let Some(last) = &pending.data {
            if frame.sequence <= last.sequence {
                return Err(SelectionError::SequenceNotAdvancing {
                    last: last.sequence,
                    got: frame.sequence,
                });
            }
        }
        if frame.sequence != expected {
            return Err(SelectionError::SequenceGap {
                expected,
                got: frame.sequence,
            });
        }
        let data = pending.data.get_or_insert_with(|| VerifiedData {
            sequence: 0,
            payload: Vec::with_capacity(self.max_staging_bytes),
        });
        data.payload.extend_from_slice(&frame.payload);
        data.sequence = frame.sequence;
        pending.tablet_ack = false;
        Ok(())
    }

    /// Records the tablet-side readiness ACK. It is necessary but never
    /// sufficient: the verified data frame above is still required.
    pub fn tablet_ack(&mut self, generation: u64, epoch: u64) -> Result<(), SelectionError> {
        let pending = self.pending_mut(generation, epoch)?;
        if pending.data.is_none() {
            return Err(SelectionError::DataNotVerified);
        }
        if !pending.source_ready {
            return Err(SelectionError::SourceNotReady);
        }
        pending.backed_range()?;
        pending.tablet_ack = true;
        Ok(())
    }

    /// Publishes the active tuple only when the current generation has both a
    /// source-ready signal and verified data from that exact epoch.
    pub fn commit(&mut self, now_ms: u64) -> Result<Published, SelectionError> {
        let snapshot = self.commit_snapshot()?;
        let digest = super::digest::sha256(
            &snapshot.payload[snapshot.range.0 as usize..snapshot.range.1 as usize],
        );
        self.commit_prehashed(&snapshot, digest, now_ms)
    }

    pub(crate) fn commit_snapshot(&self) -> Result<CommitSnapshot, SelectionError> {
        let pending = self.pending.as_ref().ok_or(SelectionError::NoPending)?;
        if !pending.source_ready {
            return Err(SelectionError::SourceNotReady);
        }
        let data = pending
            .data
            .as_ref()
            .ok_or(SelectionError::DataNotVerified)?;
        if !pending.tablet_ack {
            return Err(SelectionError::TabletNotReady);
        }
        let range = pending.backed_range()?;
        Ok(CommitSnapshot {
            request: pending.request.clone(),
            sequence: data.sequence,
            range,
            payload: data.payload.clone(),
        })
    }

    pub(crate) fn commit_prehashed(
        &mut self,
        snapshot: &CommitSnapshot,
        digest: [u8; 32],
        now_ms: u64,
    ) -> Result<Published, SelectionError> {
        let pending = self.pending.as_ref().ok_or(SelectionError::NoPending)?;
        if pending.request.request_generation != snapshot.request.request_generation {
            return Err(SelectionError::StaleGeneration {
                expected: pending.request.request_generation,
                got: snapshot.request.request_generation,
            });
        }
        if pending.request.source_epoch != snapshot.request.source_epoch {
            return Err(SelectionError::StaleEpoch {
                expected: pending.request.source_epoch,
                got: snapshot.request.source_epoch,
            });
        }
        if pending.request != snapshot.request {
            return Err(SelectionError::IdentityMismatch("commit snapshot"));
        }
        if !pending.source_ready {
            return Err(SelectionError::SourceNotReady);
        }
        if !pending.tablet_ack {
            return Err(SelectionError::TabletNotReady);
        }
        let data = pending
            .data
            .as_ref()
            .ok_or(SelectionError::DataNotVerified)?;
        if data.sequence != snapshot.sequence || data.payload.len() != snapshot.payload.len() {
            return Err(SelectionError::SequenceNotAdvancing {
                last: data.sequence,
                got: snapshot.sequence,
            });
        }
        let range = pending.backed_range()?;
        if range != snapshot.range {
            return Err(SelectionError::InvalidRange);
        }
        let published = snapshot.publication(digest, now_ms);
        self.active = Some(published.clone());
        self.active_data = self.pending.take().and_then(|pending| pending.data);
        Ok(published)
    }

    /// Cancels the pending generation; the previously active tuple remains
    /// active until a different generation commits.
    pub fn cancel(&mut self, generation: u64) -> Result<(), SelectionError> {
        let pending = self.pending.as_ref().ok_or(SelectionError::NoPending)?;
        if pending.request.request_generation != generation {
            return Err(SelectionError::StaleGeneration {
                expected: pending.request.request_generation,
                got: generation,
            });
        }
        self.pending = None;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::favorites::digest::sha256;

    fn fake_ts(marker: u8, packets: usize) -> Vec<u8> {
        let mut out = Vec::with_capacity(packets * TS_PKT_SZ);
        for i in 0..packets {
            let mut packet = [0u8; TS_PKT_SZ];
            packet[0] = 0x47;
            packet[1] = 0x1f;
            packet[2] = 0xff;
            packet[3] = 0x10 | (i & 0x0f) as u8;
            packet[4..].fill(marker);
            out.extend_from_slice(&packet);
        }
        out
    }

    fn frame(request: &SelectionRequest, sequence: u64, payload: Vec<u8>) -> DataFrame {
        DataFrame {
            session_id: request.session_id.clone(),
            manifest_id: request.manifest_id.clone(),
            file_id: request.file_id,
            channel_id: request.channel_id.clone(),
            request_generation: request.request_generation,
            source_epoch: request.source_epoch,
            sequence,
            payload,
        }
    }

    #[test]
    fn ack_before_data_cannot_publish() {
        let mut c = SelectionController::new();
        let req = c.request("s1", "m1", 1, "sbt").unwrap();
        c.mark_source_ready(
            req.request_generation,
            req.source_epoch,
            (0, 4 * TS_PKT_SZ as u64),
            0,
        )
        .unwrap();
        assert_eq!(c.state(), SelectionState::Preparing);
        assert_eq!(
            c.tablet_ack(req.request_generation, req.source_epoch),
            Err(SelectionError::DataNotVerified)
        );
        assert_eq!(c.commit(1), Err(SelectionError::DataNotVerified));
        assert!(c.active().is_none());
    }

    #[test]
    fn verified_data_and_ack_publish_the_exact_source_bytes() {
        let mut c = SelectionController::new();
        let req = c.request("s1", "m1", 1, "sbt").unwrap();
        let sbt_bytes = fake_ts(0xa1, 8);
        let record_bytes = fake_ts(0xb2, 8);
        c.accept_data_frame(&frame(&req, 1, sbt_bytes.clone()))
            .unwrap();
        assert_eq!(c.state(), SelectionState::Preparing);
        c.mark_source_ready(
            req.request_generation,
            req.source_epoch,
            (0, sbt_bytes.len() as u64),
            10,
        )
        .unwrap();
        assert_eq!(c.state(), SelectionState::Ready);
        assert_eq!(c.commit(20), Err(SelectionError::TabletNotReady));
        c.tablet_ack(req.request_generation, req.source_epoch)
            .unwrap();
        let published = c.commit(30).unwrap();
        assert_eq!(published.channel_id, "sbt");
        assert_eq!(published.request_generation, 1);
        assert_eq!(published.source_epoch, 1);
        assert_eq!(published.anchor, 0);
        assert_eq!(published.data_digest, sha256(&sbt_bytes));
        assert_ne!(published.data_digest, sha256(&record_bytes));
        assert_eq!(c.state(), SelectionState::Active);
        assert_eq!(c.active().unwrap(), &published);
    }

    #[test]
    fn concurrent_supersede_rejects_prehashed_snapshot() {
        use std::sync::{Arc, Mutex};
        let mut selection = SelectionController::new();
        let request = selection.request("s1", "m1", 1, "sbt").unwrap();
        let bytes = fake_ts(0xa1, 8);
        selection
            .accept_data_frame(&frame(&request, 1, bytes.clone()))
            .unwrap();
        selection
            .mark_source_ready(
                request.request_generation,
                request.source_epoch,
                (0, bytes.len() as u64),
                10,
            )
            .unwrap();
        selection
            .tablet_ack(request.request_generation, request.source_epoch)
            .unwrap();
        let state = Arc::new(Mutex::new(selection));
        let snapshot = state.lock().unwrap().commit_snapshot().unwrap();
        let writer_state = Arc::clone(&state);
        std::thread::spawn(move || {
            writer_state
                .lock()
                .unwrap()
                .request("s1", "m1", 2, "record")
                .unwrap();
        })
        .join()
        .unwrap();
        let digest = sha256(&bytes); // deliberately outside the shared lock
        let mut selection = state.lock().unwrap();
        assert!(matches!(
            selection.commit_prehashed(&snapshot, digest, 20),
            Err(SelectionError::StaleGeneration { .. })
        ));
        assert!(selection.active().is_none());
        assert_eq!(selection.pending().unwrap().channel_id, "record");
    }

    #[test]
    fn appending_after_hash_snapshot_requires_new_snapshot() {
        let mut selection = SelectionController::new();
        let request = selection.request("s1", "m1", 1, "sbt").unwrap();
        let bytes = fake_ts(0xa1, 8);
        selection
            .accept_data_frame(&frame(&request, 1, bytes.clone()))
            .unwrap();
        selection
            .mark_source_ready(
                request.request_generation,
                request.source_epoch,
                (0, bytes.len() as u64),
                10,
            )
            .unwrap();
        selection
            .tablet_ack(request.request_generation, request.source_epoch)
            .unwrap();
        let snapshot = selection.commit_snapshot().unwrap();
        selection
            .accept_data_frame(&frame(&request, 2, fake_ts(0xa1, 8)))
            .unwrap();
        selection
            .tablet_ack(request.request_generation, request.source_epoch)
            .unwrap();
        assert!(matches!(
            selection.commit_prehashed(&snapshot, sha256(&bytes), 20),
            Err(SelectionError::SequenceNotAdvancing { .. })
        ));
        assert!(selection.active().is_none());
    }

    #[test]
    fn data_from_the_wrong_channel_is_rejected() {
        let mut c = SelectionController::new();
        let req = c.request("s1", "m1", 1, "sbt").unwrap();
        let mut wrong = frame(&req, 1, fake_ts(0xb2, 4));
        wrong.channel_id = "record".into();
        assert_eq!(
            c.accept_data_frame(&wrong),
            Err(SelectionError::IdentityMismatch("channel_id"))
        );
        assert!(c.pending().is_some());
    }

    #[test]
    fn stale_generation_and_epoch_are_rejected_after_supersede() {
        let mut c = SelectionController::new();
        let first = c.request("s1", "m1", 1, "sbt").unwrap();
        let second = c.request("s1", "m1", 2, "record").unwrap();
        assert!(second.request_generation > first.request_generation);
        assert!(second.source_epoch > first.source_epoch);

        assert_eq!(
            c.accept_data_frame(&frame(&first, 1, fake_ts(0xa1, 4))),
            Err(SelectionError::StaleGeneration {
                expected: second.request_generation,
                got: first.request_generation
            })
        );
        assert_eq!(
            c.tablet_ack(first.request_generation, first.source_epoch),
            Err(SelectionError::StaleGeneration {
                expected: second.request_generation,
                got: first.request_generation
            })
        );
        assert_eq!(
            c.mark_source_ready(
                second.request_generation,
                first.source_epoch,
                (0, 4 * TS_PKT_SZ as u64),
                1
            ),
            Err(SelectionError::StaleEpoch {
                expected: second.source_epoch,
                got: first.source_epoch
            })
        );
    }

    #[test]
    fn a_previous_active_channel_survives_a_failed_preparation() {
        let mut c = SelectionController::new();
        let sbt = c.request("s1", "m1", 1, "sbt").unwrap();
        let sbt_bytes = fake_ts(0xa1, 4);
        c.mark_source_ready(
            sbt.request_generation,
            sbt.source_epoch,
            (0, sbt_bytes.len() as u64),
            0,
        )
        .unwrap();
        c.accept_data_frame(&frame(&sbt, 1, sbt_bytes.clone()))
            .unwrap();
        c.tablet_ack(sbt.request_generation, sbt.source_epoch)
            .unwrap();
        let published = c.commit(10).unwrap();
        assert_eq!(published.channel_id, "sbt");

        let record = c.request("s1", "m1", 2, "record").unwrap();
        assert_eq!(c.state(), SelectionState::Preparing);
        assert_eq!(c.active().unwrap().channel_id, "sbt");
        c.cancel(record.request_generation).unwrap();
        assert_eq!(c.state(), SelectionState::Active);
        assert_eq!(c.active().unwrap().channel_id, "sbt");
        assert_eq!(c.active().unwrap().source_epoch, sbt.source_epoch);
    }

    #[test]
    fn repeated_identical_request_is_idempotent() {
        let mut c = SelectionController::new();
        let first = c.request("s1", "m1", 1, "sbt").unwrap();
        let again = c.request("s1", "m1", 1, "sbt").unwrap();
        assert_eq!(first, again);
        let different = c.request("s1", "m1", 2, "record").unwrap();
        assert!(different.request_generation > first.request_generation);
    }

    #[test]
    fn transport_validation_rejects_bad_packets_and_old_sequence() {
        let mut c = SelectionController::new();
        let req = c.request("s1", "m1", 1, "sbt").unwrap();
        let mut bad = frame(&req, 1, vec![0u8; TS_PKT_SZ * 2]);
        assert!(matches!(
            c.accept_data_frame(&bad),
            Err(SelectionError::InvalidTransport(_))
        ));
        bad.payload = fake_ts(0xa1, 2);
        c.accept_data_frame(&bad).unwrap();
        let stale = frame(&req, 1, fake_ts(0xa1, 2));
        assert_eq!(
            c.accept_data_frame(&stale),
            Err(SelectionError::SequenceNotAdvancing { last: 1, got: 1 })
        );
        let short = frame(&req, 2, vec![0x47; TS_PKT_SZ - 1]);
        assert!(matches!(
            c.accept_data_frame(&short),
            Err(SelectionError::InvalidTransport(_))
        ));
    }

    #[test]
    fn structural_transport_checks_reject_reserved_headers_and_truncated_adaptation() {
        let valid = fake_ts(0xa1, 1);
        let mut bad = valid.clone();
        bad[3] = 0;
        assert!(SelectionController::validate_transport(&bad).is_err());
        bad = valid.clone();
        bad[1] |= 0x80;
        assert!(SelectionController::validate_transport(&bad).is_err());
        bad = valid.clone();
        bad[3] |= 0x80;
        assert!(SelectionController::validate_transport(&bad).is_err());
        bad = valid.clone();
        bad[3] = 0x30;
        bad[4] = 183;
        assert!(SelectionController::validate_transport(&bad).is_err());
        bad = valid.clone();
        bad[3] = 0x20;
        bad[4] = 182;
        assert!(SelectionController::validate_transport(&bad).is_err());
        bad = valid.clone();
        bad[3] = 0x30;
        bad[4] = 1;
        bad[5] = 0x10;
        assert!(SelectionController::validate_transport(&bad).is_err());
        bad[4] = 7;
        bad[6..12].copy_from_slice(&[0, 0, 0, 0, 0x7e, 0]);
        assert!(SelectionController::validate_transport(&bad).is_ok());
        bad[10] = 0x7f;
        bad[11] = 44; // Extension300 is outside the 0..299 range.
        assert!(SelectionController::validate_transport(&bad).is_err());
        assert!(SelectionController::validate_transport(&valid).is_ok());
    }

    #[test]
    fn staging_limit_rejects_oversized_frame_without_losing_existing_data() {
        let mut c = SelectionController::with_staging_limit(2 * TS_PKT_SZ);
        let req = c.request("s1", "m1", 1, "sbt").unwrap();
        let first = fake_ts(0xa1, 1);
        let second = fake_ts(0xb2, 1);
        c.accept_data_frame(&frame(&req, 1, first.clone())).unwrap();
        assert!(matches!(
            c.accept_data_frame(&frame(&req, 2, fake_ts(0xb2, 2))),
            Err(SelectionError::StagingLimitExceeded { .. })
        ));
        c.accept_data_frame(&frame(&req, 2, second.clone()))
            .unwrap();
        c.mark_source_ready(
            req.request_generation,
            req.source_epoch,
            (0, (2 * TS_PKT_SZ) as u64),
            0,
        )
        .unwrap();
        c.tablet_ack(req.request_generation, req.source_epoch)
            .unwrap();
        let published = c.commit(1).unwrap();
        let expected: Vec<u8> = first.into_iter().chain(second).collect();
        assert_eq!(c.active_payload().unwrap(), expected);
        assert_eq!(published.data_digest, sha256(&expected));
        assert_eq!(published.data_bytes, expected.len() as u64);
    }

    #[test]
    fn frame_sequences_must_start_at_one_and_cannot_skip() {
        let mut c = SelectionController::new();
        let req = c.request("s1", "m1", 1, "sbt").unwrap();
        assert_eq!(
            c.accept_data_frame(&frame(&req, 2, fake_ts(0xa1, 1))),
            Err(SelectionError::SequenceGap {
                expected: 1,
                got: 2
            })
        );
        c.accept_data_frame(&frame(&req, 1, fake_ts(0xa1, 1)))
            .unwrap();
        assert_eq!(
            c.accept_data_frame(&frame(&req, 3, fake_ts(0xa1, 1))),
            Err(SelectionError::SequenceGap {
                expected: 2,
                got: 3
            })
        );
        c.accept_data_frame(&frame(&req, 2, fake_ts(0xa1, 1)))
            .unwrap();
    }

    #[test]
    fn valid_range_cannot_exceed_retained_bytes_or_break_ts_alignment() {
        let mut c = SelectionController::new();
        let req = c.request("s1", "m1", 1, "sbt").unwrap();
        let bytes = fake_ts(0xa1, 2);
        c.accept_data_frame(&frame(&req, 1, bytes.clone())).unwrap();
        c.mark_source_ready(
            req.request_generation,
            req.source_epoch,
            (0, (100 * TS_PKT_SZ) as u64),
            0,
        )
        .unwrap();
        assert_eq!(c.state(), SelectionState::Preparing);
        assert!(matches!(
            c.tablet_ack(req.request_generation, req.source_epoch),
            Err(SelectionError::RangeNotBacked { .. })
        ));
        assert_eq!(
            c.mark_source_ready(req.request_generation, req.source_epoch, (1, 188), 0),
            Err(SelectionError::InvalidRange)
        );
        c.mark_source_ready(req.request_generation, req.source_epoch, (188, 376), 0)
            .unwrap();
        c.tablet_ack(req.request_generation, req.source_epoch)
            .unwrap();
        let published = c.commit(1).unwrap();
        assert_eq!(published.anchor, 188);
        assert!(
            published.anchor >= published.valid_range.0
                && published.anchor < published.valid_range.1
        );
        assert_eq!(c.active_payload().unwrap(), &bytes[188..376]);
    }

    #[test]
    fn changing_range_after_ack_requires_a_fresh_tablet_ack() {
        let mut c = SelectionController::new();
        let req = c.request("s1", "m1", 1, "sbt").unwrap();
        c.accept_data_frame(&frame(&req, 1, fake_ts(0xa1, 2)))
            .unwrap();
        c.mark_source_ready(req.request_generation, req.source_epoch, (0, 376), 0)
            .unwrap();
        c.tablet_ack(req.request_generation, req.source_epoch)
            .unwrap();
        c.mark_source_ready(req.request_generation, req.source_epoch, (188, 376), 0)
            .unwrap();
        assert_eq!(c.commit(1), Err(SelectionError::TabletNotReady));
    }
}
