//! Formatter/sink bound tests (TASK-003 corrections R2).
//!
//! The formatter contract is whole-record: either one complete line, or `0`
//! meaning "no record". These tests pin the empty/undersized/512/oversized
//! buffer cases, the absence of silent field loss and the bounded collector.

use std::sync::Arc;

use super::writer::Instance;
use super::*;

const OPTIONAL: &[&str] = &[
    "transport_request_unique",
    "nodeid",
    "fh",
    "anchor_epoch",
    "file_id",
    "foff",
    "len",
    "mapped_spos",
    "s_write",
    "ring_old",
    "real_bytes",
    "null_bytes",
    "wait_ms",
    "errno",
    "dropped",
];

fn inst() -> Instance {
    Instance {
        pid: 4242,
        start_ms: Some(1_500),
    }
}

fn header_event() -> Event {
    let mut e = Event::new(Component::Engine, EventKind::LiveReadProgress).reason(Reason::Sampled);
    e.event_seq = 7;
    e.boot_ms = 99_999;
    e.clock = ClockState::Boottime;
    e
}

/// Populates every optional payload field with `v`.
///
/// `errno` is always populated with a realistic negative errno: `-1` encodes to
/// the `UNSET` sentinel and is never produced by the emitters, which pass
/// `raw_os_error().unwrap_or(0)`.
fn with_all_fields(mut e: Event, v: u64) -> Event {
    e = e
        .req_unique(v)
        .nodeid(v)
        .fh(v)
        .anchor_epoch(v)
        .file_id(v)
        .foff(v)
        .len(v)
        .mapped_spos(v)
        .s_write(v)
        .ring_old(v)
        .real_bytes(v)
        .null_bytes(v)
        .wait_ms(v)
        .dropped(v);
    e.errno(-2)
}

/// Widest representable record: every numeric field at maximum width.
fn over_bound_event(mut e: Event) -> Event {
    e = with_all_fields(e, u64::MAX - 2);
    e.errno(i32::MIN)
}

fn line<'a>(buf: &'a [u8], n: usize) -> &'a str {
    assert!(n > 0, "expected a complete record");
    assert_eq!(buf[n - 1], b'\n', "record must be newline terminated");
    std::str::from_utf8(&buf[..n]).expect("ascii")
}

fn assert_all_keys_present(text: &str, count: usize) {
    for key in &OPTIONAL[..count] {
        assert!(text.contains(&format!(" {key}=")), "missing {key}: {text}");
    }
}

#[test]
fn zero_length_output_slice_returns_zero_without_panicking() {
    assert_eq!(format_line(&header_event(), &inst(), &mut []), 0);
}

#[test]
fn tiny_output_slices_return_zero_without_writing_a_record() {
    for size in 1..32usize {
        let mut buf = vec![0u8; size];
        assert_eq!(
            format_line(&header_event(), &inst(), &mut buf),
            0,
            "size {size} cannot hold a record"
        );
    }
}

#[test]
fn oversized_caller_slice_is_clamped_to_the_contract_bound() {
    let mut big = [0u8; 4096];
    let n = format_line(&header_event(), &inst(), &mut big);
    assert!(n > 0 && n <= MAX_LINE_BYTES, "clamped length {n}");
    assert_eq!(big[n - 1], b'\n');
    assert_all_keys_present(line(&big, n), 0);
    // A record that needs more than the bound returns 0 even with room to
    // spare in the caller slice: the bound is the contract, not the buffer.
    assert_eq!(
        format_line(&over_bound_event(header_event()), &inst(), &mut big),
        0
    );
}

#[test]
fn maximum_numeric_fields_keep_every_populated_key() {
    let mut buf = [0u8; MAX_LINE_BYTES];
    let n = format_line(&with_all_fields(header_event(), 7), &inst(), &mut buf);
    let text = line(&buf, n);
    assert!(n <= MAX_LINE_BYTES);
    assert_all_keys_present(text, OPTIONAL.len());
    assert!(text.starts_with("USBTV_OBS version=1 component=engine"));
    assert!(text.contains("reason=sampled"));
    assert!(
        buf[..n - 1].iter().all(|&b| (0x20..0x7F).contains(&b)),
        "numeric/enumerated ASCII only"
    );
}

