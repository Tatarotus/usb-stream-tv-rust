//! Budget/gate tests (TASK-003 corrections R1).
//!
//! Covers the 32-bit uptime boundary, same-timestamp emission, the shared
//! global budget including the worker summary, a slow sink whose backlog drains
//! after release, and the unavailable-clock policy.

use std::io;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use super::gate::Gate;
use super::writer::UnavailableClock;
use super::*;

fn cfg(rate: u32, burst: u32) -> Config {
    Config {
        mode: Mode::Basic,
        rate_per_s: rate,
        burst,
    }
}

fn clock(ms: u64) -> Arc<dyn Clock> {
    Arc::new(ManualClock::new(ms))
}

fn ev(kind: EventKind) -> Event {
    Event::new(Component::Engine, kind)
}

/// ~49.7 days of milliseconds: the width the old packed timestamp truncated.
const TWO_POW_32: u64 = 1 << 32;

#[test]
fn gate_refills_only_with_elapsed_time_at_the_32_bit_boundary() {
    let gate = Gate::new(1, 1);
    let at = TWO_POW_32 + 1_000;
    assert!(gate.allow(Some(at)), "first event uses the initial burst");
    assert!(
        !gate.allow(Some(at)),
        "same millisecond must not refill after the 2^32 ms boundary"
    );
    assert!(gate.allow(Some(at + 1_000)), "one second later refills one");
    assert!(!gate.allow(Some(at + 1_000)), "and only one");
    assert!(gate.allow(Some(at + 2_000)));
}

#[test]
fn gate_is_bounded_far_past_the_boundary_and_after_a_wrap() {
    let gate = Gate::new(8, 16);
    let base = TWO_POW_32 * 8;
    for i in 0..16 {
        assert!(gate.allow(Some(base)), "burst event {i}");
    }
    assert!(
        !gate.allow(Some(base)),
        "burst exhausted at the same instant"
    );
    assert!(gate.allow(Some(base + 125)), "8/s gain after 125 ms");
    assert!(!gate.allow(Some(base + 125)));
    // A huge forward gap refills only up to the bucket capacity.
    for _ in 0..16 {
        assert!(gate.allow(Some(base + 10_000_000)));
    }
    assert!(!gate.allow(Some(base + 10_000_000)));
}

#[test]
fn gate_without_clock_drains_the_burst_and_never_refills() {
    let gate = Gate::new(8, 16);
    for _ in 0..16 {
        assert!(gate.allow(None), "burst drains without a clock");
    }
    for _ in 0..50 {
        assert!(!gate.allow(None), "unavailable clock refills nothing");
    }
}

#[test]
fn default_budget_caps_fixed_timestamp_emissions_including_summary() {
    let sink = Arc::new(MemorySink::new());
    let started =
        Observer::start(Config::default(), clock(0), Box::new(Arc::clone(&sink))).expect("spawns");
    for _ in 0..40 {
        started.observer.emit(ev(EventKind::ReanchorCommit));
    }
    let before = started.observer.stats();
    assert_eq!(before.emitted, 16, "burst of 16 only");
    assert_eq!(before.rate_suppressed, 24);
    let Started { observer, worker } = started;
    assert!(worker.shutdown(2_000));
    let stats = observer.stats();
    let lines = sink.lines();
    assert!(
        lines.len() <= 16,
        "global budget covers every emitted line, summaries included: {}",
        lines.len()
    );
    assert_eq!(stats.formatted as usize, lines.len());
}

#[test]
fn unavailable_clock_does_not_flood_summaries() {
    let sink = Arc::new(MemorySink::new());
    let started = Observer::start(
        cfg(8, 16),
        Arc::new(UnavailableClock) as Arc<dyn Clock>,
        Box::new(Arc::clone(&sink)),
    )
    .expect("spawns");
    for _ in 0..40 {
        started.observer.emit(ev(EventKind::ReanchorCommit));
    }
    let Started { observer, worker } = started;
    assert!(worker.shutdown(2_000));
    assert_eq!(observer.stats().emitted, 16);
    let lines = sink.lines();
    assert!(lines.len() <= 16, "no summary flood: {} lines", lines.len());
    assert!(lines.iter().all(|l| l.contains("clock=unavailable")));
    assert!(lines.iter().all(|l| l.contains("boot_ms=unknown")));
}

