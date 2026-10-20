//! TASK-003: end-to-end observation and schema parity with the contract doc.

use std::sync::Arc;

use fuse_ntfs::contract::StreamBackend;
use fuse_ntfs::engine::LiveEngine;
use fuse_ntfs::observability::{
    format_line, ClockState, Component, Config, Event, EventKind, Instance, ManualClock,
    MemorySink, Mode, Observer, Reason, MAX_EVENT_BYTES, MAX_LINE_BYTES, SCHEMA_VERSION,
};

const DOC: &str = include_str!("../../../docs/architecture/OBSERVABILITY_EVENT_CONTRACT.md");

const ALL_KINDS: &[EventKind] = &[
    EventKind::ProcessStart,
    EventKind::ProcessStop,
    EventKind::FifoOpenError,
    EventKind::FifoEof,
    EventKind::FifoReadError,
    EventKind::FifoIdle,
    EventKind::FifoDataResumed,
    EventKind::FifoFlushObserved,
    EventKind::LiveReadProgress,
    EventKind::RingUnderrun,
    EventKind::ReanchorCommit,
    EventKind::OverwrittenNoncommit,
    EventKind::ResyncRequestResult,
    EventKind::FuseOpenRequest,
    EventKind::FuseOpenResult,
    EventKind::FuseRelease,
    EventKind::FuseReadResult,
    EventKind::FuseDeviceReadError,
    EventKind::FuseReplyWriteResult,
    EventKind::FuseLoopExit,
    EventKind::DropsSummary,
    EventKind::ClockUnavailable,
    EventKind::CalibrationInvalid,
    EventKind::ResyncMarkerObserved,
    EventKind::ResyncDecision,
    EventKind::GadgetObservation,
];

const ALL_REASONS: &[Reason] = &[
    Reason::None,
    Reason::Sampled,
    Reason::Cold,
    Reason::Restart,
    Reason::ChannelSwitch,
    Reason::BehindRing,
    Reason::ResumeAfterGap,
    Reason::ConfirmedSeek,
    Reason::MappingUnderflow,
    Reason::ReferenceInvalid,
    Reason::FutureProbe,
    Reason::ProbeReject,
    Reason::Eof,
    Reason::ReadError,
    Reason::Interrupted,
    Reason::Flush,
    Reason::DataResumed,
    Reason::Idle,
    Reason::MarkerConsumed,
    Reason::ModeSuppressed,
    Reason::MissingSwitch,
    Reason::Cooldown,
    Reason::Breaker,
    Reason::Launch,
    Reason::LaunchFailed,
    Reason::GadgetChanged,
    Reason::GadgetUnknown,
    Reason::OpenInvalidNode,
    Reason::OpenRoot,
    Reason::OpenNode,
    Reason::BackingImage,
    Reason::ShortWrite,
    Reason::WriteError,
    Reason::LoopUnclean,
    Reason::LoopClean,
    Reason::Unknown,
    Reason::OverwrittenRecovery,
    Reason::Reanchor,
    Reason::Starve,
    Reason::ResyncRequest,
    Reason::MarkerRequestOk,
    Reason::MarkerRequestFailed,
];

fn section<'a>(doc: &'a str, heading: &str) -> &'a str {
    let start = doc
        .find(heading)
        .unwrap_or_else(|| panic!("missing section {heading}"));
    let rest = &doc[start + heading.len()..];
    let end = rest.find("\n## ").unwrap_or(rest.len());
    &rest[..end]
}

fn backticked(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut rest = text;
    while let Some(open) = rest.find('`') {
        let after = &rest[open + 1..];
        let Some(close) = after.find('`') else { break };
        out.push(after[..close].to_string());
        rest = &after[close + 1..];
    }
    out
}

fn sorted(mut v: Vec<String>) -> Vec<String> {
    v.sort();
    v.dedup();
    v
}

#[test]
fn contract_doc_matches_rust_schema() {
    let required = backticked(section(DOC, "## Required common keys"));
    assert_eq!(
        required,
        vec![
            "version",
            "component",
            "event",
            "pid",
            "instance",
            "event_seq",
            "clock",
            "boot_ms",
            "precision_ms",
            "reason",
        ]
    );

    let optional = backticked(section(DOC, "## Optional numeric keys"));
    assert_eq!(
        optional,
        vec![
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
        ]
    );

    let rust_events = sorted(ALL_KINDS.iter().map(|k| k.as_str().to_string()).collect());
    let doc_events = sorted(backticked(section(DOC, "## Event names")));
    assert_eq!(doc_events, rust_events, "documented event set");

    let rust_reasons = sorted(ALL_REASONS.iter().map(|r| r.as_str().to_string()).collect());
    let doc_reasons = sorted(backticked(section(DOC, "## Reason tokens")));
    assert_eq!(doc_reasons, rust_reasons, "documented reason set");
}

