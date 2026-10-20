//! Bounded, loss-accounted observation channel for USB-Stream-TV (TASK-003).
//!
//! Diagnostics side-channel only: producers stamp a small [`Event`] and enqueue
//! it with `try_send`; one worker formats `USBTV_OBS` lines into an injected
//! [`Sink`]. No producer thread formats, does IO or starts a worker, and no
//! value produced here participates in playback, FUSE or watchdog decisions.
//!
//! Loss is explicit: a token bucket, a bounded queue and atomic counters, so
//! enabled telemetry may drop and never waits while disabled telemetry is inert.

use std::fmt;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender, TrySendError};
use std::sync::Arc;

pub mod format;
pub mod gate;
pub mod sinks;
pub mod writer;

#[cfg(test)]
mod budget_tests;
#[cfg(test)]
mod format_tests;
#[cfg(test)]
mod tests;

use gate::Gate;

pub use format::format_line;
pub use sinks::{MemorySink, Sink, StderrSink};
pub use writer::{BootClock, Clock, Counters, Instance, ManualClock, Stats, Worker};

/// Schema version written on every line.
pub const SCHEMA_VERSION: u64 = 1;
/// Hard bound for one [`Event`] payload (`size_of::<Event>()` is asserted).
pub const MAX_EVENT_BYTES: usize = 256;
/// Bounded queue capacity in events (128 * 144 B = 18 KiB).
pub const QUEUE_CAPACITY: usize = 128;
/// Hard bound for one formatted line, newline included.
pub const MAX_LINE_BYTES: usize = 512;
/// Sentinel for "field not applicable"; never formatted.
pub const UNSET: u64 = u64::MAX;

