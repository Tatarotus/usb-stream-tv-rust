//! TASK-004 Phase B — in-crate pause seam used by the Phase B unit tests.
//!
//! This module is only compiled for unit tests (`--lib`), so it has zero cost
//! and zero surface in production. It lets a test park a reader between
//! transformer calibration and anchor publication, which is not observable
//! from an external integration-test crate.

use std::sync::{Condvar, Mutex, OnceLock};

struct Seam {
    state: Mutex<u8>,
    cv: Condvar,
}

static SEAM: OnceLock<Seam> = OnceLock::new();

fn seam() -> &'static Seam {
    SEAM.get_or_init(|| Seam {
        state: Mutex::new(0),
        cv: Condvar::new(),
    })
}

/// Arms the seam. Call only from a serialized unit test.
pub(super) fn arm() {
    *seam().state.lock().unwrap_or_else(|e| e.into_inner()) = 1;
}

/// Blocks until a reader has reached the seam.
pub(super) fn wait_reached() {
    let s = seam();
    let mut g = s.state.lock().unwrap_or_else(|e| e.into_inner());
    while *g < 2 {
        g = s.cv.wait(g).unwrap_or_else(|e| e.into_inner());
    }
}

/// Releases a parked reader.
pub(super) fn release() {
    let s = seam();
    *s.state.lock().unwrap_or_else(|e| e.into_inner()) = 3;
    s.cv.notify_all();
}

/// Called by `flush()` after invalidating the anchor and before clearing the
/// ring, while `xform+anchor` are held. No-op unless the seam is armed.
pub(super) fn park_flush_after_anchor() {
    park();
}

/// Called by the engine after pacing and before the delivery commit. No-op
/// unless the seam is armed.
pub(super) fn park_before_commit() {
    park();
}

/// Called by the engine after the new sequence is computed and before the
/// atomic transformer-update + anchor-publication section. No-op unless the
/// seam is armed.
pub(super) fn park_before_calibration() {
    park();
}

fn park() {
    let s = seam();
    let mut g = s.state.lock().unwrap_or_else(|e| e.into_inner());
    if *g != 1 {
        return;
    }
    *g = 2;
    s.cv.notify_all();
    while *g != 3 {
        g = s.cv.wait(g).unwrap_or_else(|e| e.into_inner());
    }
    *g = 0;
}
