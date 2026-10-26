//! Bounded read-evidence detector for favorite selection.
//!
//! USB mass storage exposes sector reads, not an "open file" event. A small
//! number of metadata/thumbnail reads must therefore never change the active
//! channel. This detector requires several distinct, contiguous blocks of
//! *new* coverage in the opening region of one file before emitting a
//! selection intent. It never publishes a channel itself: the selection
//! controller owns epoch/commit.
//!
//! Evidence is a union of half-open intervals. Repeated or overlapping reads
//! add no coverage, and an original request split across extents is passed as
//! one [`ReadSpan`] list so it counts as one request, not several.
//!
//! All waits use the caller-supplied monotonic millisecond clock. No clock,
//! network, mutex or allocation proportional to catalog size is used.

use std::collections::BTreeMap;

pub const DEFAULT_BLOCK_BYTES: u64 = 256 * 1024;
pub const DEFAULT_MIN_BLOCKS: u32 = 3;
pub const DEFAULT_MIN_REQUESTS: u32 = 3;
pub const DEFAULT_MIN_COVERAGE: u64 = 1024 * 1024;
pub const DEFAULT_TOLERANCE_BYTES: u64 = 256 * 1024;
pub const DEFAULT_DEBOUNCE_MS: u64 = 2000;
pub const DEFAULT_TTL_MS: u64 = 15_000;
pub const DEFAULT_MAX_INTERVALS: usize = 32;
pub const DEFAULT_MAX_WINDOW_BYTES: u64 = 8 * 1024 * 1024;
pub const DEFAULT_MAX_RUNS: usize = 4;
pub const DEFAULT_MAX_CANDIDATES: usize = 32;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DetectorConfig {
    pub block_bytes: u64,
    pub min_blocks: u32,
    pub min_requests: u32,
    pub min_coverage: u64,
    pub tolerance_bytes: u64,
    pub debounce_ms: u64,
    pub ttl_ms: u64,
    pub max_intervals: usize,
    pub max_window_bytes: u64,
    pub max_runs: usize,
    pub max_candidates: usize,
}