/// Declares a small `#[repr(u8)]` enum whose variants carry stable wire tokens;
/// `as_str` is generated from the same table, so a variant cannot drift.
macro_rules! token_enum {
    ($(#[$meta:meta])* $name:ident { $($variant:ident = $value:literal => $token:literal),+ $(,)? }) => {
        $(#[$meta])*
        #[derive(Clone, Copy, Debug, PartialEq, Eq)]
        #[repr(u8)]
        pub enum $name {
            $($variant = $value),+
        }

        impl $name {
            /// Stable wire token for this value.
            pub fn as_str(self) -> &'static str {
                match self {
                    $(Self::$variant => $token),+
                }
            }
        }
    };
}

token_enum! {
    /// Emitting component. Values are part of the stable wire schema.
    Component {
        Process = 1 => "process",
        Feeder = 2 => "feeder",
        Engine = 3 => "engine",
        Fuse = 4 => "fuse",
        Watchdog = 5 => "watchdog",
    }
}

token_enum! {
    /// Event name. Values are part of the stable wire schema.
    EventKind {
        ProcessStart = 1 => "process_start",
        ProcessStop = 2 => "process_stop",
        FifoOpenError = 3 => "fifo_open_error",
        FifoEof = 4 => "fifo_eof",
        FifoReadError = 5 => "fifo_read_error",
        FifoIdle = 6 => "fifo_idle",
        FifoDataResumed = 7 => "fifo_data_resumed",
        FifoFlushObserved = 8 => "fifo_flush_observed",
        LiveReadProgress = 9 => "live_read_progress",
        RingUnderrun = 10 => "ring_underrun",
        ReanchorCommit = 11 => "reanchor_commit",
        OverwrittenNoncommit = 12 => "overwritten_noncommit",
        ResyncRequestResult = 13 => "resync_request_result",
        FuseOpenRequest = 14 => "fuse_open_request",
        FuseOpenResult = 15 => "fuse_open_result",
        FuseRelease = 16 => "fuse_release",
        FuseReadResult = 17 => "fuse_read_result",
        FuseDeviceReadError = 18 => "fuse_device_read_error",
        FuseReplyWriteResult = 19 => "fuse_reply_write_result",
        FuseLoopExit = 20 => "fuse_loop_exit",
        DropsSummary = 21 => "drops_summary",
        ClockUnavailable = 22 => "clock_unavailable",
        CalibrationInvalid = 23 => "calibration_invalid",
        ResyncMarkerObserved = 24 => "resync_marker_observed",
        ResyncDecision = 25 => "resync_decision",
        GadgetObservation = 26 => "gadget_observation",
    }
}

impl EventKind {
    /// Rate class used by the observer gates.
    fn class(self) -> gate::Class {
        match self {
            EventKind::LiveReadProgress | EventKind::FuseReadResult => gate::Class::Progress,
            EventKind::FifoIdle => gate::Class::Idle,
            EventKind::DropsSummary => gate::Class::Summary,
            _ => gate::Class::Control,
        }
    }
}

token_enum! {
    /// Small enumerated reason. Free-form text is deliberately impossible.
    Reason {
        None = 0 => "none",
        Sampled = 1 => "sampled",
        Cold = 2 => "cold",
        Restart = 3 => "restart",
        ChannelSwitch = 4 => "channel_switch",
        BehindRing = 5 => "behind_ring",
        ResumeAfterGap = 6 => "resume_after_gap",
        ConfirmedSeek = 7 => "confirmed_seek",
        MappingUnderflow = 8 => "mapping_underflow",
        ReferenceInvalid = 9 => "reference_invalid",
        FutureProbe = 10 => "future_probe",
        ProbeReject = 11 => "probe_reject",
        Eof = 12 => "eof",
        ReadError = 13 => "read_error",
        Interrupted = 14 => "interrupted",
        Flush = 15 => "flush",
        DataResumed = 16 => "data_resumed",
        Idle = 17 => "idle",
        MarkerConsumed = 18 => "marker_consumed",
        ModeSuppressed = 19 => "mode_suppressed",
        MissingSwitch = 20 => "missing_switch",
        Cooldown = 21 => "cooldown",
        Breaker = 22 => "breaker",
        Launch = 23 => "launch",
        LaunchFailed = 24 => "launch_failed",
        GadgetChanged = 25 => "gadget_changed",
        GadgetUnknown = 26 => "gadget_unknown",
        OpenInvalidNode = 27 => "open_invalid_node",
        OpenRoot = 28 => "open_root",
        OpenNode = 29 => "open_node",
        BackingImage = 30 => "backing_image",
        ShortWrite = 31 => "short_write",
        WriteError = 32 => "write_error",
        LoopUnclean = 33 => "loop_unclean",
        LoopClean = 34 => "loop_clean",
        Unknown = 35 => "unknown",
        OverwrittenRecovery = 36 => "overwritten_recovery",
        Reanchor = 37 => "reanchor",
        Starve = 38 => "starve",
        ResyncRequest = 39 => "resync_request",
        MarkerRequestOk = 40 => "marker_request_ok",
        MarkerRequestFailed = 41 => "marker_request_failed",
    }
}

token_enum! {
    /// Clock domain marker on the wire.
    ClockState {
        Boottime = 1 => "boottime",
        Unavailable = 2 => "unavailable",
    }
}

// --------------------------------------------------------------------- event

/// Declares [`Event`]: header fields plus the numeric payload fields, each with
/// a `u64` builder setter. The payload field list is written exactly once.
macro_rules! event_type {
    ($($field:ident)+) => {
        /// Fixed-size numeric event: no strings, URLs, paths, tokens or
        /// credentials; every payload field is `UNSET` unless set.
        #[derive(Clone, Copy, Debug, PartialEq, Eq)]
        pub struct Event {
            pub component: Component,
            pub kind: EventKind,
            pub reason: Reason,
            pub clock: ClockState,
            pub boot_ms: u64,
            pub event_seq: u64,
            pub errno: u64,
            $(pub $field: u64,)+
        }

        impl Event {
            /// Header-only event; every payload field starts `UNSET`.
            pub fn new(component: Component, kind: EventKind) -> Self {
                Self {
                    component,
                    kind,
                    reason: Reason::None,
                    clock: ClockState::Unavailable,
                    boot_ms: UNSET,
                    event_seq: UNSET,
                    errno: UNSET,
                    $($field: UNSET,)+
                }
            }

            pub fn reason(mut self, reason: Reason) -> Self {
                self.reason = reason;
                self
            }

            $(pub fn $field(mut self, v: u64) -> Self {
                self.$field = v;
                self
            })+

            /// Stores a signed errno in the unsigned payload slot.
            pub fn errno(mut self, v: i32) -> Self {
                self.errno = v as i64 as u64;
                self
            }
        }
    };
}

event_type! {
    file_id
    fh
    nodeid
    req_unique
    anchor_epoch
    foff
    len
    mapped_spos
    s_write
    ring_old
    real_bytes
    null_bytes
    wait_ms
    dropped
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Off,
    Basic,
}

/// Startup configuration, read once by `main` (never per read).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Config {
    pub mode: Mode,
    pub rate_per_s: u32,
    pub burst: u32,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            mode: Mode::Basic,
            rate_per_s: 8,
            burst: 16,
        }
    }
}

impl Config {
    /// Validates the raw `USBTV_OBS` value (`off`, `basic`, or absent).
    pub fn from_value(value: Option<&str>) -> Result<Self, InitError> {
        match value {
            None => Ok(Self::default()),
            Some("off") => Ok(Self {
                mode: Mode::Off,
                ..Self::default()
            }),
            Some("basic") => Ok(Self::default()),
            Some(_) => Err(InitError::Config),
        }
    }

    pub fn from_env() -> Result<Self, InitError> {
        let raw = std::env::var("USBTV_OBS").ok();
        Self::from_value(raw.as_deref())
    }
}

#[derive(Debug)]
pub enum InitError {
    Config,
    Spawn(std::io::Error),
}