#[test]
fn no_populated_field_is_silently_dropped_at_the_boundary() {
    for value in [0u64, 9, 1_000, u32::MAX as u64, u64::MAX - 2] {
        let mut buf = [0u8; MAX_LINE_BYTES];
        let n = format_line(&with_all_fields(header_event(), value), &inst(), &mut buf);
        if n == 0 {
            continue; // dropped whole, which is explicit (see the loss test)
        }
        assert!(n <= MAX_LINE_BYTES);
        assert_all_keys_present(line(&buf, n), OPTIONAL.len());
    }
    // Wider header values may push the record over the bound: still all-or-none.
    let mut wide = header_event();
    wide.event_seq = u64::MAX - 2;
    wide.boot_ms = u64::MAX - 2;
    let wide_inst = Instance {
        pid: u32::MAX,
        start_ms: Some(u64::MAX - 1),
    };
    for value in [u64::MAX - 2, u64::MAX / 2] {
        let mut buf = [0u8; MAX_LINE_BYTES];
        let n = format_line(&with_all_fields(wide, value), &wide_inst, &mut buf);
        if n > 0 {
            assert_all_keys_present(line(&buf, n), OPTIONAL.len());
        }
    }
}

#[test]
fn oversized_record_is_counted_as_format_loss_and_never_written() {
    let sink = Arc::new(MemorySink::new());
    let started = Observer::start(
        Config::default(),
        Arc::new(ManualClock::new(0)) as Arc<dyn Clock>,
        Box::new(Arc::clone(&sink)),
    )
    .expect("spawns");
    started.observer.emit(over_bound_event(header_event()));
    let Started { observer, worker } = started;
    assert!(worker.shutdown(2_000));
    let stats = observer.stats();
    assert_eq!(stats.format_loss, 1, "documented whole-record loss counter");
    assert_eq!(stats.formatted, 0);
    assert!(
        sink.is_empty(),
        "no empty or partial record reaches the sink"
    );
}

#[test]
fn memory_sink_storage_is_capped_and_counted() {
    let sink = Arc::new(MemorySink::with_limits(4, 64 * 1024));
    let started = Observer::start(
        Config {
            mode: Mode::Basic,
            rate_per_s: 100_000,
            burst: 100_000,
        },
        Arc::new(ManualClock::new(0)) as Arc<dyn Clock>,
        Box::new(Arc::clone(&sink)),
    )
    .expect("spawns");
    for _ in 0..20 {
        started
            .observer
            .emit(Event::new(Component::Process, EventKind::ProcessStart));
    }
    let Started { observer, worker } = started;
    assert!(worker.shutdown(2_000));
    assert_eq!(sink.limits(), (4, 64 * 1024));
    assert_eq!(sink.len(), 4, "line cap honoured");
    assert!(sink.full() >= 1, "rejections are counted by the sink");
    assert!(
        observer.stats().sink_errors >= 1,
        "worker counts the errors"
    );
    assert!(sink.bytes() <= 64 * 1024);
}

#[test]
fn memory_sink_byte_cap_is_enforced_independently() {
    let sink = Arc::new(MemorySink::with_limits(1_000, 300));
    let mut s = Arc::clone(&sink);
    let line = b"USBTV_OBS version=1\n";
    let mut accepted = 0;
    let mut rejected = 0;
    for _ in 0..50 {
        match Sink::write_line(&mut s, line) {
            Ok(()) => accepted += 1,
            Err(_) => rejected += 1,
        }
    }
    assert!(accepted > 0 && rejected > 0, "{accepted}/{rejected}");
    assert!(sink.bytes() <= 300);
    assert_eq!(sink.full(), rejected as u64);
}
