//! Unit tests for the bounded observation channel (fake clock and sinks).

use std::io;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use super::writer::{format_line, Instance, MemorySink, Sink};
use super::*;

fn manual(ms: u64) -> Arc<ManualClock> {
    Arc::new(ManualClock::new(ms))
}

fn cfg(rate: u32, burst: u32) -> Config {
    Config {
        mode: Mode::Basic,
        rate_per_s: rate,
        burst,
    }
}

fn start(cfg: Config, clock: Arc<ManualClock>) -> (Observer, Worker, Arc<MemorySink>) {
    let sink = Arc::new(MemorySink::new());
    let started = Observer::start(cfg, clock, Box::new(Arc::clone(&sink))).expect("spawns");
    (started.observer, started.worker, sink)
}

fn ev(kind: EventKind) -> Event {
    Event::new(Component::Engine, kind)
}

#[test]
fn event_payload_is_bounded_and_copy() {
    assert!(std::mem::size_of::<Event>() <= MAX_EVENT_BYTES);
    let a = ev(EventKind::LiveReadProgress);
    let b = a;
    assert_eq!(a, b);
}

#[test]
fn formatted_line_is_exact_and_bounded() {
    let inst = Instance {
        pid: 42,
        start_ms: Some(7_000),
    };
    let mut e = ev(EventKind::LiveReadProgress)
        .reason(Reason::Sampled)
        .file_id(0)
        .foff(1_234)
        .len(18_800)
        .real_bytes(1_000)
        .null_bytes(500);
    e.event_seq = 9;
    e.boot_ms = 12_345;
    e.clock = ClockState::Boottime;
    let mut buf = [0u8; MAX_LINE_BYTES];
    let n = format_line(&e, &inst, &mut buf);
    let line = std::str::from_utf8(&buf[..n]).unwrap();
    assert_eq!(
        line,
        "USBTV_OBS version=1 component=engine event=live_read_progress pid=42 \
instance=42.7000 event_seq=9 clock=boottime boot_ms=12345 precision_ms=1 \
reason=sampled file_id=0 foff=1234 len=18800 real_bytes=1000 null_bytes=500\n"
    );
    assert!(n <= MAX_LINE_BYTES);
    assert_eq!(line.matches("USBTV_OBS").count(), 1);
}

#[test]
fn over_bound_record_returns_zero_instead_of_a_partial_line() {
    let inst = Instance {
        pid: u32::MAX,
        start_ms: Some(u64::MAX - 1),
    };
    let mut e = ev(EventKind::ReanchorCommit)
        .reason(Reason::ChannelSwitch)
        .req_unique(u64::MAX - 2)
        .nodeid(u64::MAX - 2)
        .fh(u64::MAX - 2)
        .anchor_epoch(u64::MAX - 2)
        .file_id(u64::MAX - 2)
        .foff(u64::MAX - 2)
        .len(u64::MAX - 2)
        .mapped_spos(u64::MAX - 2)
        .s_write(u64::MAX - 2)
        .ring_old(u64::MAX - 2)
        .real_bytes(u64::MAX - 2)
        .null_bytes(u64::MAX - 2)
        .wait_ms(u64::MAX - 2)
        .dropped(u64::MAX - 2)
        .errno(-2_147_483_648);
    e.event_seq = u64::MAX - 2;
    e.boot_ms = u64::MAX - 2;
    e.clock = ClockState::Boottime;
    let mut buf = [0u8; MAX_LINE_BYTES];
    // Whole-record contract: no truncation, no silent trailing-field loss.
    assert_eq!(format_line(&e, &inst, &mut buf), 0);
}

#[test]
fn unknown_clock_is_reported_not_zero() {
    let inst = Instance::unknown();
    let mut e = ev(EventKind::FifoEof);
    e.clock = ClockState::Unavailable;
    let mut buf = [0u8; MAX_LINE_BYTES];
    let n = format_line(&e, &inst, &mut buf);
    let line = std::str::from_utf8(&buf[..n]).unwrap();
    assert!(line.contains("clock=unavailable"));
    assert!(line.contains("boot_ms=unknown"));
    assert!(!line.contains("boot_ms=0 "));
    assert!(line.contains("instance=") && line.contains(".unknown"));
}

#[test]
fn disabled_observer_has_no_worker_or_output() {
    let obs = Observer::disabled();
    assert!(!obs.is_enabled());
    for _ in 0..10 {
        obs.emit(ev(EventKind::FifoEof));
    }
    let stats = obs.stats();
    assert_eq!(stats.disabled, 10);
    assert_eq!(stats.emitted, 0);
    assert_eq!(stats.formatted, 0);
}

