//! Observation helpers for the FUSE session (TASK-003, corrected in R4).
//!
//! Numeric-only records: no formatting, no sink IO and no blocking. Every
//! reporter captures the values it needs *before* calling the observer, so a
//! timestamp or `errno` read by the observer can never replace the transport's
//! own result.

use super::FuseSession;
use crate::observability::{Component, Event, EventKind, Reason};
use crate::vfs::fuse_abi::{fuse_in_header, fuse_read_in};

impl FuseSession {
    /// Numeric FUSE event: no formatting, no sink IO, never blocks.
    pub(super) fn report(
        &self,
        kind: EventKind,
        why: Reason,
        inh: &fuse_in_header,
        fh: u64,
        len: u64,
    ) {
        let ev = Event::new(Component::Fuse, kind)
            .reason(why)
            .nodeid(inh.nodeid)
            .req_unique(inh.unique)
            .fh(fh)
            .len(len);
        self.server.obs.emit(ev);
    }

    /// READ result: requested size, offset, request handle and request
    /// correlation, plus the bytes actually placed in the reply payload.
    pub(super) fn report_read_result(
        &self,
        inh: &fuse_in_header,
        rin: &fuse_read_in,
        returned: usize,
    ) {
        let requested = u64::from(rin.size);
        let why = if (returned as u64) < requested {
            Reason::ShortWrite
        } else {
            Reason::Sampled
        };
        let ev = Event::new(Component::Fuse, EventKind::FuseReadResult)
            .reason(why)
            .nodeid(inh.nodeid)
            .req_unique(inh.unique)
            .fh(rin.fh)
            .foff(rin.offset)
            .len(requested)
            .real_bytes(returned as u64);
        self.server.obs.emit(ev);
    }

    /// Device read failure with the transport errno.
    pub(super) fn report_device_error(&self, errno: i32) {
        let ev = Event::new(Component::Fuse, EventKind::FuseDeviceReadError)
            .reason(Reason::ReadError)
            .errno(errno);
        self.server.obs.emit(ev);
    }

    /// Reply write failure/short write: errno captured before any observer call,
    /// plus expected versus actual length and the originating request.
    pub(super) fn report_write_error(
        &self,
        inh: &fuse_in_header,
        written: isize,
        expected: usize,
        errno: i32,
    ) {
        let why = if written < 0 {
            Reason::WriteError
        } else {
            Reason::ShortWrite
        };
        let actual = if written < 0 { 0 } else { written as u64 };
        let ev = Event::new(Component::Fuse, EventKind::FuseReplyWriteResult)
            .reason(why)
            .nodeid(inh.nodeid)
            .req_unique(inh.unique)
            .len(expected as u64)
            .real_bytes(actual)
            .errno(errno);
        self.server.obs.emit(ev);
    }

    /// Loop termination, including the unclean error return.
    pub(super) fn report_loop_exit(&self, why: Reason) {
        let ev = Event::new(Component::Fuse, EventKind::FuseLoopExit).reason(why);
        self.server.obs.emit(ev);
    }
}
