//! Clock, counters and the single output worker.
//!
//! Nothing in this module runs on a producer thread: producers only stamp an
//! [`Event`] and enqueue it with `try_send`. The formatter lives in
//! [`super::format`] and the sinks in [`super::sinks`].

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::Duration;

use super::gate::Gate;
use super::{ClockState, Component, Event, EventKind, Reason, MAX_LINE_BYTES};

pub use super::format::format_line;
pub use super::sinks::{MemorySink, Sink, StderrSink};

/// Monotonic, suspend-aware millisecond clock used to stamp observations.
pub trait Clock: Send + Sync {
    /// Milliseconds since boot, or `None` when the domain is unavailable.
    fn boot_ms(&self) -> Option<u64>;
}

/// Linux `CLOCK_BOOTTIME` (suspend-aware, monotonic since boot).
pub struct BootClock;

impl Clock for BootClock {
    fn boot_ms(&self) -> Option<u64> {
        // Tiny libc boundary: zero-initialized timespec, checked return,
        // range-checked fields, overflow-safe conversion (ARM32 `time_t`).
        let mut ts: libc::timespec = unsafe { std::mem::zeroed() };
        let rc = unsafe { libc::clock_gettime(libc::CLOCK_BOOTTIME, &mut ts) };
        if rc != 0 {
            return None;
        }
        let sec = u64::try_from(ts.tv_sec).ok()?;
        let nsec = u64::try_from(ts.tv_nsec).ok()?;
        if nsec >= 1_000_000_000 {
            return None;
        }
        sec.checked_mul(1000)?.checked_add(nsec / 1_000_000)
    }
}

/// Clock that never answers; used by disabled observers and clock tests.
pub struct UnavailableClock;

impl Clock for UnavailableClock {
    fn boot_ms(&self) -> Option<u64> {
        None
    }
}

/// Deterministic clock for tests and harnesses.
pub struct ManualClock {
    now: AtomicU64,
}

impl ManualClock {
    pub fn new(ms: u64) -> Self {
        Self {
            now: AtomicU64::new(ms),
        }
    }

    pub fn set(&self, ms: u64) {
        self.now.store(ms, Ordering::SeqCst);
    }

    pub fn advance(&self, ms: u64) {
        self.now.fetch_add(ms, Ordering::SeqCst);
    }
}

impl Clock for ManualClock {
    fn boot_ms(&self) -> Option<u64> {
        Some(self.now.load(Ordering::SeqCst))
    }
}

/// `(pid, start_boot_time)` instance identity for one process.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Instance {
    pub pid: u32,
    pub start_ms: Option<u64>,
}

impl Instance {
    pub fn new(start_ms: Option<u64>) -> Self {
        Self {
            pid: std::process::id(),
            start_ms,
        }
    }

    pub fn unknown() -> Self {
        Self::new(None)
    }
}

// ------------------------------------------------------------------- counters

/// Producer/consumer loss accounting for the bounded channel.
///
/// The counters live with the output side because the worker owns half of them
/// (`formatted`, `sink_errors`, `format_loss`, `summary_gated`) and shares the
/// rest with the producer handle.
#[derive(Debug, Default)]
pub struct Counters {
    emitted: AtomicU64,
    rate_suppressed: AtomicU64,
    queue_full: AtomicU64,
    disabled: AtomicU64,
    sink_errors: AtomicU64,
    formatted: AtomicU64,
    format_loss: AtomicU64,
    summary_gated: AtomicU64,
}

/// Coherent-enough snapshot of the counters (reads are independent atomics).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Stats {
    pub emitted: u64,
    pub rate_suppressed: u64,
    pub queue_full: u64,
    pub disabled: u64,
    pub sink_errors: u64,
    pub formatted: u64,
    /// Records dropped whole because they did not fit the line bound.
    pub format_loss: u64,
    /// Worker summaries refused by the shared global budget.
    pub summary_gated: u64,
}

impl Stats {
    /// Total loss a producer could observe: rate gate + full queue.
    pub fn dropped(&self) -> u64 {
        self.rate_suppressed.saturating_add(self.queue_full)
    }
}

impl Counters {
    pub fn snapshot(&self) -> Stats {
        Stats {
            emitted: self.emitted.load(Ordering::Relaxed),
            rate_suppressed: self.rate_suppressed.load(Ordering::Relaxed),
            queue_full: self.queue_full.load(Ordering::Relaxed),
            disabled: self.disabled.load(Ordering::Relaxed),
            sink_errors: self.sink_errors.load(Ordering::Relaxed),
            formatted: self.formatted.load(Ordering::Relaxed),
            format_loss: self.format_loss.load(Ordering::Relaxed),
            summary_gated: self.summary_gated.load(Ordering::Relaxed),
        }
    }

    pub(crate) fn note_emitted(&self) {
        self.emitted.fetch_add(1, Ordering::Relaxed);
    }

    pub(crate) fn note_suppressed(&self) {
        self.rate_suppressed.fetch_add(1, Ordering::Relaxed);
    }

    pub(crate) fn note_queue_full(&self) {
        self.queue_full.fetch_add(1, Ordering::Relaxed);
    }

    pub(crate) fn note_disabled(&self) {
        self.disabled.fetch_add(1, Ordering::Relaxed);
    }

    pub(crate) fn note_sink_error(&self) {
        self.sink_errors.fetch_add(1, Ordering::Relaxed);
    }

    pub(crate) fn note_formatted(&self) {
        self.formatted.fetch_add(1, Ordering::Relaxed);
    }

