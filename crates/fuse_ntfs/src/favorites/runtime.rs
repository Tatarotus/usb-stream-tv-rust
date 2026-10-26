//! Bounded runtime that turns detector intents into authenticated source
//! preparation and serves immutable bootstrap / active epoch bytes by file.
//!
//! Ownership split:
//! * FUSE only routes disk offsets to `file_id` + in-file offset and copies
//!   ready bytes; it never performs network I/O, JSON parsing or hashing;
//! * a single worker (driven by the caller, e.g. a background thread) opens
//!   the newest intent, validates frames, splices the transition and commits;
//! * inactive files serve only their own immutable bootstrap. Bytes from the
//!   active channel are never used as a fallback for another file.
//!
//! The runtime keeps at most one pending intent and two retained prefixes
//! (pending and active), each capped by `SelectionController`'s fixed limit.

use std::collections::{HashMap, VecDeque};
use std::sync::{Condvar, Mutex};
use std::time::{Duration, Instant};

use crate::contract::TS_PKT_SZ;
use crate::engine::ring::{fill_null_at, RingData};
use crate::engine::snapper::is_pat;
use crate::engine::ts_xform::{ts_has_sps, ts_payload_off};
use crate::favorites::bootstrap::{self, Bootstrap, SpliceState};
use crate::favorites::detector::{DetectorConfig, ProbeDetector};
use crate::favorites::extents::{ExtentMap, MapRegion};
use crate::favorites::selection::DataFrame;
use crate::favorites::selection::{
    Published, SelectionController, SelectionError, SelectionRequest, SelectionState,
};

pub const DEFAULT_MIN_READY_BYTES: usize = 128 * 1024;
pub const DEFAULT_MAX_FRAMES_PER_STEP: usize = 48;
pub const DEFAULT_ACTIVE_CAPACITY_BYTES: usize = 128 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FavoriteFile {
    pub file_id: u32,
    pub channel_id: String,
    pub path: String,
    pub logical_size: u64,
}

#[derive(Debug, Clone)]
pub struct RuntimeConfig {
    pub session_id: String,
    pub manifest_id: String,
    pub min_ready_bytes: usize,
    pub max_frames_per_step: usize,
    pub staging_bytes: usize,
    pub active_capacity_bytes: usize,
    pub detector_config: DetectorConfig,
}