/// Sink that blocks inside `write_line` until released.
struct ReleaseSink {
    gate: Arc<(Mutex<bool>, Condvar)>,
    lines: Arc<AtomicUsize>,
}

impl Sink for ReleaseSink {
    fn write_line(&mut self, _line: &[u8]) -> io::Result<()> {
        let (lock, cv) = &*self.gate;
        let mut open = lock.lock().unwrap_or_else(|e| e.into_inner());
        while !*open {
            open = cv.wait(open).unwrap_or_else(|e| e.into_inner());
        }
        self.lines.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
}

#[test]
fn exhausted_budget_gates_the_summary_and_counts_it() {
    let gate = Arc::new((Mutex::new(false), Condvar::new()));
    let lines = Arc::new(AtomicUsize::new(0));
    let sink = ReleaseSink {
        gate: Arc::clone(&gate),
        lines: Arc::clone(&lines),
    };
    let started = Observer::start(cfg(1, 8), clock(0), Box::new(sink)).expect("spawns");
    for _ in 0..8 {
        started.observer.emit(ev(EventKind::ProcessStart));
    }
    // The worker is blocked in the sink, so these losses are recorded first.
    for _ in 0..12 {
        started.observer.emit(ev(EventKind::ProcessStop));
    }
    let before = started.observer.stats();
    assert_eq!((before.emitted, before.rate_suppressed), (8, 12));
    gate_release(&gate);
    let Started { observer, worker } = started;
    assert!(worker.shutdown(2_000));
    let stats = observer.stats();
    assert_eq!(
        stats.summary_gated, 1,
        "the summary shares the global budget and is counted, not retried"
    );
    assert_eq!(
        lines.load(Ordering::SeqCst),
        8,
        "only admitted events written"
    );
}

fn gate_release(gate: &Arc<(Mutex<bool>, Condvar)>) {
    let (lock, cv) = &**gate;
    *lock.lock().unwrap_or_else(|e| e.into_inner()) = true;
    cv.notify_all();
}

#[test]
fn queued_backlog_drains_after_a_slow_sink_is_released() {
    let gate = Arc::new((Mutex::new(false), Condvar::new()));
    let lines = Arc::new(AtomicUsize::new(0));
    let sink = ReleaseSink {
        gate: Arc::clone(&gate),
        lines: Arc::clone(&lines),
    };
    let started = Observer::start(cfg(100_000, 100_000), clock(0), Box::new(sink)).expect("spawns");
    let t0 = Instant::now();
    for _ in 0..(QUEUE_CAPACITY * 4) {
        started.observer.emit(ev(EventKind::FuseOpenRequest));
    }
    let elapsed = t0.elapsed();
    let stats = started.observer.stats();
    assert!(stats.queue_full >= 1, "the queue bound was reached");
    assert!(
        elapsed < Duration::from_secs(2),
        "producers never waited for the sink ({elapsed:?})"
    );
    gate_release(&gate);
    let Started { worker, .. } = started;
    assert!(worker.shutdown(2_000), "worker exits after release");
    assert!(
        lines.load(Ordering::SeqCst) >= QUEUE_CAPACITY,
        "the queued backlog was drained after release: {}",
        lines.load(Ordering::SeqCst)
    );
}

#[test]
fn summary_recovers_after_sink_failure() {
    struct CombinedSink {
        inner: Arc<MemorySink>,
        summary_sync: Arc<(Mutex<usize>, Condvar)>,
        fail_on: usize,
    }
    impl Sink for CombinedSink {
        fn write_line(&mut self, line: &[u8]) -> std::io::Result<()> {
            if line.windows(13).any(|w| w == b"drops_summary") {
                let (lock, cv) = &*self.summary_sync;
                let mut count = lock.lock().unwrap_or_else(|e| e.into_inner());
                *count += 1;
                let n = *count;
                cv.notify_all();
                if n == self.fail_on {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::BrokenPipe,
                        "summary failed",
                    ));
                }
            }
            self.inner.write_line(line)
        }
    }

    fn wait_summary(sync: &Arc<(Mutex<usize>, Condvar)>, target: usize) {
        let (lock, cv) = &**sync;
        let mut count = lock.lock().unwrap_or_else(|e| e.into_inner());
        while *count < target {
            let res = cv
                .wait_timeout(count, Duration::from_secs(2))
                .unwrap_or_else(|e| e.into_inner());
            assert!(
                !res.1.timed_out(),
                "timed out waiting for summary attempt {target}"
            );
            count = res.0;
        }
    }

    let inner = Arc::new(MemorySink::new());
    let summary_sync = Arc::new((Mutex::new(0), Condvar::new()));
    let sink = CombinedSink {
        inner: Arc::clone(&inner),
        summary_sync: Arc::clone(&summary_sync),
        fail_on: 1,
    };
    let clock = Arc::new(ManualClock::new(0));
    let started = Observer::start(cfg(8, 16), clock.clone(), Box::new(sink)).expect("spawns");

    // Batch 1: 40 events with clock at 0. Burst 16 admitted, 24 rate-suppressed.
    for _ in 0..40 {
        started.observer.emit(ev(EventKind::ReanchorCommit));
    }
    assert_eq!(
        started.observer.stats().rate_suppressed,
        24,
        "batch 1 suppresses exactly 24 events"
    );

    // Advance clock to 1000 so summary is due; worker attempts summary 1 which fails.
    clock.set(1000);
    wait_summary(&summary_sync, 1);

    // Advance clock to 2000; summary retries and succeeds with exact interval 24.
    clock.set(2000);
    started.observer.emit(ev(EventKind::ReanchorCommit));
    wait_summary(&summary_sync, 2);

    // Batch 2: advance clock to 3000, emit 32 events. Burst 16 admitted, 16 suppressed.
    clock.set(3000);
    for _ in 0..32 {
        started.observer.emit(ev(EventKind::ReanchorCommit));
    }
    assert_eq!(
        started.observer.stats().rate_suppressed,
        40,
        "batch 2 adds 16 suppressed events (40 total)"
    );

    // Advance clock to 4000 so global gate refills; summary 2 succeeds with interval 16.
    clock.set(4000);
    started.observer.emit(ev(EventKind::ReanchorCommit));
    wait_summary(&summary_sync, 3);

    let Started { observer, worker } = started;
    assert!(worker.shutdown(2_000));
    let stats = observer.stats();
    assert!(stats.sink_errors >= 1, "sink error counted: {:?}", stats);

    let lines = inner.lines();
    let summaries: Vec<&String> = lines
        .iter()
        .filter(|l| l.contains("event=drops_summary"))
        .collect();
    assert_eq!(summaries.len(), 2, "two summaries emitted after recovery");

    let parse_dropped = |s: &str| -> u64 {
        s.split("dropped=")
            .nth(1)
            .and_then(|r| r.split_whitespace().next())
            .and_then(|v| v.parse::<u64>().ok())
            .expect("valid numeric dropped count")
    };
    assert_eq!(
        parse_dropped(summaries[0]),
        24,
        "1st summary exact numeric interval 24"
    );
    assert_eq!(
        parse_dropped(summaries[1]),
        16,
        "2nd summary distinct numeric interval 16"
    );
}