#[test]
fn off_mode_starts_disabled() {
    let mut c = cfg(8, 16);
    c.mode = Mode::Off;
    let started = Observer::start(c, manual(1), Box::new(MemorySink::new())).unwrap();
    assert!(!started.observer.is_enabled());
    assert!(started.worker.shutdown(100));
}

#[test]
fn config_validation_is_strict() {
    assert_eq!(Config::from_value(None).unwrap(), Config::default());
    assert_eq!(Config::from_value(Some("basic")).unwrap().mode, Mode::Basic);
    assert_eq!(Config::from_value(Some("off")).unwrap().mode, Mode::Off);
    assert!(Config::from_value(Some("verbose")).is_err());
    let default = Config::default();
    assert_eq!((default.rate_per_s, default.burst), (8, 16));
}

#[test]
fn progress_is_sampled_once_per_second() {
    let clock = manual(1_000);
    let (obs, worker, sink) = start(cfg(1000, 1000), Arc::clone(&clock));
    for _ in 0..50 {
        obs.emit(ev(EventKind::LiveReadProgress).reason(Reason::Sampled));
    }
    assert_eq!(obs.stats().emitted, 1, "one sample per second");
    assert_eq!(obs.stats().rate_suppressed, 49);
    clock.advance(999);
    obs.emit(ev(EventKind::LiveReadProgress));
    assert_eq!(obs.stats().emitted, 1, "same second is suppressed");
    clock.advance(1);
    obs.emit(ev(EventKind::LiveReadProgress));
    assert_eq!(obs.stats().emitted, 2, "next second admits one");
    drop(obs);
    assert!(worker.shutdown(2_000));
    let data: Vec<String> = sink
        .lines()
        .into_iter()
        .filter(|l| l.contains("event=live_read_progress"))
        .collect();
    assert_eq!(data.len(), 2);
}

#[test]
fn global_burst_is_bounded_and_counted() {
    let (obs, worker, sink) = start(cfg(1000, 1000), manual(0));
    for _ in 0..40 {
        obs.emit(ev(EventKind::ReanchorCommit));
    }
    drop(obs);
    assert!(worker.shutdown(2_000));
    assert_eq!(sink.len(), 40, "large burst admits the whole batch");

    let started =
        Observer::start(cfg(8, 16), manual(0), Box::new(MemorySink::new())).expect("spawns");
    for _ in 0..40 {
        started.observer.emit(ev(EventKind::ReanchorCommit));
    }
    let stats = started.observer.stats();
    assert_eq!(stats.emitted, 16);
    assert_eq!(stats.rate_suppressed, 24);
    assert!(started.worker.shutdown(2_000));
}

struct BlockedSink {
    gate: Arc<(Mutex<bool>, Condvar)>,
    lines: Arc<AtomicUsize>,
}

