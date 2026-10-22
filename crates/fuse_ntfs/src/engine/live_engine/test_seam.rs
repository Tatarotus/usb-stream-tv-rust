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

/// Second, INDEPENDENT seam: parked inside `commit_delivery`'s `xform`+`anchor`
/// critical section, right after the generation check and before the return
/// intent is armed. It must not share the state of the first seam, otherwise
/// `park_before_commit()` (which runs earlier, outside the lock) would swallow
/// the park. This is the only point where a test can hold the anchor, with a
/// valid generation, against a concurrent `on_open()`.
pub(super) fn arm_commit_hold() {
    *commit_hold().state.lock().unwrap_or_else(|e| e.into_inner()) = 1;
}

pub(super) fn wait_commit_hold_reached() {
    let s = commit_hold();
    let mut g = s.state.lock().unwrap_or_else(|e| e.into_inner());
    while *g < 2 {
        g = s.cv.wait(g).unwrap_or_else(|e| e.into_inner());
    }
}

pub(super) fn release_commit_hold() {
    let s = commit_hold();
    *s.state.lock().unwrap_or_else(|e| e.into_inner()) = 3;
    s.cv.notify_all();
}

fn commit_hold() -> &'static Seam {
    static HOLD: OnceLock<Seam> = OnceLock::new();
    HOLD.get_or_init(|| Seam {
        state: Mutex::new(0),
        cv: Condvar::new(),
    })
}

/// Third, independent seam: parked in `on_open()` immediately after the
/// `anchor` lock is acquired and BEFORE the return intent is cleared and the
/// generation is bumped. It lets a test observe, deterministically, whether an
/// arming commit landed before or after the reset's critical section.
pub(super) fn arm_open_hold() {
    *open_hold().state.lock().unwrap_or_else(|e| e.into_inner()) = 1;
}

pub(super) fn wait_open_hold_reached() {
    let s = open_hold();
    let mut g = s.state.lock().unwrap_or_else(|e| e.into_inner());
    while *g < 2 {
        g = s.cv.wait(g).unwrap_or_else(|e| e.into_inner());
    }
}

pub(super) fn release_open_hold() {
    let s = open_hold();
    *s.state.lock().unwrap_or_else(|e| e.into_inner()) = 3;
    s.cv.notify_all();
}

fn open_hold() -> &'static Seam {
    static HOLD: OnceLock<Seam> = OnceLock::new();
    HOLD.get_or_init(|| Seam {
        state: Mutex::new(0),
        cv: Condvar::new(),
    })
}

pub(super) fn park_in_open_after_anchor_lock() {
    let s = open_hold();
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

/// Fourth, independent seam: parked immediately after `serve_live` snapshots
/// the anchor generation and before any shared state (anchor mapping, intent,
/// state machine) is read or written.
pub(super) fn arm_after_snapshot() {
    *after_snapshot().state.lock().unwrap_or_else(|e| e.into_inner()) = 1;
}

pub(super) fn wait_after_snapshot_reached() {
    let s = after_snapshot();
    let mut g = s.state.lock().unwrap_or_else(|e| e.into_inner());
    while *g < 2 {
        g = s.cv.wait(g).unwrap_or_else(|e| e.into_inner());
    }
}

pub(super) fn release_after_snapshot() {
    let s = after_snapshot();
    *s.state.lock().unwrap_or_else(|e| e.into_inner()) = 3;
    s.cv.notify_all();
}

fn after_snapshot() -> &'static Seam {
    static HOLD: OnceLock<Seam> = OnceLock::new();
    HOLD.get_or_init(|| Seam {
        state: Mutex::new(0),
        cv: Condvar::new(),
    })
}

pub(super) fn park_after_gen_snapshot() {
    let s = after_snapshot();
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

pub(super) fn park_in_commit_after_gen_check() {
    let s = commit_hold();
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