    pub(crate) fn note_format_loss(&self) {
        self.format_loss.fetch_add(1, Ordering::Relaxed);
    }

    pub(crate) fn note_summary_gated(&self) {
        self.summary_gated.fetch_add(1, Ordering::Relaxed);
    }
}

// --------------------------------------------------------------------- worker

/// Handle of the single formatting/writer thread.
pub struct Worker {
    done: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
}

impl Worker {
    /// Worker used when observation is off: no thread, no sink.
    pub fn idle() -> Self {
        Self {
            done: Arc::new(AtomicBool::new(true)),
            stop: Arc::new(AtomicBool::new(true)),
            handle: None,
        }
    }

    /// Spawns the single writer. `global` is the same budget the producers use,
    /// so drop summaries are gated by the one effective global rate.
    pub(crate) fn spawn(
        rx: Receiver<Event>,
        mut sink: Box<dyn Sink>,
        clock: Arc<dyn Clock>,
        stats: Arc<Counters>,
        inst: Instance,
        global: Arc<Gate>,
    ) -> Result<Self, super::InitError> {
        let done = Arc::new(AtomicBool::new(false));
        let stop = Arc::new(AtomicBool::new(false));
        let done_t = Arc::clone(&done);
        let stop_t = Arc::clone(&stop);
        let handle = thread::Builder::new()
            .name("obs-writer".into())
            .stack_size(256 * 1024)
            .spawn(move || {
                let mut buf = [0u8; MAX_LINE_BYTES];
                let mut next_summary_ms = 0u64;
                let mut reported = 0u64;
                loop {
                    match rx.recv_timeout(Duration::from_millis(100)) {
                        Ok(ev) => {
                            let now = clock.boot_ms();
                            let dropped = stats.snapshot().dropped();
                            if dropped > reported {
                                let interval = dropped.saturating_sub(reported);
                                let due = match now {
                                    Some(ms) => ms >= next_summary_ms,
                                    None => true,
                                };
                                if due && global.allow(now) {
                                    if write_event(
                                        &mut sink,
                                        &summary_event(interval, now),
                                        &inst,
                                        &mut buf,
                                        &stats,
                                    ) {
                                        reported = dropped;
                                    }
                                    next_summary_ms = now.unwrap_or(0).saturating_add(1000);
                                } else if due {
                                    stats.note_summary_gated();
                                    next_summary_ms = now.unwrap_or(0).saturating_add(1000);
                                }
                            }
                            write_event(&mut sink, &ev, &inst, &mut buf, &stats);
                        }
                        Err(RecvTimeoutError::Timeout) => {
                            // Bounded idle retry for a pending drop summary.
                            let now = clock.boot_ms();
                            let dropped = stats.snapshot().dropped();
                            if dropped > reported {
                                let due = match now {
                                    Some(ms) => ms >= next_summary_ms,
                                    None => true,
                                };
                                if due && global.allow(now) {
                                    let interval = dropped.saturating_sub(reported);
                                    if write_event(
                                        &mut sink,
                                        &summary_event(interval, now),
                                        &inst,
                                        &mut buf,
                                        &stats,
                                    ) {
                                        reported = dropped;
                                    }
                                    next_summary_ms = now.unwrap_or(0).saturating_add(1000);
                                }
                            }
                            if stop_t.load(Ordering::Relaxed) {
                                break;
                            }
                        }
                        Err(RecvTimeoutError::Disconnected) => break,
                    }
                }
                done_t.store(true, Ordering::SeqCst);
            })
            .map_err(super::InitError::Spawn)?;
        Ok(Self {
            done,
            stop,
            handle: Some(handle),
        })
    }

    pub fn is_done(&self) -> bool {
        self.done.load(Ordering::SeqCst)
    }

    /// Best-effort shutdown: drains what is already queued, waits at most
    /// `timeout_ms` for the worker, and never joins a worker stuck on a sink.
    pub fn shutdown(mut self, timeout_ms: u64) -> bool {
        self.stop.store(true, Ordering::SeqCst);
        let mut waited = 0u64;
        while !self.is_done() && waited < timeout_ms {
            thread::sleep(Duration::from_millis(1));
            waited += 1;
        }
        if self.is_done() {
            if let Some(h) = self.handle.take() {
                let _ = h.join();
            }
            true
        } else {
            // Detach: the worker may still be blocked inside the sink.
            self.handle.take();
            false
        }
    }
}

/// Worker-generated drop summary. `event_seq=0` is the documented sentinel for
/// a record that was not allocated by a producer.
fn summary_event(interval: u64, now: Option<u64>) -> Event {
    let mut ev = Event::new(Component::Process, EventKind::DropsSummary)
        .reason(Reason::Sampled)
        .dropped(interval);
    ev.event_seq = 0;
    if let Some(ms) = now {
        ev.clock = ClockState::Boottime;
        ev.boot_ms = ms;
    }
    ev
}

fn write_event(
    sink: &mut Box<dyn Sink>,
    ev: &Event,
    inst: &Instance,
    buf: &mut [u8; MAX_LINE_BYTES],
    stats: &Counters,
) -> bool {
    let n = format_line(ev, inst, buf);
    if n == 0 {
        stats.note_format_loss();
        return false;
    }
    match sink.write_line(&buf[..n]) {
        Ok(()) => {
            stats.note_formatted();
            true
        }
        Err(_) => {
            stats.note_sink_error();
            false
        }
    }
}