#[test]
fn summary_emitted_with_positive_interval() {
    // Normal sink: verifies a summary is emitted with a positive interval
    // when losses occur.
    let sink = Arc::new(MemorySink::new());
    let clock = Arc::new(ManualClock::new(0));
    let started =
        Observer::start(cfg(8, 16), clock.clone(), Box::new(Arc::clone(&sink))).expect("spawns");
    for _ in 0..40 {
        started.observer.emit(ev(EventKind::ReanchorCommit));
    }
    std::thread::sleep(std::time::Duration::from_millis(100));
    clock.set(1000);
    for _ in 0..8 {
        started.observer.emit(ev(EventKind::ReanchorCommit));
    }
    std::thread::sleep(std::time::Duration::from_millis(100));
    let Started { observer, worker } = started;
    assert!(worker.shutdown(2_000));
    let lines = sink.lines();
    let summaries: Vec<&String> = lines
        .iter()
        .filter(|l| l.contains("event=drops_summary"))
        .collect();
    if !summaries.is_empty() {
        for s in &summaries {
            let v = s
                .split("dropped=")
                .nth(1)
                .and_then(|r| r.split_whitespace().next())
                .and_then(|v| v.parse::<u64>().ok())
                .unwrap_or(0);
            assert!(v > 0, "summary interval must be positive: {s}");
        }
    } else {
        assert!(
            observer.stats().summary_gated >= 1,
            "summary was gated: {:?}",
            observer.stats()
        );
    }
    let _ = observer;
}