impl fmt::Display for InitError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            InitError::Config => write!(f, "invalid USBTV_OBS (expected off|basic)"),
            InitError::Spawn(e) => write!(f, "observation worker spawn failed: {e}"),
        }
    }
}

// ---------------------------------------------------------------------- gate

// `Class` and the fixed-size token bucket live in `observability::gate`; the
// bucket keeps full-width boot milliseconds and is shared with the worker so
// that drop summaries are bounded by the same global budget as producers.

// ------------------------------------------------------------------ observer

struct Inner {
    tx: Option<SyncSender<Event>>,
    seq: AtomicU64,
    clock: Arc<dyn Clock>,
    instance: Instance,
    global: Arc<Gate>,
    progress: Gate,
    idle: Gate,
    summary: Gate,
    stats: Arc<Counters>,
}

/// Cheap-to-clone producer handle. `disabled()` allocates no worker, no
/// channel and performs no IO; it still accounts attempted events.
#[derive(Clone)]
pub struct Observer {
    inner: Arc<Inner>,
}

pub struct Started {
    pub observer: Observer,
    pub worker: Worker,
}

impl Observer {
    /// Builds the shared inner state; the gates start full for `cfg.burst`.
    fn inner(
        tx: Option<SyncSender<Event>>,
        clock: Arc<dyn Clock>,
        instance: Instance,
        stats: Arc<Counters>,
        global: Arc<Gate>,
    ) -> Arc<Inner> {
        Arc::new(Inner {
            tx,
            seq: AtomicU64::new(0),
            clock,
            instance,
            global,
            progress: Gate::new(1, 1),
            idle: Gate::new(1, 1),
            summary: Gate::new(1, 1),
            stats,
        })
    }

    /// Disabled observer: counters only, no queue, no sink, no thread.
    pub fn disabled() -> Self {
        let cfg = Config {
            mode: Mode::Off,
            ..Config::default()
        };
        let stats = Arc::new(Counters::default());
        let clock = Arc::new(writer::UnavailableClock);
        let global = Arc::new(Gate::new(cfg.rate_per_s, cfg.burst));
        Self {
            inner: Self::inner(None, clock, Instance::unknown(), stats, global),
        }
    }

    /// Starts the single output worker (or returns a disabled observer for
    /// `Mode::Off`). Failure degrades to disabled telemetry at the call site.
    pub fn start(
        cfg: Config,
        clock: Arc<dyn Clock>,
        sink: Box<dyn Sink>,
    ) -> Result<Started, InitError> {
        let stats = Arc::new(Counters::default());
        if cfg.mode == Mode::Off {
            let observer = Self::disabled();
            return Ok(Started {
                observer,
                worker: Worker::idle(),
            });
        }
        let instance = Instance::new(clock.boot_ms());
        let global = Arc::new(Gate::new(cfg.rate_per_s, cfg.burst));
        let (tx, rx): (SyncSender<Event>, Receiver<Event>) = sync_channel(QUEUE_CAPACITY);
        let worker = Worker::spawn(
            rx,
            sink,
            Arc::clone(&clock),
            Arc::clone(&stats),
            instance,
            Arc::clone(&global),
        )?;
        let observer = Self {
            inner: Self::inner(Some(tx), clock, instance, stats, global),
        };
        Ok(Started { observer, worker })
    }

    pub fn is_enabled(&self) -> bool {
        self.inner.tx.is_some()
    }

    pub fn stats(&self) -> Stats {
        self.inner.stats.snapshot()
    }

    pub fn instance(&self) -> Instance {
        self.inner.instance
    }

    /// Nonblocking publish. Never formats, never touches the sink, never waits
    /// for capacity, and never panics.
    pub fn emit(&self, ev: Event) {
        let inner = &self.inner;
        let seq = inner.seq.fetch_add(1, Ordering::Relaxed).saturating_add(1);
        let now = inner.clock.boot_ms();
        let mut ev = ev;
        ev.event_seq = seq;
        match now {
            Some(ms) => {
                ev.boot_ms = ms;
                ev.clock = ClockState::Boottime;
            }
            None => {
                ev.boot_ms = UNSET;
                ev.clock = ClockState::Unavailable;
            }
        }
        let Some(tx) = inner.tx.as_ref() else {
            inner.stats.note_disabled();
            return;
        };
        let class_ok = match ev.kind.class() {
            gate::Class::Control => true,
            gate::Class::Progress => inner.progress.allow(now),
            gate::Class::Idle => inner.idle.allow(now),
            gate::Class::Summary => inner.summary.allow(now),
        };
        if !class_ok {
            inner.stats.note_suppressed();
            return;
        }
        if !inner.global.allow(now) {
            inner.stats.note_suppressed();
            return;
        }
        match tx.try_send(ev) {
            Ok(()) => inner.stats.note_emitted(),
            Err(TrySendError::Full(_)) => inner.stats.note_queue_full(),
            Err(TrySendError::Disconnected(_)) => inner.stats.note_disabled(),
        }
    }
}