impl Sink for BlockedSink {
    fn write_line(&mut self, _line: &[u8]) -> io::Result<()> {
        let (lock, cv) = &*self.gate;
        let mut open = lock.lock().unwrap();
        while !*open {
            open = cv.wait(open).unwrap();
        }
        self.lines.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
}

#[test]
fn blocked_sink_never_blocks_producers() {
    let gate = Arc::new((Mutex::new(false), Condvar::new()));
    let lines = Arc::new(AtomicUsize::new(0));
    let sink = BlockedSink {
        gate: Arc::clone(&gate),
        lines: Arc::clone(&lines),
    };
    let started = Observer::start(cfg(100_000, 100_000), manual(1), Box::new(sink)).unwrap();
    let obs = started.observer;
    let t0 = Instant::now();
    for _ in 0..400 {
        obs.emit(ev(EventKind::FuseOpenRequest));
    }
    let elapsed = t0.elapsed();
    let stats = obs.stats();
    assert!(stats.queue_full >= 1, "queue bound reached");
    assert!(
        elapsed < Duration::from_secs(2),
        "producers completed in {elapsed:?} without the sink"
    );
    {
        let (lock, cv) = &*gate;
        *lock.lock().unwrap() = true;
        cv.notify_all();
    }
    assert!(started.worker.shutdown(2_000), "worker exits after release");
    assert!(lines.load(Ordering::SeqCst) >= 1);
}

#[test]
fn sink_failure_is_counted_without_panicking() {
    let clock = manual(0);
    let sink = Arc::new(MemorySink::new());
    sink.fail_writes(true);
    let started = Observer::start(cfg(1000, 1000), clock, Box::new(sink)).unwrap();
    for _ in 0..5 {
        started.observer.emit(ev(EventKind::FifoOpenError));
    }
    drop(started.observer);
    assert!(started.worker.shutdown(2_000));
}

#[test]
fn drops_are_summarised_once() {
    let clock = manual(1_000);
    let (obs, worker, sink) = start(cfg(1000, 1000), Arc::clone(&clock));
    for _ in 0..4 {
        obs.emit(ev(EventKind::FifoEof));
    }
    for _ in 0..4 {
        obs.emit(ev(EventKind::LiveReadProgress));
    }
    assert!(obs.stats().dropped() >= 3, "{:?}", obs.stats());
    clock.advance(2_000);
    obs.emit(ev(EventKind::ProcessStop));
    drop(obs);
    assert!(worker.shutdown(2_000));
    let lines = sink.lines();
    let summaries: Vec<&String> = lines
        .iter()
        .filter(|l| l.contains("event=drops_summary"))
        .collect();
    assert_eq!(summaries.len(), 1, "one summary at most");
    assert!(summaries[0].contains("dropped="));
    assert!(summaries[0].contains("clock=boottime"));
    assert!(
        summaries[0].contains("event_seq=0"),
        "summary keeps the required common keys: {}",
        summaries[0]
    );
}

#[test]
fn single_producer_allocation_order_matches_output_order() {
    // Single producer only: `event_seq` is allocated before enqueue, so with one
    // emitter the allocation order and the file order agree. With several
    // producers that is not guaranteed — see the concurrent test below.
    let (obs, worker, sink) = start(cfg(1000, 1000), manual(0));
    obs.emit(ev(EventKind::ProcessStart));
    obs.emit(ev(EventKind::ReanchorCommit));
    drop(obs);
    assert!(worker.shutdown(2_000));
    assert_eq!(
        seqs_of(&sink.lines()),
        vec!["1".to_string(), "2".to_string()]
    );
}

/// Clock that holds the first producer until the second producer has finished
/// its whole `emit`; the worker never calls this clock first (an event must be
/// enqueued first, and enqueueing requires an earlier clock call).
///
/// `Observer::start` performs call 0 (instance identity), so call 1 is the first
/// producer: blocking there is what forces the cross-thread inversion.
struct OrderedClock {
    calls: AtomicUsize,
    second_done: Arc<AtomicBool>,
}

impl Clock for OrderedClock {
    fn boot_ms(&self) -> Option<u64> {
        if self.calls.fetch_add(1, Ordering::SeqCst) == 1 {
            while !self.second_done.load(Ordering::SeqCst) {
                std::thread::yield_now();
            }
        }
        Some(0)
    }
}

fn seqs_of(lines: &[String]) -> Vec<String> {
    lines
        .iter()
        .map(|l| {
            l.split("event_seq=")
                .nth(1)
                .and_then(|r| r.split(' ').next())
                .unwrap_or("?")
                .to_string()
        })
        .collect()
}

#[test]
fn concurrent_sequences_are_unique_allocation_ids_that_may_reorder() {
    let sink = Arc::new(MemorySink::new());
    let second_done = Arc::new(AtomicBool::new(false));
    let clock = Arc::new(OrderedClock {
        calls: AtomicUsize::new(0),
        second_done: Arc::clone(&second_done),
    });
    let started =
        Observer::start(cfg(1000, 1000), clock.clone(), Box::new(Arc::clone(&sink))).unwrap();
    let first = started.observer.clone();
    let second = started.observer.clone();

    let a = std::thread::spawn(move || first.emit(ev(EventKind::ProcessStart)));
    // Call 0 is `start()`; call 1 is this first producer, which already holds
    // seq 1 and is now waiting for the second producer.
    while clock.calls.load(Ordering::SeqCst) < 2 {
        std::thread::yield_now();
    }
    let b = std::thread::spawn(move || {
        second.emit(ev(EventKind::FifoEof));
        second_done.store(true, Ordering::SeqCst);
    });
    b.join().unwrap();
    a.join().unwrap();

    drop(started.observer);
    assert!(started.worker.shutdown(2_000));
    let seqs = seqs_of(&sink.lines());
    let mut unique: Vec<u32> = seqs.iter().map(|s| s.parse().unwrap()).collect();
    assert_eq!(unique.len(), 2);
    unique.sort_unstable();
    assert_eq!(unique, vec![1, 2], "sequences are unique allocation ids");
    assert_eq!(
        seqs,
        vec!["2".to_string(), "1".to_string()],
        "allocation order is not wall-output order for concurrent producers"
    );
}