impl RuntimeConfig {
    pub fn new(session_id: &str, manifest_id: &str) -> Self {
        Self {
            session_id: session_id.to_string(),
            manifest_id: manifest_id.to_string(),
            min_ready_bytes: DEFAULT_MIN_READY_BYTES,
            max_frames_per_step: DEFAULT_MAX_FRAMES_PER_STEP,
            staging_bytes: super::selection::DEFAULT_MAX_STAGING_BYTES,
            active_capacity_bytes: DEFAULT_ACTIVE_CAPACITY_BYTES,
            detector_config: DetectorConfig::default(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RuntimeError {
    EmptyField(&'static str),
    UnknownFile(u32),
    DuplicateFile(u32),
    ExtentMissing(u32),
    LogicalSizeMismatch { file_id: u32 },
    Bootstrap(bootstrap::BootstrapError),
    StagingTooSmall,
}

impl From<bootstrap::BootstrapError> for RuntimeError {
    fn from(value: bootstrap::BootstrapError) -> Self {
        Self::Bootstrap(value)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransportFailure(pub String);

/// Feeder-side source link. Implementations own authentication/reconnection;
/// the runtime only sees frames already bound to the request it opened.
pub trait SourceTransport: Send {
    fn open(&mut self, request: &SelectionRequest) -> Result<(), TransportFailure>;
    fn poll(&mut self) -> Result<Option<DataFrame>, TransportFailure>;
    fn cancel(&mut self, request: &SelectionRequest);

    /// Epoch-bound control confirmation returned by the media handshake.
    /// Default `None` keeps fake transports explicit; a confirmation never
    /// substitutes for verified data (`SelectionController` still requires
    /// `is_ready()`).
    fn confirmation(&self) -> Option<(u64, u64)> {
        None
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Intent {
    pub file_id: u32,
    pub channel_id: String,
    pub observed_at_ms: u64,
}

#[derive(Debug, Clone)]
struct Worker {
    request: SelectionRequest,
    opened: bool,
}

struct ActiveView {
    published: Published,
    /// Bootstrap-spliced live window. `stream_start` is its absolute file
    /// offset; bytes before the window are no longer available and become
    /// NULL, never another channel's data.
    stream: RingData,
    splice: SpliceState,
    /// File and stream coordinates are separate. The TV may already have
    /// consumed a megabyte of probing/NULLs before the source becomes ready.
    anchor_file: u64,
    anchor_stream: u64,
    last_sequence: u64,
    resync: VecDeque<u64>,
    candidate: Option<(u64, bool, bool, bool)>,
}

impl ActiveView {
    /// Discover decodable anchor points on the worker, never by scanning or
    /// allocating a multi-megabyte window in the FUSE callback. The existing
    /// snapper's PAT/SPS checks are extended with PMT/PPS/IDR evidence.
    fn append(&mut self, bytes: &[u8]) {
        let start = self.stream.s_write();
        for (index, packet) in bytes.as_chunks::<TS_PKT_SZ>().0.iter().enumerate() {
            let pos = start.saturating_add((index * TS_PKT_SZ) as u64);
            if is_pat(packet, 0) {
                self.candidate = Some((pos, false, false, false));
            }
            if let Some((pat, pmt, sps, pps)) = self.candidate.as_mut() {
                let pid = ((u16::from(packet[1]) & 0x1f) << 8) | u16::from(packet[2]);
                if pid == self.splice.pmt_pid() {
                    *pmt = true;
                }
                if pid == self.splice.video_pid() {
                    *sps |= ts_has_sps(packet);
                    if let Some(off) = ts_payload_off(packet) {
                        let payload = &packet[off..];
                        let has_nal = |kind| {
                            payload
                                .windows(4)
                                .any(|w| w[..3] == [0, 0, 1] && w[3] & 0x1f == kind)
                        };
                        *pps |= has_nal(8);
                        if *pmt && *sps && *pps && has_nal(5) {
                            self.resync.push_back(*pat);
                            self.candidate = None;
                        }
                    }
                }
            }
        }
        self.stream.append(bytes);
        let oldest = self.stream.ring_old();
        while self.resync.front().is_some_and(|point| *point < oldest) || self.resync.len() > 4096 {
            self.resync.pop_front();
        }
    }

    fn resync_target(&self) -> Option<u64> {
        let target = self.stream.s_write().saturating_sub(1024 * 1024);
        self.resync
            .iter()
            .rev()
            .find(|point| **point <= target)
            .or_else(|| self.resync.front())
            .copied()
    }
}

#[derive(Default)]
struct ReadChain {
    last_end: Option<u64>,
    last_at_ms: u64,
    probe_end: Option<u64>,
    probe_count: u32,
    pace_deadline: Option<Instant>,
}

struct RuntimeState {
    selection: SelectionController,
    detector: ProbeDetector,
    pending: Option<Intent>,
    retry_intent: Option<Intent>,
    retry_at_ms: Option<u64>,
    dropped_intents: u64,
    worker: Option<Worker>,
    active: Option<ActiveView>,
    last_error: Option<String>,
    chains: HashMap<u32, ReadChain>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct WorkerReport {
    pub opened: bool,
    pub frames: usize,
    pub rejected: usize,
    pub committed: Option<Published>,
    pub error: Option<String>,
}

pub struct FavoritesRuntime {
    map: ExtentMap,
    files: HashMap<u32, FavoriteFile>,
    bootstraps: HashMap<u32, Bootstrap>,
    config: RuntimeConfig,
    started: Instant,
    state: Mutex<RuntimeState>,
    changed: Condvar,
}

impl FavoritesRuntime {
    pub fn new(
        map: ExtentMap,
        files: Vec<FavoriteFile>,
        config: RuntimeConfig,
    ) -> Result<Self, RuntimeError> {
        if config.session_id.trim().is_empty() {
            return Err(RuntimeError::EmptyField("session_id"));
        }
        if config.manifest_id.trim().is_empty() {
            return Err(RuntimeError::EmptyField("manifest_id"));
        }
        if config.min_ready_bytes < TS_PKT_SZ || config.active_capacity_bytes < TS_PKT_SZ {
            return Err(RuntimeError::StagingTooSmall);
        }
        let mut by_id = HashMap::new();
        let mut bootstraps = HashMap::new();
        for file in files {
            if file.channel_id.trim().is_empty() || file.path.trim().is_empty() {
                return Err(RuntimeError::EmptyField("file"));
            }
            if by_id.insert(file.file_id, file.clone()).is_some() {
                return Err(RuntimeError::DuplicateFile(file.file_id));
            }
            if map.file_extents(file.file_id).is_empty() {
                return Err(RuntimeError::ExtentMissing(file.file_id));
            }
            let bootstrap = Bootstrap::for_file(file.file_id, &file.channel_id);
            bootstrap.validate()?;
            bootstraps.insert(file.file_id, bootstrap);
        }
        if by_id.is_empty() {
            return Err(RuntimeError::EmptyField("files"));
        }
        let detector_config = config.detector_config;
        let staging_bytes = config.staging_bytes;
        Ok(Self {
            map,
            files: by_id,
            bootstraps,
            config,
            started: Instant::now(),
            state: Mutex::new(RuntimeState {
                selection: SelectionController::with_staging_limit(staging_bytes),
                detector: ProbeDetector::new(detector_config),
                pending: None,
                retry_intent: None,
                retry_at_ms: None,
                dropped_intents: 0,
                worker: None,
                active: None,
                last_error: None,
                chains: HashMap::new(),
            }),
            changed: Condvar::new(),
        })
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, RuntimeState> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Monotonic millisecond clock shared by the detector/selection worker.
    pub fn now_ms(&self) -> u64 {
        self.started.elapsed().as_millis().min(u128::from(u64::MAX)) as u64
    }

    pub fn route(&self, disk_offset: u64) -> MapRegion {
        self.map.route(disk_offset)
    }

    pub fn map(&self) -> &ExtentMap {
        &self.map
    }

    pub fn files(&self) -> &HashMap<u32, FavoriteFile> {
        &self.files
    }

    pub fn bootstrap(&self, file_id: u32) -> Option<&Bootstrap> {
        self.bootstraps.get(&file_id)
    }

    pub fn state(&self) -> SelectionState {
        self.lock().selection.state()
    }

    pub fn active(&self) -> Option<Published> {
        self.lock()
            .active
            .as_ref()
            .map(|active| active.published.clone())
    }

    /// Absolute `(start, len)` of the retained post-bootstrap live window.
    /// Offsets older than `start` are served as NULL, never as another file's
    /// bytes. Intended for diagnostics and bounded-memory tests.
    pub fn active_window(&self) -> Option<(u64, u64)> {
        self.lock().active.as_ref().map(|active| {
            (
                active.stream.ring_old(),
                active.stream.s_write() - active.stream.ring_old(),
            )
        })
    }

    pub fn pending_request(&self) -> Option<SelectionRequest> {
        self.lock().selection.pending().cloned()
    }

    pub fn pending_intent(&self) -> Option<Intent> {
        self.lock().pending.clone()
    }

    pub fn dropped_intents(&self) -> u64 {
        self.lock().dropped_intents
    }

    pub fn last_error(&self) -> Option<String> {
        self.lock().last_error.clone()
    }

    /// Records the newest detector intent. Only one preparation is retained;
    /// a different file/channel supersedes it (the old epoch is canceled by
    /// the worker before the new one opens).
    pub fn enqueue_intent(
        &self,
        file_id: u32,
        channel_id: &str,
        observed_at_ms: u64,
    ) -> Result<Intent, RuntimeError> {
        let file = self
            .files
            .get(&file_id)
            .ok_or(RuntimeError::UnknownFile(file_id))?;
        if file.channel_id != channel_id {
            return Err(RuntimeError::UnknownFile(file_id));
        }
        let intent = Intent {
            file_id,
            channel_id: channel_id.to_string(),
            observed_at_ms,
        };
        let mut state = self.lock();
        if let Some(existing) = &state.pending {
            if existing.file_id == intent.file_id && existing.channel_id == intent.channel_id {
                state.pending = Some(intent.clone());
                return Ok(intent);
            }
            state.dropped_intents = state.dropped_intents.saturating_add(1);
        }
        state.pending = Some(intent.clone());
        Ok(intent)
    }

    /// Tablet/control readiness ACK for the exact pending epoch. It is
    /// necessary but not sufficient: verified data and source readiness must
    /// already exist.
    pub fn tablet_ack(&self, generation: u64, epoch: u64) -> Result<(), SelectionError> {
        let mut state = self.lock();
        state.selection.tablet_ack(generation, epoch)
    }

    /// Runs one bounded worker step. No network call happens under the lock.
    pub fn process(&self, transport: &mut dyn SourceTransport, now_ms: u64) -> WorkerReport {
        let mut report = WorkerReport::default();
        let (cancel_old, start) = {
            let mut state = self.lock();
            if state.pending.is_some() {
                state.retry_intent = None;
                state.retry_at_ms = None;
            } else if let (Some(intent), Some(at)) = (state.retry_intent.clone(), state.retry_at_ms)
            {
                if now_ms >= at {
                    state.pending = Some(intent);
                    state.retry_intent = None;
                    state.retry_at_ms = None;
                }
            }
            let intent = state.pending.take();
            match intent {
                Some(intent) => {
                    let same = state.worker.as_ref().map(|worker| {
                        worker.request.file_id == intent.file_id
                            && worker.request.channel_id == intent.channel_id
                    });
                    if same == Some(true) {
                        (None, false)
                    } else {
                        let old = state.worker.take();
                        match state.selection.request(
                            &self.config.session_id,
                            &self.config.manifest_id,
                            intent.file_id,
                            &intent.channel_id,
                        ) {
                            Ok(request) => {
                                state.worker = Some(Worker {
                                    request,
                                    opened: false,
                                });
                                (old.map(|w| w.request), true)
                            }
                            Err(error) => {
                                state.last_error = Some(format!("request: {error:?}"));
                                report.error = state.last_error.clone();
                                (old.map(|w| w.request), false)
                            }
                        }
                    }
                }
                None => (None, false),
            }
        };
        if let Some(old) = cancel_old {
            transport.cancel(&old);
        }
        if !start {
            // A worker may already be open; keep polling it below.
        }

        let request = match self.lock().worker.as_ref().map(|w| w.request.clone()) {
            Some(request) => request,
            None => return report,
        };
        let opened = self
            .lock()
            .worker
            .as_ref()
            .map(|w| w.opened)
            .unwrap_or(false);
        if !opened {
            match transport.open(&request) {
                Ok(()) => {
                    self.lock().worker.as_mut().unwrap().opened = true;
                    report.opened = true;
                }
                Err(error) => {
                    let mut state = self.lock();
                    let generation = request.request_generation;
                    let _ = state.selection.cancel(generation);
                    state.worker = None;
                    state.last_error = Some(format!("open: {}", error.0));
                    // Rate-limited retry keeps a transient manager/source
                    // outage from losing the selected identity; the previous
                    // active window (if any) remains untouched.
                    state.retry_intent = Some(Intent {
                        file_id: request.file_id,
                        channel_id: request.channel_id.clone(),
                        observed_at_ms: now_ms,
                    });
                    state.retry_at_ms = Some(now_ms.saturating_add(1000));
                    report.error = state.last_error.clone();
                    return report;
                }
            }
        }

        for _ in 0..self.config.max_frames_per_step {
            let frame = match transport.poll() {
                Ok(Some(frame)) => frame,
                Ok(None) => break,
                Err(error) => {
                    let mut state = self.lock();
                    let _ = state.selection.cancel(request.request_generation);
                    state.worker = None;
                    state.last_error = Some(format!("poll: {}", error.0));
                    if state.pending.is_none() {
                        // Preparation and committed links both retry with
                        // a fresh epoch; a newer queued intent takes priority.
                        state.retry_intent = Some(Intent {
                            file_id: request.file_id,
                            channel_id: request.channel_id.clone(),
                            observed_at_ms: now_ms,
                        });
                        state.retry_at_ms = Some(now_ms.saturating_add(1000));
                    }
                    self.changed.notify_all();
                    report.error = state.last_error.clone();
                    return report;
                }
            };
            let mut state = self.lock();
            if state.selection.pending().is_some() {
                match state.selection.accept_data_frame(&frame) {
                    Ok(()) => report.frames += 1,
                    Err(error) => {
                        report.rejected += 1;
                        if matches!(error, SelectionError::StagingLimitExceeded { .. }) {
                            let _ = state.selection.cancel(request.request_generation);
                            state.worker = None;
                            state.last_error = Some(format!("preparation limit: {error:?}"));
                            report.error = state.last_error.clone();
                            self.changed.notify_all();
                            drop(state);
                            transport.cancel(&request);
                            return report;
                        }
                        continue;
                    }
                }
            } else if let Some(active) = state.active.as_mut() {
                // Post-commit live continuation: identity and monotonic
                // sequence are still enforced; only the exact published epoch
                // may append to the bounded window.
                let published = &active.published;
                if frame.session_id == published.session_id
                    && frame.manifest_id == published.manifest_id
                    && frame.file_id == published.file_id
                    && frame.channel_id == published.channel_id
                    && frame.request_generation == published.request_generation
                    && frame.source_epoch == published.source_epoch
                    && frame.sequence == active.last_sequence.saturating_add(1)
                {
                    match active.splice.rewrite(&frame.payload) {
                        Ok(bytes) => {
                            active.append(&bytes);
                            active.last_sequence = frame.sequence;
                            self.changed.notify_all();
                            report.frames += 1;
                        }
                        Err(error) => {
                            report.rejected += 1;
                            state.worker = None;
                            state.last_error = Some(format!("active transport: {error:?}"));
                            state.retry_intent = Some(Intent {
                                file_id: request.file_id,
                                channel_id: request.channel_id.clone(),
                                observed_at_ms: now_ms,
                            });
                            state.retry_at_ms = Some(now_ms.saturating_add(1000));
                            report.error = state.last_error.clone();
                            self.changed.notify_all();
                            drop(state);
                            transport.cancel(&request);
                            return report;
                        }
                    }
                } else {
                    report.rejected += 1;
                }
            } else {
                report.rejected += 1;
            }
            let pending_len = state
                .selection
                .pending_payload()
                .map(|p| p.len())
                .unwrap_or(0);
            let check = if state.selection.state() == SelectionState::Preparing
                && pending_len >= self.config.min_ready_bytes
            {
                state
                    .selection
                    .pending_payload()
                    .map(|payload| payload.to_vec())
            } else {
                None
            };
            drop(state);
            if let Some(payload) = check {
                // Bitstream parsing and allocation happen off the state lock.
                let validation = bootstrap::validate(&payload, None);
                let mut state = self.lock();
                let same = state.selection.pending().is_some_and(|pending| {
                    pending.request_generation == request.request_generation
                        && pending.source_epoch == request.source_epoch
                });
                if !same {
                    continue;
                }
                if validation.is_ok() {
                    let range = (0, payload.len() as u64);
                    let generation = request.request_generation;
                    let epoch = request.source_epoch;
                    if let Err(error) = state
                        .selection
                        .mark_source_ready(generation, epoch, range, now_ms)
                    {
                        state.last_error = Some(format!("ready: {error:?}"));
                    }
                } else if let Err(error) = validation {
                    let incomplete = matches!(
                        error,
                        bootstrap::BootstrapError::MissingPat
                            | bootstrap::BootstrapError::MissingPmt
                            | bootstrap::BootstrapError::MissingVideo
                            | bootstrap::BootstrapError::MissingAudio
                            | bootstrap::BootstrapError::MissingSps
                            | bootstrap::BootstrapError::MissingPps
                            | bootstrap::BootstrapError::MissingIdr
                            | bootstrap::BootstrapError::NoPcr
                            | bootstrap::BootstrapError::AudioNotAc3
                            | bootstrap::BootstrapError::SliceHeader
                    );
                    if !incomplete {
                        let _ = state.selection.cancel(request.request_generation);
                        state.worker = None;
                        state.last_error = Some(format!("source invariant: {error:?}"));
                        report.error = state.last_error.clone();
                        self.changed.notify_all();
                        drop(state);
                        transport.cancel(&request);
                        return report;
                    }
                }
            }
        }

        if let Some((generation, epoch)) = transport.confirmation() {
            if generation == request.request_generation && epoch == request.source_epoch {
                let mut state = self.lock();
                // Before verified data this returns `DataNotVerified` and is
                // deliberately ignored; a later step retries with the same
                // epoch. A confirmation alone can never commit.
                let _ = state.selection.tablet_ack(generation, epoch);
            }
        }

        let ready = self.lock().selection.ready_for_commit();
        if ready {
            let (snapshot, predecessor) = {
                let state = self.lock();
                match state.selection.commit_snapshot() {
                    Ok(snapshot) => {
                        let predecessor = state
                            .active
                            .as_ref()
                            .filter(|active| {
                                active.published.file_id == snapshot.request.file_id
                                    && active.published.channel_id == snapshot.request.channel_id
                            })
                            .map(|active| active.splice.clone());
                        (snapshot, predecessor)
                    }
                    Err(_) => return report,
                }
            };
            let raw = &snapshot.payload;
            let request_now = &snapshot.request;
            let digest =
                super::digest::sha256(&raw[snapshot.range.0 as usize..snapshot.range.1 as usize]);
            let Some(bootstrap) = self.bootstraps.get(&request_now.file_id) else {
                return report;
            };
            let splice = match predecessor {
                Some(predecessor) => predecessor.successor(raw),
                None => SpliceState::new(bootstrap.bytes(), raw),
            };
            match splice.and_then(|mut splice| splice.rewrite(raw).map(|bytes| (splice, bytes))) {
                Ok((splice, bytes)) => {
                    // The large allocation and initial copy are outside the
                    // state mutex; FUSE never waits behind a 128 MiB memset.
                    let capacity = self
                        .config
                        .active_capacity_bytes
                        .min(DEFAULT_ACTIVE_CAPACITY_BYTES);
                    let stream = RingData::with_capacity(capacity as u64);
                    let mut candidate = ActiveView {
                        published: snapshot.publication(digest, now_ms),
                        stream,
                        splice,
                        anchor_file: 0,
                        anchor_stream: 0,
                        last_sequence: snapshot.sequence,
                        resync: VecDeque::new(),
                        candidate: None,
                    };
                    candidate.append(&bytes);
                    let mut state = self.lock();
                    let same = state
                        .selection
                        .pending()
                        .map(|pending| {
                            pending.request_generation == request_now.request_generation
                                && pending.source_epoch == request_now.source_epoch
                        })
                        .unwrap_or(false);
                    if same {
                        match state.selection.commit_prehashed(&snapshot, digest, now_ms) {
                            Ok(published) => {
                                state.detector.on_committed(request_now.file_id, now_ms);
                                let chain_end = state
                                    .chains
                                    .get(&request_now.file_id)
                                    .and_then(|chain| chain.last_end)
                                    .unwrap_or(bootstrap.len() as u64)
                                    .max(bootstrap.len() as u64);
                                candidate.published = published.clone();
                                candidate.anchor_file =
                                    chain_end.div_ceil(TS_PKT_SZ as u64) * TS_PKT_SZ as u64;
                                state.active = Some(candidate);
                                self.changed.notify_all();
                                state.retry_intent = None;
                                state.retry_at_ms = None;
                                // Keep the same authenticated epoch open so
                                // post-commit frames extend the live window.
                                report.committed = Some(published);
                            }
                            Err(SelectionError::TabletNotReady) => {
                                // Keep the previous active source; a later
                                // control ACK may still commit this epoch.
                            }
                            Err(error) => {
                                state.last_error = Some(format!("commit: {error:?}"));
                                report.error = state.last_error.clone();
                            }
                        }
                    }
                }
                Err(error) => {
                    let mut state = self.lock();
                    let _ = state.selection.cancel(request_now.request_generation);
                    state.worker = None;
                    state.last_error = Some(format!("transition: {error:?}"));
                    report.error = state.last_error.clone();
                }
            }
        }
        report
    }

    /// Serves bytes for one favorite file. `[0, bootstrap_len)` is immutable;
    /// beyond it only the active epoch for the same `file_id` is returned.
    /// Inactive/tail/mismatched offsets get phase-aligned NULL packets.
    pub fn serve_file(&self, file_id: u32, foff: u64, dst: &mut [u8]) -> usize {
        let read_started = Instant::now();
        if dst.is_empty() {
            return 0;
        }
        let Some(bootstrap) = self.bootstraps.get(&file_id) else {
            fill_null_at(dst, foff);
            return dst.len();
        };
        let boot_len = bootstrap.len() as u64;
        let now = self.now_ms();
        let (chain_read, mut reanchor) = {
            let mut state = self.lock();
            // Tail/thumbnail probes cannot modify the active read chain.
            // A completed chain remains a chain beyond the opening window,
            // including during outages. Applying the far-probe shortcut first
            // would disable waiting/pacing at exactly 8 MiB with no source.
            let contiguous = state
                .chains
                .get(&file_id)
                .is_some_and(|chain| chain.last_end == Some(foff));
            let far_probe = foff >= 8 * 1024 * 1024
                && !contiguous
                && state
                    .active
                    .as_ref()
                    .filter(|active| active.published.file_id == file_id)
                    .map(|active| {
                        foff.saturating_sub(active.anchor_file)
                            .saturating_add(active.anchor_stream)
                            > active.stream.s_write().saturating_add(2 * 1024 * 1024)
                    })
                    .unwrap_or(true);
            let chain = state.chains.entry(file_id).or_default();
            let restart = foff == 0
                && chain.last_end.is_some()
                && now.saturating_sub(chain.last_at_ms) > 1500;
            let sequential = !far_probe
                && (chain.last_end == Some(foff)
                    || (chain.last_end.is_none() && foff < 8 * 1024 * 1024)
                    || restart);
            let mut confirmed_jump = false;
            if !far_probe && !sequential {
                if chain.probe_end == Some(foff) {
                    chain.probe_count = chain.probe_count.saturating_add(1);
                } else {
                    chain.probe_count = 1;
                }
                chain.probe_end = Some(foff.saturating_add(dst.len() as u64));
                confirmed_jump = chain.probe_count >= 3;
            } else if sequential {
                chain.probe_end = None;
                chain.probe_count = 0;
            }
            if let Some(intent) = state.detector.observe(file_id, foff, dst.len() as u64, now) {
                let channel_id = self.files.get(&file_id).map(|file| file.channel_id.clone());
                if let Some(channel_id) = channel_id {
                    if state.pending.as_ref().is_some_and(|pending| {
                        pending.file_id == file_id && pending.channel_id == channel_id
                    }) {
                        state.pending = Some(Intent {
                            file_id,
                            channel_id,
                            observed_at_ms: intent.detected_at_ms,
                        });
                    } else {
                        if state.pending.is_some() {
                            state.dropped_intents = state.dropped_intents.saturating_add(1);
                        }
                        state.pending = Some(Intent {
                            file_id,
                            channel_id,
                            observed_at_ms: intent.detected_at_ms,
                        });
                    }
                }
            }
            (sequential || confirmed_jump, restart || confirmed_jump)
        };
        let mut done = 0usize;
        let mut response_epoch = None;
        let budget_end = Instant::now() + Duration::from_millis(500);
        if foff < boot_len {
            let available = (boot_len - foff).min(dst.len() as u64) as usize;
            dst[..available]
                .copy_from_slice(&bootstrap.bytes()[foff as usize..foff as usize + available]);
            done = available;
        }
        let bootstrap_copied = done;
        while done < dst.len() {
            let mut state = self.lock();
            let wanted = foff.saturating_add(done as u64);
            let preparing = state
                .pending
                .as_ref()
                .is_some_and(|intent| intent.file_id == file_id)
                || state
                    .retry_intent
                    .as_ref()
                    .is_some_and(|intent| intent.file_id == file_id)
                || state
                    .worker
                    .as_ref()
                    .is_some_and(|worker| worker.request.file_id == file_id);
            if let Some(active) = state
                .active
                .as_mut()
                .filter(|active| active.published.file_id == file_id)
            {
                let epoch = active.published.source_epoch;
                if response_epoch.is_some_and(|previous| previous != epoch) {
                    // One SCSI response must never contain two source epochs.
                    break;
                }
                if wanted < active.anchor_file {
                    let count =
                        (active.anchor_file - wanted).min((dst.len() - done) as u64) as usize;
                    fill_null_at(&mut dst[done..done + count], wanted);
                    done += count;
                    continue;
                }
                let mut pos = active
                    .anchor_stream
                    .saturating_add(wanted - active.anchor_file);
                if chain_read && (reanchor || pos < active.stream.ring_old()) {
                    // Preserve the TS byte phase. At most 187 NULL bytes bridge
                    // from an arbitrary block boundary to the new packet.
                    active.anchor_file = wanted.div_ceil(TS_PKT_SZ as u64) * TS_PKT_SZ as u64;
                    let Some(target) = active.resync_target() else {
                        // No retained PAT+PMT+SPS+PPS+IDR yet. Never remap
                        // the reader to arbitrary bytes of a partial GOP.
                        break;
                    };
                    active.anchor_stream = target;
                    reanchor = false;
                    if wanted < active.anchor_file {
                        continue;
                    }
                    pos = active.anchor_stream;
                }
                if pos >= active.stream.ring_old() && pos < active.stream.s_write() {
                    let available =
                        (active.stream.s_write() - pos).min((dst.len() - done) as u64) as usize;
                    if active
                        .stream
                        .copy_resident(pos, &mut dst[done..done + available])
                    {
                        response_epoch = Some(epoch);
                        done += available;
                        continue;
                    }
                }
                if !chain_read || pos < active.stream.ring_old() {
                    break;
                }
            } else if !preparing || !chain_read {
                break;
            }
            let now = Instant::now();
            if now >= budget_end {
                break;
            }
            // Condvar releases the mutex. Every cooperative wait is <=50 ms;
            // the feeder remains free to append and wake the reader.
            let step = (budget_end - now).min(Duration::from_millis(50));
            let (guard, _) = self
                .changed
                .wait_timeout(state, step)
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            drop(guard);
        }
        if chain_read {
            let mut state = self.lock();
            let chain = state.chains.entry(file_id).or_default();
            chain.last_end = Some(foff.saturating_add(dst.len() as u64));
            chain.last_at_ms = self.now_ms();
            if done < dst.len() {
                if let Some(active) = state
                    .active
                    .as_mut()
                    .filter(|active| active.published.file_id == file_id)
                {
                    // On a bounded outage preserve the next stream byte rather
                    // than skipping live data by the NULL-filled request size.
                    let wanted = foff.saturating_add(done as u64);
                    if wanted >= active.anchor_file {
                        let pos = active
                            .anchor_stream
                            .saturating_add(wanted - active.anchor_file);
                        if pos >= active.stream.s_write() {
                            active.anchor_file = foff
                                .saturating_add(dst.len() as u64)
                                .div_ceil(TS_PKT_SZ as u64)
                                * TS_PKT_SZ as u64;
                            active.anchor_stream = active.stream.s_write();
                        }
                    }
                }
            }
        }
        if done < dst.len() {
            fill_null_at(&mut dst[done..], foff.saturating_add(done as u64));
            done = dst.len();
        }
        let charge = dst.len().saturating_sub(bootstrap_copied);
        if charge > 0 && chain_read {
            let until = {
                let mut state = self.lock();
                let chain = state.chains.entry(file_id).or_default();
                // 1 MiB/s exceeds our 6 Mbit/s muxrate. Edge waits provide
                // the source clock; this ceiling also prevents NULL floods
                // on retry, crediting time already spent waiting for data.
                let floor = read_started
                    .checked_sub(Duration::from_millis(100))
                    .unwrap_or(read_started);
                let base = chain.pace_deadline.unwrap_or(read_started).max(floor);
                let nanos =
                    ((charge as u128) * 1_000_000_000 / (1024 * 1024)).min(u64::MAX as u128) as u64;
                let until = base + Duration::from_nanos(nanos);
                chain.pace_deadline = Some(until);
                until
            };
            while Instant::now() < until {
                std::thread::sleep(
                    until
                        .saturating_duration_since(Instant::now())
                        .min(Duration::from_millis(50)),
                );
            }
        }
        done
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::favorites::extents::ExtentMap;
    use crate::vfs::ntfs_mft::parse_runlist;
    use std::collections::VecDeque;

    const SBT: u32 = 7;
    const RECORD: u32 = 8;

    fn runs(lcn: u64, clusters: u64) -> Vec<crate::vfs::ntfs_mft::RunPiece> {
        let mut encoded = vec![0x44];
        encoded.extend_from_slice(&(clusters as u32).to_le_bytes());
        encoded.extend_from_slice(&(lcn as i32).to_le_bytes());
        encoded.push(0);
        parse_runlist(&encoded)
    }

    fn runtime() -> FavoritesRuntime {
        runtime_with_capacity(DEFAULT_ACTIVE_CAPACITY_BYTES)
    }

    fn runtime_with_capacity(capacity: usize) -> FavoritesRuntime {
        let mut map = ExtentMap::new(32 * 1024 * 1024).unwrap();
        map.add_metadata(0, 1024 * 1024).unwrap();
        map.add_file_runs(SBT, &runs(256, 1024), 4 * 1024 * 1024)
            .unwrap();
        map.add_file_runs(RECORD, &runs(1280, 1024), 4 * 1024 * 1024)
            .unwrap();
        let files = vec![
            FavoriteFile {
                file_id: SBT,
                channel_id: "sbt".into(),
                path: "Favoritos/Abertos/SBT.trp".into(),
                logical_size: 4 * 1024 * 1024,
            },
            FavoriteFile {
                file_id: RECORD,
                channel_id: "record".into(),
                path: "Favoritos/Abertos/Record.trp".into(),
                logical_size: 4 * 1024 * 1024,
            },
        ];
        let mut config = RuntimeConfig::new("session", "manifest");
        config.min_ready_bytes = 64 * 1024;
        config.active_capacity_bytes = capacity;
        FavoritesRuntime::new(map, files, config).unwrap()
    }

    /// Live prefixes are structurally valid and distinguishable by the identity
    /// section for their own channel.
    fn live(channel: &str) -> Vec<u8> {
        let id = if channel == "sbt" { SBT } else { RECORD };
        Bootstrap::for_file(id, channel).bytes().to_vec()
    }

    struct FakeTransport {
        current: Option<SelectionRequest>,
        queue: VecDeque<DataFrame>,
        opened: Vec<SelectionRequest>,
        cancelled: Vec<SelectionRequest>,
        fail_next_open: bool,
        fail_next_poll: bool,
        pause: bool,
    }

    impl FakeTransport {
        fn new() -> Self {
            Self {
                current: None,
                queue: VecDeque::new(),
                opened: Vec::new(),
                cancelled: Vec::new(),
                fail_next_open: false,
                fail_next_poll: false,
                pause: false,
            }
        }

        fn push(&mut self, frame: DataFrame) {
            self.queue.push_back(frame);
        }

        fn supply(&mut self) {
            let Some(request) = self.current.clone() else {
                return;
            };
            let payload = live(&request.channel_id);
            let chunk = (64 * 1024 / TS_PKT_SZ) * TS_PKT_SZ;
            for (index, chunk_bytes) in payload.chunks(chunk).enumerate() {
                let sequence = index as u64 + 1;
                self.queue.push_back(DataFrame {
                    session_id: request.session_id.clone(),
                    manifest_id: request.manifest_id.clone(),
                    file_id: request.file_id,
                    channel_id: request.channel_id.clone(),
                    request_generation: request.request_generation,
                    source_epoch: request.source_epoch,
                    sequence,
                    payload: chunk_bytes.to_vec(),
                });
            }
        }
    }

    impl SourceTransport for FakeTransport {
        fn open(&mut self, request: &SelectionRequest) -> Result<(), TransportFailure> {
            if self.fail_next_open {
                self.fail_next_open = false;
                return Err(TransportFailure("simulated".into()));
            }
            self.opened.push(request.clone());
            self.current = Some(request.clone());
            if !self.pause {
                self.supply();
            }
            Ok(())
        }

        fn poll(&mut self) -> Result<Option<DataFrame>, TransportFailure> {
            if self.fail_next_poll {
                self.fail_next_poll = false;
                return Err(TransportFailure("simulated poll failure".into()));
            }
            Ok(self.queue.pop_front())
        }

        fn cancel(&mut self, request: &SelectionRequest) {
            self.cancelled.push(request.clone());
            self.current = None;
            self.queue.clear();
        }
    }

    fn drive_until_ready(runtime: &FavoritesRuntime, transport: &mut FakeTransport) {
        for step in 0..8 {
            let report = runtime.process(transport, 1000 + step * 10);
            assert!(report.error.is_none(), "{report:?}");
            if runtime.state() == SelectionState::Ready {
                return;
            }
        }
        panic!("did not become ready: {:?}", runtime.state());
    }

    fn commit(runtime: &FavoritesRuntime, transport: &mut FakeTransport, now: u64) -> Published {
        drive_until_ready(runtime, transport);
        let request = runtime.pending_request().unwrap();
        runtime
            .tablet_ack(request.request_generation, request.source_epoch)
            .unwrap();
        let report = runtime.process(transport, now);
        report.committed.expect("commit")
    }

    #[test]
    fn first_channel_uses_bootstrap_then_commits_matching_epoch() {
        let runtime = runtime();
        let mut transport = FakeTransport::new();
        let mut read = vec![0u8; 188 * 4];
        runtime.serve_file(SBT, 0, &mut read);
        let bootstrap = runtime.bootstrap(SBT).unwrap();
        assert_eq!(&read[..], &bootstrap.bytes()[..188 * 4]);
        assert_eq!(runtime.active(), None);

        transport.pause = true;
        runtime.enqueue_intent(SBT, "sbt", 10).unwrap();
        runtime.process(&mut transport, 20);
        let request = runtime.pending_request().unwrap();
        assert_eq!(request.channel_id, "sbt");
        assert_eq!(
            runtime.tablet_ack(request.request_generation, request.source_epoch),
            Err(SelectionError::DataNotVerified)
        );
        transport.pause = false;
        transport.supply();
        let published = commit(&runtime, &mut transport, 50);
        assert_eq!(published.file_id, SBT);
        assert_eq!(runtime.active().unwrap().source_epoch, request.source_epoch);

        // Prefix served before the ACK is still byte-identical afterwards.
        let mut after = vec![0u8; 188 * 4];
        runtime.serve_file(SBT, 0, &mut after);
        assert_eq!(after, read);
        assert!(runtime.serve_file(SBT, bootstrap.len() as u64, &mut read) > 0);
    }

    #[test]
    fn a_to_b_to_a_never_leaks_the_active_channel() {
        let runtime = runtime();
        let mut transport = FakeTransport::new();
        runtime.enqueue_intent(SBT, "sbt", 0).unwrap();
        commit(&runtime, &mut transport, 10);
        let sbt_epoch = runtime.active().unwrap().source_epoch;

        // B prepares while A remains published.
        runtime.enqueue_intent(RECORD, "record", 20).unwrap();
        runtime.process(&mut transport, 30);
        assert_eq!(runtime.state(), SelectionState::Ready);
        assert_eq!(runtime.active().unwrap().file_id, SBT);
        let mut b_read = vec![0u8; 4096];
        runtime.serve_file(RECORD, 0, &mut b_read);
        assert_eq!(
            &b_read[..],
            &runtime.bootstrap(RECORD).unwrap().bytes()[..4096]
        );

        commit(&runtime, &mut transport, 40);
        assert_eq!(runtime.active().unwrap().file_id, RECORD);

        // Back to A gets a fresh epoch, not a silent reuse of B.
        runtime.enqueue_intent(SBT, "sbt", 50).unwrap();
        commit(&runtime, &mut transport, 60);
        let active = runtime.active().unwrap();
        assert_eq!(active.file_id, SBT);
        assert!(active.source_epoch > sbt_epoch);
        assert_ne!(active.channel_id, "record");
    }

    #[test]
    fn stale_frames_after_new_intent_do_not_publish() {
        let runtime = runtime();
        let mut transport = FakeTransport::new();
        runtime.enqueue_intent(SBT, "sbt", 0).unwrap();
        commit(&runtime, &mut transport, 10);

        runtime.enqueue_intent(RECORD, "record", 20).unwrap();
        runtime.process(&mut transport, 30);
        let b_request = runtime.pending_request().unwrap();
        let stale = DataFrame {
            session_id: b_request.session_id.clone(),
            manifest_id: b_request.manifest_id.clone(),
            file_id: SBT,
            channel_id: "sbt".into(),
            request_generation: b_request.request_generation - 1,
            source_epoch: b_request.source_epoch - 1,
            sequence: 1,
            payload: live("sbt")[..188].to_vec(),
        };
        transport.push(stale);
        let report = runtime.process(&mut transport, 40);
        assert!(report.rejected >= 1);
        assert_eq!(runtime.active().unwrap().file_id, SBT);

        // A newer intent cancels B and B's frames can never become active.
        runtime.enqueue_intent(SBT, "sbt", 50).unwrap();
        let report = runtime.process(&mut transport, 60);
        assert!(report.error.is_none(), "{report:?}");
        assert_eq!(transport.cancelled.len(), 2);
        assert_eq!(transport.cancelled[0].file_id, SBT);
        assert_eq!(transport.cancelled[1].file_id, RECORD);
    }

    #[test]
    fn transition_failure_keeps_previous_source_and_supersedes_queue() {
        let runtime = runtime();
        let mut transport = FakeTransport::new();
        runtime.enqueue_intent(SBT, "sbt", 0).unwrap();
        commit(&runtime, &mut transport, 10);
        let active_before = runtime.active().unwrap();

        // B opens but its "live" frame is structurally invalid, so readiness
        // validation never succeeds and no B bytes can be published.
        struct BadTransport {
            opened: Option<SelectionRequest>,
        }
        impl SourceTransport for BadTransport {
            fn open(&mut self, request: &SelectionRequest) -> Result<(), TransportFailure> {
                self.opened = Some(request.clone());
                Ok(())
            }
            fn poll(&mut self) -> Result<Option<DataFrame>, TransportFailure> {
                let request = self.opened.clone().unwrap();
                Ok(Some(DataFrame {
                    session_id: request.session_id.clone(),
                    manifest_id: request.manifest_id.clone(),
                    file_id: request.file_id,
                    channel_id: request.channel_id.clone(),
                    request_generation: request.request_generation,
                    source_epoch: request.source_epoch,
                    sequence: 1,
                    payload: vec![0u8; TS_PKT_SZ],
                }))
            }
            fn cancel(&mut self, _request: &SelectionRequest) {}
        }
        let mut bad = BadTransport { opened: None };
        runtime.enqueue_intent(RECORD, "record", 20).unwrap();
        for step in 0..4 {
            let report = runtime.process(&mut bad, 30 + step);
            assert!(report.error.is_none());
        }
        assert_eq!(runtime.active().unwrap(), active_before);
        assert_eq!(runtime.state(), SelectionState::Preparing);

        // A different intent supersedes the failed/stalled one.
        runtime.enqueue_intent(SBT, "sbt", 60).unwrap();
        runtime.process(&mut transport, 70);
        assert_eq!(runtime.state(), SelectionState::Ready);
        assert_eq!(runtime.active().unwrap(), active_before);
    }

    #[test]
    fn broken_active_link_reopens_a_fresh_epoch_and_keeps_the_old_window() {
        let runtime = runtime();
        let mut transport = FakeTransport::new();
        runtime.enqueue_intent(SBT, "sbt", 0).unwrap();
        let old = commit(&runtime, &mut transport, 10);
        let old_window = runtime.active_window().unwrap();
        let mut previous_bytes = vec![0; old_window.1 as usize];
        runtime
            .lock()
            .active
            .as_ref()
            .unwrap()
            .stream
            .read_at(old_window.0, &mut previous_bytes);

        transport.fail_next_poll = true;
        let report = runtime.process(&mut transport, 1000);
        assert!(report.error.is_some());
        assert_eq!(runtime.active().unwrap(), old);
        assert_eq!(runtime.active_window().unwrap(), old_window);

        // The retry is rate-limited, then opens a new generation/epoch.
        let report = runtime.process(&mut transport, 1500);
        assert!(report.frames == 0);
        let report = runtime.process(&mut transport, 2100);
        assert!(report.error.is_none(), "{report:?}");
        assert!(report.opened);
        assert!(runtime.pending_request().unwrap().source_epoch > old.source_epoch);
        let request = runtime.pending_request().unwrap();
        runtime
            .tablet_ack(request.request_generation, request.source_epoch)
            .unwrap();
        let report = runtime.process(&mut transport, 2200);
        let new = report.committed.expect("reconnected commit");
        assert_eq!(new.file_id, SBT);
        assert!(new.source_epoch > old.source_epoch);
        assert_eq!(runtime.active().unwrap(), new);
        let (_, count) = runtime.active_window().unwrap();
        let mut replacement = vec![0; count as usize];
        runtime
            .lock()
            .active
            .as_ref()
            .unwrap()
            .stream
            .read_at(0, &mut replacement);
        previous_bytes.extend(replacement);
        bootstrap::validate(&previous_bytes, None)
            .expect("reconnect must not reset PCR/CC to slate");
    }

    #[test]
    fn failed_initial_preparation_retries_with_fresh_epoch() {
        let runtime = runtime();
        let mut transport = FakeTransport::new();
        transport.pause = true;
        runtime.enqueue_intent(SBT, "sbt", 0).unwrap();
        runtime.process(&mut transport, 10);
        let old = runtime.pending_request().unwrap();
        transport.fail_next_poll = true;
        assert!(runtime.process(&mut transport, 20).error.is_some());
        assert!(runtime.active().is_none());
        assert!(!runtime.process(&mut transport, 1000).opened);
        let report = runtime.process(&mut transport, 1020);
        assert!(report.opened);
        assert!(runtime.pending_request().unwrap().source_epoch > old.source_epoch);
    }

    #[test]
    fn transient_failure_retry_does_not_override_newer_intent() {
        let runtime = runtime();
        let mut transport = FakeTransport::new();
        transport.pause = true;
        runtime.enqueue_intent(SBT, "sbt", 0).unwrap();
        runtime.process(&mut transport, 10);
        transport.fail_next_poll = true;
        runtime.process(&mut transport, 20);
        runtime.enqueue_intent(RECORD, "record", 30).unwrap();
        runtime.process(&mut transport, 40);
        assert_eq!(runtime.pending_request().unwrap().file_id, RECORD);
        runtime.process(&mut transport, 2000);
        assert_eq!(runtime.pending_request().unwrap().file_id, RECORD);
    }

    #[test]
    fn pending_queue_is_bounded_and_reopen_failure_keeps_active() {
        let runtime = runtime();
        let mut transport = FakeTransport::new();
        runtime.enqueue_intent(SBT, "sbt", 0).unwrap();
        commit(&runtime, &mut transport, 10);
        let active_before = runtime.active().unwrap();

        for index in 0..32 {
            let file = if index % 2 == 0 { SBT } else { RECORD };
            let channel = if file == SBT { "sbt" } else { "record" };
            runtime.enqueue_intent(file, channel, 100 + index).unwrap();
        }
        assert_eq!(runtime.dropped_intents(), 31);
        assert!(runtime.pending_intent().is_some());

        transport.fail_next_open = true;
        runtime.process(&mut transport, 300);
        assert_eq!(runtime.active().unwrap(), active_before);
        assert!(runtime.last_error().unwrap().contains("open:"));
    }

    #[test]
    fn serving_tail_and_repeated_offsets_is_stable() {
        let runtime = runtime();
        let mut transport = FakeTransport::new();
        runtime.enqueue_intent(SBT, "sbt", 0).unwrap();
        commit(&runtime, &mut transport, 10);
        let tail = ((runtime.bootstrap(SBT).unwrap().len() as u64 + 16 * 1024 * 1024)
            / TS_PKT_SZ as u64)
            * TS_PKT_SZ as u64;
        let mut a = vec![0u8; 188 * 3];
        let mut b = vec![0u8; 188 * 3];
        runtime.serve_file(SBT, tail, &mut a);
        runtime.serve_file(SBT, tail, &mut b);
        assert_eq!(a, b);
        assert!(a.chunks(TS_PKT_SZ).all(|p| p[0] == 0x47));
    }

    #[test]
    fn post_commit_frames_extend_a_bounded_window_without_reusing_old_bytes() {
        let runtime = runtime();
        let mut transport = FakeTransport::new();
        runtime.enqueue_intent(SBT, "sbt", 0).unwrap();
        commit(&runtime, &mut transport, 10);
        let (start, len) = runtime.active_window().expect("window");
        assert_eq!(start, 0);
        assert!(len > 0);

        let request = runtime.active().unwrap();
        transport.push(DataFrame {
            session_id: request.session_id.clone(),
            manifest_id: request.manifest_id.clone(),
            file_id: request.file_id,
            channel_id: request.channel_id.clone(),
            request_generation: request.request_generation,
            source_epoch: request.source_epoch,
            sequence: request.sequence + 1,
            payload: crate::engine::ring::NULL_PKT.to_vec(),
        });
        let report = runtime.process(&mut transport, 20);
        assert_eq!(report.frames, 1);
        assert_eq!(runtime.active_window().unwrap().1, len + TS_PKT_SZ as u64);

        // A stale/foreign frame is rejected and never appended.
        transport.push(DataFrame {
            session_id: request.session_id,
            manifest_id: request.manifest_id,
            file_id: request.file_id,
            channel_id: request.channel_id,
            request_generation: request.request_generation,
            source_epoch: request.source_epoch - 1,
            sequence: request.sequence + 2,
            payload: crate::engine::ring::NULL_PKT.to_vec(),
        });
        let report = runtime.process(&mut transport, 30);
        assert_eq!(report.rejected, 1);
        assert_eq!(runtime.active_window().unwrap().1, len + TS_PKT_SZ as u64);
    }

    #[test]
    fn disk_reads_emit_one_bounded_detector_intent() {
        let runtime = runtime();
        let mut buf = vec![0u8; 256 * 1024];
        assert!(runtime.pending_intent().is_none());
        for offset in [0u64, 256 * 1024, 512 * 1024] {
            runtime.serve_file(SBT, offset, &mut buf);
        }
        assert!(runtime.pending_intent().is_none());
        runtime.serve_file(SBT, 768 * 1024, &mut buf);
        let intent = runtime.pending_intent().expect("intent after 1 MiB");
        assert_eq!(intent.file_id, SBT);
        assert_eq!(intent.channel_id, "sbt");
        // Reads of another file supersede the pending intent without using it
        // for serving; the other file still gets its own bootstrap.
        for offset in [0u64, 256 * 1024, 512 * 1024, 768 * 1024] {
            runtime.serve_file(RECORD, offset, &mut buf);
        }
        assert_eq!(runtime.pending_intent().unwrap().file_id, RECORD);
        assert_eq!(runtime.dropped_intents(), 1);
    }

    #[test]
    fn commit_anchors_after_completed_probe_responses_not_at_bootstrap_end() {
        let runtime = runtime();
        let mut read = vec![0u8; 256 * 1024];
        let block = read.len() as u64;
        let consumed = runtime.bootstrap(SBT).unwrap().len() as u64;
        let consumed = consumed.div_ceil(block) * block + block;
        for offset in (0..consumed).step_by(block as usize) {
            runtime.serve_file(SBT, offset, &mut read);
        }
        let mut transport = FakeTransport::new();
        commit(&runtime, &mut transport, 1000);
        let offset = consumed;
        let pad = (offset.div_ceil(TS_PKT_SZ as u64) * TS_PKT_SZ as u64 - offset) as usize;
        let mut expected = vec![0u8; TS_PKT_SZ * 4];
        {
            let state = runtime.lock();
            let active = state.active.as_ref().unwrap();
            assert_eq!(active.anchor_stream, 0);
            assert_eq!(active.anchor_file, offset + pad as u64);
            active.stream.read_at(0, &mut expected);
        }
        let mut after = vec![0u8; pad + expected.len()];
        runtime.serve_file(SBT, offset, &mut after);
        assert_eq!(&after[pad..], &expected);
        assert_eq!(after[pad], 0x47);
    }

    #[test]
    fn ring_capacity_is_bytes_and_feeder_overwrites_without_growth() {
        let capacity = 128 * 1024;
        let runtime = runtime_with_capacity(capacity);
        let mut transport = FakeTransport::new();
        runtime.enqueue_intent(SBT, "sbt", 0).unwrap();
        let published = commit(&runtime, &mut transport, 10);
        let (oldest, retained) = runtime.active_window().unwrap();
        assert!(oldest > 0);
        assert!(retained <= capacity as u64);
        {
            let state = runtime.lock();
            let active = state.active.as_ref().unwrap();
            assert_eq!(
                active.stream.capacity(),
                (capacity / TS_PKT_SZ * TS_PKT_SZ) as u64
            );
        }
        let payload = crate::engine::ring::NULL_PKT.repeat(1024);
        transport.push(DataFrame {
            session_id: published.session_id,
            manifest_id: published.manifest_id,
            file_id: published.file_id,
            channel_id: published.channel_id,
            request_generation: published.request_generation,
            source_epoch: published.source_epoch,
            sequence: published.sequence + 1,
            payload,
        });
        assert_eq!(runtime.process(&mut transport, 20).frames, 1);
        let (new_oldest, retained) = runtime.active_window().unwrap();
        assert!(new_oldest > oldest);
        assert!(retained <= capacity as u64);
    }

    #[test]
    fn tail_probes_leave_completed_chain_unchanged() {
        let runtime = runtime();
        let mut read = vec![0; 4096];
        runtime.serve_file(SBT, 0, &mut read);
        let end = runtime.lock().chains[&SBT].last_end;
        for offset in [
            32 * 1024 * 1024,
            32 * 1024 * 1024 + 4096,
            32 * 1024 * 1024 + 8192,
        ] {
            runtime.serve_file(SBT, offset, &mut read);
        }
        assert_eq!(runtime.lock().chains[&SBT].last_end, end);
        assert!(runtime.pending_intent().is_none());
    }

    #[test]
    fn live_edge_wait_releases_mutex_and_wakes_on_append() {
        use std::sync::Arc;
        let runtime = Arc::new(runtime());
        let mut transport = FakeTransport::new();
        runtime.enqueue_intent(SBT, "sbt", 0).unwrap();
        let published = commit(&runtime, &mut transport, 10);
        let edge = {
            let mut state = runtime.lock();
            let active = state.active.as_ref().unwrap();
            let edge = active.anchor_file + active.stream.s_write();
            state.chains.entry(SBT).or_default().last_end = Some(edge);
            edge
        };
        let reader_runtime = Arc::clone(&runtime);
        let started = Instant::now();
        let reader = std::thread::spawn(move || {
            let mut read = vec![0; TS_PKT_SZ];
            reader_runtime.serve_file(SBT, edge, &mut read);
            read
        });
        std::thread::sleep(Duration::from_millis(40));
        // A lock held during the edge wait would deadlock this worker.
        transport.push(DataFrame {
            session_id: published.session_id,
            manifest_id: published.manifest_id,
            file_id: published.file_id,
            channel_id: published.channel_id,
            request_generation: published.request_generation,
            source_epoch: published.source_epoch,
            sequence: published.sequence + 1,
            payload: crate::engine::ring::NULL_PKT.to_vec(),
        });
        assert_eq!(runtime.process(&mut transport, 20).frames, 1);
        let read = reader.join().unwrap();
        assert_eq!(read[0], 0x47);
        assert_eq!(
            (u16::from(read[1] & 0x1f) << 8) | u16::from(read[2]),
            0x1fff
        );
        assert!(started.elapsed() < Duration::from_millis(450));
    }

    #[test]
    fn retry_without_active_source_waits_without_blocking_the_worker_lock() {
        let runtime = runtime();
        let mut transport = FakeTransport::new();
        transport.fail_next_open = true;
        runtime.enqueue_intent(SBT, "sbt", 0).unwrap();
        assert!(runtime.process(&mut transport, 10).error.is_some());
        let offset = runtime.bootstrap(SBT).unwrap().len() as u64;
        let mut read = vec![0; 4096];
        let started = Instant::now();
        runtime.serve_file(SBT, offset, &mut read);
        assert!(started.elapsed() >= Duration::from_millis(450));
        assert!(started.elapsed() < Duration::from_millis(900));
        assert_eq!(runtime.lock().chains[&SBT].last_end, Some(offset + 4096));
    }

    #[test]
    fn null_chain_without_source_is_paced_but_tail_probes_are_immediate() {
        let runtime = runtime();
        let offset = runtime.bootstrap(SBT).unwrap().len() as u64;
        let mut read = vec![0; 64 * 1024];
        let started = Instant::now();
        for index in 0..8 {
            runtime.serve_file(SBT, offset + index * read.len() as u64, &mut read);
        }
        assert!(started.elapsed() >= Duration::from_millis(400));
        let before = runtime.lock().chains[&SBT].last_end;
        let started = Instant::now();
        for index in 0..3 {
            runtime.serve_file(SBT, 32 * 1024 * 1024 + index * read.len() as u64, &mut read);
        }
        assert!(started.elapsed() < Duration::from_millis(100));
        assert_eq!(runtime.lock().chains[&SBT].last_end, before);
    }

    #[test]
    fn contiguous_16k_reads_remain_paced_after_eight_mib_without_source() {
        let runtime = runtime();
        let block = 16 * 1024u64;
        let offset = 8 * 1024 * 1024 - block;
        runtime.lock().chains.entry(SBT).or_default().last_end = Some(offset);
        let mut read = vec![0; block as usize];
        let started = Instant::now();
        for index in 0..4 {
            runtime.serve_file(SBT, offset + index * block, &mut read);
        }
        assert_eq!(
            runtime.lock().chains[&SBT].last_end,
            Some(offset + 4 * block)
        );
        assert!(started.elapsed() >= Duration::from_millis(45));
    }

    #[test]
    fn far_probes_and_16k_return_reads_preserve_k3_chain_isolation() {
        let runtime = runtime();
        let block = 16 * 1024u64;
        let previous_end = 4 * 1024 * 1024;
        runtime.lock().chains.entry(SBT).or_default().last_end = Some(previous_end);
        let mut read = vec![0; block as usize];
        let started = Instant::now();
        for index in 0..4 {
            runtime.serve_file(SBT, 400 * 1024 * 1024 + index * block, &mut read);
        }
        assert!(started.elapsed() < Duration::from_millis(100));
        assert_eq!(runtime.lock().chains[&SBT].last_end, Some(previous_end));
        assert!(runtime.pending_intent().is_none());
        let returned = runtime.bootstrap(SBT).unwrap().len() as u64;
        let returned = returned.div_ceil(block) * block;
        runtime.serve_file(SBT, returned, &mut read);
        runtime.serve_file(SBT, returned + block, &mut read);
        assert_eq!(runtime.lock().chains[&SBT].last_end, Some(previous_end));
        runtime.serve_file(SBT, returned + 2 * block, &mut read);
        assert_eq!(
            runtime.lock().chains[&SBT].last_end,
            Some(returned + 3 * block)
        );
        runtime.serve_file(SBT, returned + 3 * block, &mut read);
        assert_eq!(
            runtime.lock().chains[&SBT].last_end,
            Some(returned + 4 * block)
        );
    }
}