#[test]
fn formatted_line_has_required_keys_in_contract_order() {
    let inst = Instance {
        pid: 4242,
        start_ms: Some(1_500),
    };
    let mut event = Event::new(Component::Engine, EventKind::LiveReadProgress)
        .reason(Reason::Sampled)
        .req_unique(1)
        .nodeid(2)
        .fh(3)
        .anchor_epoch(4)
        .file_id(5)
        .foff(6)
        .len(7)
        .mapped_spos(8)
        .s_write(9)
        .ring_old(10)
        .real_bytes(11)
        .null_bytes(12)
        .wait_ms(13)
        .dropped(14)
        .errno(-5);
    event.event_seq = 4_242_424;
    event.boot_ms = 99_999;
    event.clock = ClockState::Boottime;

    let mut buf = [0u8; MAX_LINE_BYTES];
    let n = format_line(&event, &inst, &mut buf);
    assert!(n <= MAX_LINE_BYTES);
    let line = std::str::from_utf8(&buf[..n]).expect("ascii");
    assert_eq!(buf[n - 1], b'\n');

    let keys: Vec<&str> = line
        .trim_end()
        .split(' ')
        .filter_map(|kv| kv.split_once('=').map(|(k, _)| k))
        .collect();
    assert_eq!(
        &keys[..10],
        &[
            "version",
            "component",
            "event",
            "pid",
            "instance",
            "event_seq",
            "clock",
            "boot_ms",
            "precision_ms",
            "reason",
        ]
    );
    assert_eq!(
        &keys[10..],
        &[
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
        ]
    );
    assert!(line.contains(&format!("version={SCHEMA_VERSION}")));
    assert!(line.contains("pid=4242"));
    assert!(line.contains("instance=4242.1500"));
    assert!(line.contains("precision_ms=1"));
    assert!(line.contains("errno=-5"));
}

#[test]
fn event_payload_stays_within_the_contract_bound() {
    assert!(
        std::mem::size_of::<Event>() <= MAX_EVENT_BYTES,
        "{} bytes",
        std::mem::size_of::<Event>()
    );
}

#[test]
fn engine_observations_reach_the_bounded_sink() {
    let sink = Arc::new(MemorySink::new());
    let clock = Arc::new(ManualClock::new(20_000));
    let started = Observer::start(
        Config {
            mode: Mode::Basic,
            rate_per_s: 64,
            burst: 64,
        },
        clock,
        Box::new(Arc::clone(&sink)),
    )
    .expect("observer starts");

    let engine = Arc::new(LiveEngine::with_observer(started.observer));
    engine.ring.write_chunk(&vec![0x47u8; 8 * 1024 * 1024]);

    let mut buf = vec![0u8; 188 * 16];
    let n = engine.serve_live(0, 0, &mut buf, 3_500);
    assert_eq!(n, buf.len());
    drop(engine);
    assert!(started.worker.shutdown(2_000), "worker drained");

    let lines = sink.lines();
    let progress: Vec<&String> = lines
        .iter()
        .filter(|l| l.contains("event=live_read_progress"))
        .collect();
    assert_eq!(progress.len(), 1, "progress is sampled once: {lines:?}");
    let line = progress[0];
    assert!(line.contains("component=engine"), "{line}");
    assert!(line.contains("reason=cold"), "{line}");
    assert!(line.contains("foff=0"), "{line}");
    assert!(line.contains("len=3008"), "{line}");
    assert!(line.contains("clock=boottime boot_ms=20000"), "{line}");
    assert!(line.len() <= MAX_LINE_BYTES);
    assert!(line.is_ascii());
}

// The parity fixtures and scenarios live in two cohesive child modules of this
// integration target (explicit paths, so they are not auto-discovered Cargo
// targets and the target list is unchanged).

#[path = "observability/fixtures.rs"]
mod fixtures;

#[path = "observability/parity.rs"]
mod parity;