impl Default for DetectorConfig {
    fn default() -> Self {
        Self {
            block_bytes: DEFAULT_BLOCK_BYTES,
            min_blocks: DEFAULT_MIN_BLOCKS,
            min_requests: DEFAULT_MIN_REQUESTS,
            min_coverage: DEFAULT_MIN_COVERAGE,
            tolerance_bytes: DEFAULT_TOLERANCE_BYTES,
            debounce_ms: DEFAULT_DEBOUNCE_MS,
            ttl_ms: DEFAULT_TTL_MS,
            max_intervals: DEFAULT_MAX_INTERVALS,
            max_window_bytes: DEFAULT_MAX_WINDOW_BYTES,
            max_runs: DEFAULT_MAX_RUNS,
            max_candidates: DEFAULT_MAX_CANDIDATES,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReadSpan {
    pub offset: u64,
    pub len: u64,
}

impl ReadSpan {
    pub fn new(offset: u64, len: u64) -> Self {
        Self { offset, len }
    }

    pub fn end(&self) -> Option<u64> {
        self.offset.checked_add(self.len)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SelectionIntent {
    pub file_id: u32,
    pub opening_start: u64,
    pub covered_bytes: u64,
    pub distinct_blocks: u32,
    pub useful_requests: u32,
    pub detected_at_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CandidateView {
    pub file_id: u32,
    pub covered_bytes: u64,
    pub distinct_blocks: u32,
    pub useful_requests: u32,
    pub runs: usize,
}

#[derive(Debug, Clone, Default)]
struct EvidenceRun {
    opening_start: u64,
    max_end: u64,
    intervals: Vec<(u64, u64)>,
    covered: u64,
    requests: u32,
    last_useful_ms: u64,
}

impl EvidenceRun {
    fn new(opening_start: u64, now_ms: u64) -> Self {
        Self {
            opening_start,
            max_end: opening_start,
            last_useful_ms: now_ms,
            ..Self::default()
        }
    }

    fn is_empty(&self) -> bool {
        self.intervals.is_empty()
    }

    fn accepts(&self, first_start: u64, last_end: u64, cfg: &DetectorConfig) -> bool {
        if self.is_empty() {
            return false;
        }
        let within_forward = first_start <= self.max_end.saturating_add(cfg.tolerance_bytes);
        let overlaps_forward = last_end > self.opening_start;
        let within_window = first_start < self.opening_start.saturating_add(cfg.max_window_bytes);
        within_forward && overlaps_forward && within_window
    }

    /// Adds one clipped span and returns the number of newly covered bytes.
    fn add_span(&mut self, span: ReadSpan, cfg: &DetectorConfig) -> Result<u64, ()> {
        let Some(end) = span.end() else {
            return Ok(0);
        };
        let window_end = self.opening_start.saturating_add(cfg.max_window_bytes);
        let start = span.offset.max(self.opening_start);
        let end = end.min(window_end);
        if end <= start {
            return Ok(0);
        }
        let before = self.covered;
        self.intervals.push((start, end));
        self.intervals.sort_unstable();
        let mut merged: Vec<(u64, u64)> = Vec::with_capacity(self.intervals.len());
        for (s, e) in self.intervals.drain(..) {
            match merged.last_mut() {
                Some(last) if s <= last.1 => {
                    if e > last.1 {
                        last.1 = e;
                    }
                }
                _ => merged.push((s, e)),
            }
        }
        if merged.len() > cfg.max_intervals {
            return Err(());
        }
        self.intervals = merged;
        self.covered = self
            .intervals
            .iter()
            .fold(0u64, |sum, (s, e)| sum.saturating_add(e - s));
        self.max_end = self.max_end.max(end);
        Ok(self.covered - before)
    }

    fn distinct_blocks(&self, block_bytes: u64) -> u32 {
        if block_bytes == 0 {
            return 0;
        }
        // Interval-count bounded: merge block-index ranges instead of
        // enumerating every block, so a hostile read cannot allocate
        // proportional to the requested byte length.
        let mut ranges: Vec<(u64, u64)> = Vec::with_capacity(self.intervals.len());
        for &(start, end) in &self.intervals {
            if end <= start {
                continue;
            }
            let first = start / block_bytes;
            let last = (end - 1) / block_bytes + 1;
            ranges.push((first, last));
        }
        ranges.sort_unstable();
        let mut total = 0u64;
        let mut current: Option<(u64, u64)> = None;
        for (start, end) in ranges {
            match current {
                Some((cs, ce)) if start <= ce => current = Some((cs, ce.max(end))),
                Some((cs, ce)) => {
                    total = total.saturating_add(ce - cs);
                    current = Some((start, end));
                }
                None => current = Some((start, end)),
            }
        }
        if let Some((cs, ce)) = current {
            total = total.saturating_add(ce - cs);
        }
        total.min(u32::MAX as u64) as u32
    }

    fn is_ready(&self, cfg: &DetectorConfig) -> bool {
        self.requests >= cfg.min_requests
            && self.covered >= cfg.min_coverage
            && self.distinct_blocks(cfg.block_bytes) >= cfg.min_blocks
    }

    fn strength(&self) -> (u64, u32, u64) {
        (self.covered, self.requests, self.max_end)
    }
}

#[derive(Debug, Clone)]
struct Candidate {
    runs: Vec<EvidenceRun>,
}

impl Candidate {
    fn new() -> Self {
        Self { runs: Vec::new() }
    }

    fn select_run(
        &mut self,
        first_start: u64,
        last_end: u64,
        cfg: &DetectorConfig,
        now_ms: u64,
    ) -> usize {
        if let Some(index) = self
            .runs
            .iter()
            .position(|run| run.accepts(first_start, last_end, cfg))
        {
            return index;
        }
        self.runs.push(EvidenceRun::new(first_start, now_ms));
        self.runs.len() - 1
    }

    fn prune_runs(&mut self, cfg: &DetectorConfig) {
        while self.runs.len() > cfg.max_runs.max(1) {
            let weakest = self
                .runs
                .iter()
                .enumerate()
                .min_by_key(|(_, run)| run.strength())
                .map(|(i, _)| i)
                .unwrap_or(0);
            self.runs.remove(weakest);
        }
    }

    fn observe(&mut self, spans: &[ReadSpan], cfg: &DetectorConfig, now_ms: u64) {
        let Some(first) = spans.first() else {
            return;
        };
        let first_start = spans.iter().map(|s| s.offset).min().unwrap_or(first.offset);
        let last_end = spans
            .iter()
            .filter_map(|s| s.end())
            .max()
            .unwrap_or(first.offset);
        let index = self.select_run(first_start, last_end, cfg, now_ms);
        let mut new_bytes = 0u64;
        for span in spans {
            match self.runs[index].add_span(*span, cfg) {
                Ok(bytes) => new_bytes = new_bytes.saturating_add(bytes),
                Err(()) => {
                    self.runs.remove(index);
                    return;
                }
            }
        }
        if new_bytes > 0 {
            self.runs[index].requests = self.runs[index].requests.saturating_add(1);
            self.runs[index].last_useful_ms = now_ms;
        }
        self.prune_runs(cfg);
    }

    fn best(&self) -> Option<&EvidenceRun> {
        self.runs.iter().max_by_key(|run| run.strength())
    }
}

#[derive(Debug)]
pub struct ProbeDetector {
    cfg: DetectorConfig,
    active: Option<u32>,
    candidates: BTreeMap<u32, Candidate>,
    last_commit_ms: Option<u64>,
    dropped_candidates: u64,
}

impl Default for ProbeDetector {
    fn default() -> Self {
        Self::new(DetectorConfig::default())
    }
}

impl ProbeDetector {
    pub fn new(mut cfg: DetectorConfig) -> Self {
        cfg.max_candidates = cfg.max_candidates.min(DEFAULT_MAX_CANDIDATES);
        Self {
            cfg,
            active: None,
            candidates: BTreeMap::new(),
            last_commit_ms: None,
            dropped_candidates: 0,
        }
    }

    pub fn config(&self) -> &DetectorConfig {
        &self.cfg
    }

    pub fn active(&self) -> Option<u32> {
        self.active
    }

    pub fn set_active(&mut self, file_id: Option<u32>) {
        self.active = file_id;
        if let Some(id) = file_id {
            self.candidates.remove(&id);
        }
    }

    pub fn candidate_count(&self) -> usize {
        self.candidates.len()
    }

    pub fn dropped_candidates(&self) -> u64 {
        self.dropped_candidates
    }

    pub fn candidate(&self, file_id: u32) -> Option<CandidateView> {
        let candidate = self.candidates.get(&file_id)?;
        let best = candidate.best()?;
        Some(CandidateView {
            file_id,
            covered_bytes: best.covered,
            distinct_blocks: best.distinct_blocks(self.cfg.block_bytes),
            useful_requests: best.requests,
            runs: candidate.runs.len(),
        })
    }

    fn expire(&mut self, now_ms: u64) {
        let ttl = self.cfg.ttl_ms;
        let before = self.candidates.len();
        self.candidates.retain(|_, candidate| {
            candidate
                .runs
                .retain(|run| now_ms.saturating_sub(run.last_useful_ms) <= ttl);
            !candidate.runs.is_empty()
        });
        self.dropped_candidates = self
            .dropped_candidates
            .saturating_add((before - self.candidates.len()) as u64);
    }

    /// Records one contiguous read.
    pub fn observe(
        &mut self,
        file_id: u32,
        offset: u64,
        len: u64,
        now_ms: u64,
    ) -> Option<SelectionIntent> {
        self.observe_request(file_id, &[ReadSpan::new(offset, len)], now_ms)
    }

    /// Records one original request. `spans` are the fragments of that request
    /// after routing; they count as a single request even when the read
    /// crossed several extents.
    pub fn observe_request(
        &mut self,
        file_id: u32,
        spans: &[ReadSpan],
        now_ms: u64,
    ) -> Option<SelectionIntent> {
        self.expire(now_ms);
        if spans.len() > self.cfg.max_intervals.min(DEFAULT_MAX_INTERVALS) {
            return None;
        }
        let spans: Vec<ReadSpan> = spans
            .iter()
            .copied()
            // Eligibility is an absolute file-offset window, not a window
            // anchored wherever a probe happened to begin. In particular,
            // sequential scans near EOF must not select another channel.
            .filter_map(|s| {
                let end = s.end()?.min(self.cfg.max_window_bytes);
                if s.offset < end {
                    Some(ReadSpan::new(s.offset, end - s.offset))
                } else {
                    None
                }
            })
            .collect();
        if spans.is_empty() {
            return None;
        }
        // Reads of the published channel are served normally and never
        // confirm a return by themselves.
        if self.active == Some(file_id) {
            return None;
        }

        if !self.candidates.contains_key(&file_id)
            && self.candidates.len() >= self.cfg.max_candidates
        {
            self.dropped_candidates = self.dropped_candidates.saturating_add(1);
            return None;
        }
        let candidate = self
            .candidates
            .entry(file_id)
            .or_insert_with(Candidate::new);
        candidate.observe(&spans, &self.cfg, now_ms);

        if candidate.runs.is_empty() {
            self.candidates.remove(&file_id);
            self.dropped_candidates = self.dropped_candidates.saturating_add(1);
            return None;
        }

        let ready = candidate
            .best()
            .map(|run| run.is_ready(&self.cfg))
            .unwrap_or(false);
        if !ready {
            return None;
        }
        if let Some(last) = self.last_commit_ms {
            if now_ms < last.saturating_add(self.cfg.debounce_ms) {
                return None;
            }
        }

        let run = candidate.best()?.clone();
        let intent = SelectionIntent {
            file_id,
            opening_start: run.opening_start,
            covered_bytes: run.covered,
            distinct_blocks: run.distinct_blocks(self.cfg.block_bytes),
            useful_requests: run.requests,
            detected_at_ms: now_ms,
        };
        self.candidates.remove(&file_id);
        Some(intent)
    }

    /// Called by the selection controller after a successful commit.
    pub fn on_committed(&mut self, file_id: u32, now_ms: u64) {
        self.last_commit_ms = Some(now_ms);
        self.set_active(Some(file_id));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const K: u64 = 256 * 1024;
    const MI: u64 = 1024 * 1024;

    fn detector() -> ProbeDetector {
        let mut d = ProbeDetector::default();
        d.set_active(Some(0));
        d
    }

    #[test]
    fn one_or_two_probes_never_confirm() {
        let mut d = detector();
        assert!(d.observe(1, 0, K, 0).is_none());
        assert!(d.observe(1, K, K, 100).is_none());
        assert_eq!(d.candidate_count(), 1);
        assert_eq!(d.candidate(1).unwrap().useful_requests, 2);
    }

    #[test]
    fn four_distinct_blocks_confirm_after_the_request_that_completes_them() {
        let mut d = detector();
        assert!(d.observe(1, 0, K, 0).is_none());
        assert!(d.observe(1, K, K, 100).is_none());
        assert!(d.observe(1, 2 * K, K, 200).is_none());
        let intent = d.observe(1, 3 * K, K, 300).expect("intent");
        assert_eq!(intent.file_id, 1);
        assert_eq!(intent.opening_start, 0);
        assert_eq!(intent.covered_bytes, MI);
        assert_eq!(intent.distinct_blocks, 4);
        assert_eq!(intent.useful_requests, 4);
        assert_eq!(d.candidate_count(), 0);
    }

    #[test]
    fn repeated_overlapping_and_giant_reads_do_not_confirm() {
        let mut d = detector();
        for _ in 0..5 {
            assert!(d.observe(1, 0, MI, 0).is_none());
        }
        assert_eq!(d.candidate(1).unwrap().useful_requests, 1);
        // A giant isolated first read is still one request.
        let mut d = detector();
        assert!(d.observe(1, 0, 8 * MI, 0).is_none());
        assert_eq!(d.candidate(1).unwrap().useful_requests, 1);
        assert_eq!(d.candidate(1).unwrap().covered_bytes, 8 * MI);
    }

    #[test]
    fn fragments_of_one_request_count_once() {
        let mut d = detector();
        let spans = [
            ReadSpan::new(0, K),
            ReadSpan::new(K, K),
            ReadSpan::new(2 * K, K),
            ReadSpan::new(3 * K, K),
        ];
        assert!(d.observe_request(1, &spans, 0).is_none());
        assert_eq!(d.candidate(1).unwrap().useful_requests, 1);
        assert!(d.observe(1, 4 * K, K, 100).is_none());
        let intent = d.observe(1, 5 * K, K, 200).expect("intent");
        assert_eq!(intent.useful_requests, 3);
    }

    #[test]
    fn isolated_probe_in_another_file_does_not_erase_a_candidate() {
        let mut d = detector();
        assert!(d.observe(1, 0, K, 0).is_none());
        assert!(d.observe(1, K, K, 100).is_none());
        assert!(d.observe(1, 2 * K, K, 200).is_none());
        assert!(d.observe(2, 0, 128 * 1024, 250).is_none());
        let intent = d.observe(1, 3 * K, K, 300).expect("candidate survives");
        assert_eq!(intent.file_id, 1);
    }

    #[test]
    fn tail_jump_does_not_add_to_or_erase_the_opening_run() {
        let mut d = detector();
        assert!(d.observe(1, 0, K, 0).is_none());
        assert!(d.observe(1, K, K, 100).is_none());
        assert!(d.observe(1, 2 * K, K, 200).is_none());
        assert!(d.observe(1, 8 * MI, K, 250).is_none());
        assert_eq!(d.candidate(1).unwrap().runs, 1);
        let intent = d.observe(1, 3 * K, K, 300).expect("opening run survives");
        assert_eq!(intent.opening_start, 0);
        assert_eq!(intent.covered_bytes, MI);
    }

    #[test]
    fn reads_of_the_active_channel_do_not_confirm() {
        let mut d = detector();
        for i in 0..8 {
            assert!(d.observe(0, i * K, K, i * 100).is_none());
        }
        assert_eq!(d.candidate_count(), 0);
        assert_eq!(d.active(), Some(0));
    }

    #[test]
    fn debounce_delays_a_second_commit() {
        let mut d = ProbeDetector::new(DetectorConfig {
            ttl_ms: 60_000,
            ..DetectorConfig::default()
        });
        d.set_active(Some(0));
        assert!(d.observe(1, 0, K, 0).is_none());
        assert!(d.observe(1, K, K, 100).is_none());
        assert!(d.observe(1, 2 * K, K, 200).is_none());
        assert!(d.observe(1, 3 * K, K, 300).is_some());
        d.on_committed(1, 300);

        assert!(d.observe(2, 0, K, 500).is_none());
        assert!(d.observe(2, K, K, 600).is_none());
        assert!(d.observe(2, 2 * K, K, 700).is_none());
        // Ready but inside the 2 s debounce window.
        assert!(d.observe(2, 3 * K, K, 800).is_none());
        assert!(d.observe(2, 4 * K, K, 2301).is_some());
    }

    #[test]
    fn candidates_expire_without_unbounded_growth() {
        let mut d = ProbeDetector::new(DetectorConfig {
            ttl_ms: 1000,
            ..DetectorConfig::default()
        });
        d.set_active(Some(0));
        assert!(d.observe(1, 0, K, 0).is_none());
        assert!(d.observe(2, 0, K, 100).is_none());
        assert_eq!(d.candidate_count(), 2);
        assert!(d.observe(3, 0, 1, 5000).is_none());
        assert_eq!(d.candidate_count(), 1);
        assert!(d.candidate(1).is_none());
        assert!(d.dropped_candidates() >= 2);
    }

    #[test]
    fn first_channel_also_needs_confirmation() {
        let mut d = ProbeDetector::default();
        assert_eq!(d.active(), None);
        assert!(d.observe(1, 0, K, 0).is_none());
        assert!(d.observe(1, K, K, 100).is_none());
        assert!(d.observe(1, 2 * K, K, 200).is_none());
        assert!(d.observe(1, 3 * K, K, 300).is_some());
    }

    #[test]
    fn interval_capacity_drops_the_overgrown_run_without_panicking() {
        let mut d = ProbeDetector::new(DetectorConfig {
            max_intervals: 2,
            tolerance_bytes: 8 * MI,
            ..DetectorConfig::default()
        });
        d.set_active(Some(0));
        assert!(d.observe(1, 0, 1, 0).is_none());
        assert!(d.observe(1, 2 * MI, 1, 100).is_none());
        // The third disjoint interval exceeds this run's fixed capacity.
        assert!(d.observe(1, 4 * MI, 1, 200).is_none());
        assert!(d.candidate(1).is_none());
        assert_eq!(d.dropped_candidates(), 1);
    }

    #[test]
    fn candidate_count_is_hard_bounded_even_before_ttl_expiry() {
        let mut d = ProbeDetector::new(DetectorConfig {
            max_candidates: usize::MAX,
            ..DetectorConfig::default()
        });
        for id in 0..1000 {
            assert!(d.observe(id, 0, 1, 0).is_none());
        }
        assert_eq!(d.candidate_count(), DEFAULT_MAX_CANDIDATES);
        assert_eq!(d.dropped_candidates(), 1000 - DEFAULT_MAX_CANDIDATES as u64);
    }

    #[test]
    fn sequential_tail_scan_never_enters_opening_evidence() {
        let mut d = ProbeDetector::default();
        for i in 0..4 {
            assert!(d
                .observe(1, 4_000_000_000 - MI + i * K, K, i * 100)
                .is_none());
        }
        assert_eq!(d.candidate_count(), 0);
        assert!(d.observe(1, 8 * MI - K, 4 * K, 500).is_none());
        assert_eq!(d.candidate(1).unwrap().covered_bytes, K);
    }

    #[test]
    fn duplicate_probes_do_not_renew_useful_evidence_ttl() {
        let mut d = ProbeDetector::new(DetectorConfig {
            ttl_ms: 1000,
            ..DetectorConfig::default()
        });
        d.observe(1, 0, K, 0);
        d.observe(1, 0, K, 500);
        d.observe(1, 0, K, 900);
        assert!(d.observe(1, K, K, 1001).is_none());
        let candidate = d.candidate(1).unwrap();
        assert_eq!(candidate.covered_bytes, K);
        assert_eq!(candidate.useful_requests, 1);
    }

    #[test]
    fn new_run_does_not_keep_an_old_run_alive() {
        let mut d = ProbeDetector::new(DetectorConfig {
            ttl_ms: 1000,
            ..DetectorConfig::default()
        });
        d.observe(1, 0, K, 0);
        d.observe(1, 4 * MI, K, 500);
        assert!(d.observe(1, 4 * MI + K, K, 1001).is_none());
        assert_eq!(d.candidate(1).unwrap().runs, 1);
        assert_eq!(d.candidate(1).unwrap().covered_bytes, 2 * K);
    }

    #[test]
    fn debounce_starts_at_actual_commit_after_slow_preparation() {
        let mut d = ProbeDetector::default();
        for i in 0..3 {
            assert!(d.observe(1, i * K, K, i * 100).is_none());
        }
        assert!(d.observe(1, 3 * K, K, 300).is_some());
        d.on_committed(1, 10_000);
        for i in 0..4 {
            assert!(d.observe(2, i * K, K, 10_100 + i * 100).is_none());
        }
        assert!(d.observe(2, 4 * K, K, 11_999).is_none());
        assert!(d.observe(2, 5 * K, K, 12_000).is_some());
    }
}
