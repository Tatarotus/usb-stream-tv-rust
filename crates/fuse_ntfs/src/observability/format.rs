//! Whole-record line formatter (TASK-003, corrected in TASK-003-R1/R2).
//!
//! Contract: [`format_line`] either produces one complete record terminated by
//! `\n` and returns its length, or it returns `0` meaning "no record". A `0`
//! result is never a partial line: the caller must not write anything (the
//! contents of `out` are unspecified). Fields are never truncated and trailing
//! populated fields are never silently dropped; a record whose complete key set
//! does not fit is dropped whole and accounted as a formatting loss.

use std::io::Write;

use super::writer::Instance;
use super::{ClockState, Event, MAX_LINE_BYTES, UNSET};

/// Incremental writer that tracks a single whole-record overflow flag.
struct Line<'a> {
    buf: &'a mut [u8],
    len: usize,
    overflow: bool,
}

impl<'a> Line<'a> {
    fn new(buf: &'a mut [u8]) -> Self {
        Self {
            buf,
            len: 0,
            overflow: false,
        }
    }

    /// Payload capacity: the last byte of the slice stays reserved for `\n`.
    fn limit(&self) -> usize {
        self.buf.len().saturating_sub(1)
    }

    /// Appends `text`; any byte that does not fit sets the overflow flag.
    fn text(&mut self, text: &str) {
        let limit = self.limit();
        if self.len >= limit {
            self.overflow = true;
            return;
        }
        let mut w = &mut self.buf[self.len..limit];
        let before = w.len();
        match w.write_all(text.as_bytes()) {
            Ok(()) => self.len += before - w.len(),
            Err(_) => self.overflow = true,
        }
    }

    /// Appends ` key=value`; `UNSET` fields are omitted (not applicable).
    fn opt(&mut self, key: &str, value: u64) {
        if value == UNSET {
            return;
        }
        let limit = self.limit();
        if self.len >= limit {
            self.overflow = true;
            return;
        }
        let mut w = &mut self.buf[self.len..limit];
        let before = w.len();
        match write!(w, " {key}={value}") {
            Ok(()) => self.len += before - w.len(),
            Err(_) => self.overflow = true,
        }
    }

    /// Appends ` key=<signed value>` (used for `errno`).
    fn signed(&mut self, key: &str, value: i64) {
        let limit = self.limit();
        if self.len >= limit {
            self.overflow = true;
            return;
        }
        let mut w = &mut self.buf[self.len..limit];
        let before = w.len();
        match write!(w, " {key}={value}") {
            Ok(()) => self.len += before - w.len(),
            Err(_) => self.overflow = true,
        }
    }

    /// `instance=<pid>.<start_boot_ms>` or `<pid>.unknown`.
    fn instance(&mut self, pid: u32, start: Option<u64>) {
        let limit = self.limit();
        if self.len >= limit {
            self.overflow = true;
            return;
        }
        let mut w = &mut self.buf[self.len..limit];
        let before = w.len();
        let res = match start {
            Some(ms) => write!(w, " instance={pid}.{ms}"),
            None => write!(w, " instance={pid}.unknown"),
        };
        match res {
            Ok(()) => self.len += before - w.len(),
            Err(_) => self.overflow = true,
        }
    }

    /// Terminates the record and returns its length, or `0` if it overflowed.
    fn finish(self) -> usize {
        if self.overflow {
            return 0;
        }
        let at = self.len.min(self.limit());
        self.buf[at] = b'\n';
        at + 1
    }
}

/// Formats one event into `out` and returns the byte count, newline included.
///
/// The effective capacity is `min(out.len(), `[`MAX_LINE_BYTES`]`)`; a slice
/// larger than the contract bound is clamped, never overrun. Returns `0` when
/// the complete record does not fit, in which case the caller must not write
/// the buffer: no partial or malformed record is ever produced.
pub fn format_line(ev: &Event, inst: &Instance, out: &mut [u8]) -> usize {
    let cap = out.len().min(MAX_LINE_BYTES);
    if cap < 2 {
        return 0;
    }
    let mut l = Line::new(&mut out[..cap]);
    l.text("USBTV_OBS");
    l.opt("version", super::SCHEMA_VERSION);
    l.text(" component=");
    l.text(ev.component.as_str());
    l.text(" event=");
    l.text(ev.kind.as_str());
    l.opt("pid", u64::from(inst.pid));
    l.instance(inst.pid, inst.start_ms);
    l.opt("event_seq", ev.event_seq);
    l.text(" clock=");
    l.text(ev.clock.as_str());
    if ev.clock == ClockState::Boottime && ev.boot_ms != UNSET {
        l.opt("boot_ms", ev.boot_ms);
    } else {
        l.text(" boot_ms=unknown");
    }
    l.opt("precision_ms", 1);
    l.text(" reason=");
    l.text(ev.reason.as_str());
    l.opt("transport_request_unique", ev.req_unique);
    l.opt("nodeid", ev.nodeid);
    l.opt("fh", ev.fh);
    l.opt("anchor_epoch", ev.anchor_epoch);
    l.opt("file_id", ev.file_id);
    l.opt("foff", ev.foff);
    l.opt("len", ev.len);
    l.opt("mapped_spos", ev.mapped_spos);
    l.opt("s_write", ev.s_write);
    l.opt("ring_old", ev.ring_old);
    l.opt("real_bytes", ev.real_bytes);
    l.opt("null_bytes", ev.null_bytes);
    l.opt("wait_ms", ev.wait_ms);
    if ev.errno != UNSET {
        l.signed("errno", ev.errno as i64);
    }
    l.opt("dropped", ev.dropped);
    l.finish()
}
