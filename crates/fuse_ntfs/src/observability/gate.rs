//! Admission budget for observation lines (TASK-003, corrected in TASK-003-R).
//!
//! One bounded token bucket per class plus one global budget. The bucket keeps
//! the refill timestamp in full-width boot milliseconds: the previous packed
//! 32-bit timestamp truncated it, so every call after 2^32 ms (~49.7 days) of
//! uptime compared a truncated `last` against a full `now`, saw a huge elapsed
//! interval and refilled the bucket on the spot.

use std::sync::atomic::{AtomicU64, Ordering};

/// Rate class of an event kind; `Control` events are not class-gated.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Class {
    Progress,
    Idle,
    Summary,
    Control,
}

/// Fixed-size token bucket: one event costs 1000 milli-tokens.
#[derive(Debug)]
pub(crate) struct Gate {
    /// Milli-tokens available now (saturates at `capacity_milli`).
    tokens: AtomicU64,
    /// Boot milliseconds of the last refill (full `u64`, never truncated).
    last_ms: AtomicU64,
    /// Bucket capacity in milli-tokens.
    capacity_milli: u64,
    /// Refill in milli-tokens per millisecond.
    refill_per_ms: u64,
}

impl Gate {
    /// Bucket that starts full with `burst` events of capacity and refills at
    /// `rate_per_s` events per second.
    pub(crate) fn new(rate_per_s: u32, burst: u32) -> Self {
        let capacity_milli = u64::from(burst) * 1000;
        Self {
            tokens: AtomicU64::new(capacity_milli),
            last_ms: AtomicU64::new(0),
            capacity_milli,
            refill_per_ms: u64::from(rate_per_s),
        }
    }

    /// Lock-free admission of one event.
    ///
    /// A `None` clock (unavailable) refills nothing: the bucket only drains, so
    /// a broken clock cannot flood the queue. Time is compared in full `u64`
    /// milliseconds and a call in the same millisecond never refills.
    pub(crate) fn allow(&self, now: Option<u64>) -> bool {
        if let Some(now) = now {
            self.refill(now);
        }
        self.take()
    }

    /// Claims the window `(last, now]` with a CAS so concurrent callers credit
    /// each elapsed millisecond at most once; `now <= last` never refills.
    fn refill(&self, now: u64) {
        let mut last = self.last_ms.load(Ordering::Relaxed);
        while now > last {
            match self.last_ms.compare_exchange_weak(
                last,
                now,
                Ordering::Relaxed,
                Ordering::Relaxed,
            ) {
                Ok(_) => {
                    self.credit((now - last).saturating_mul(self.refill_per_ms));
                    return;
                }
                Err(actual) => last = actual,
            }
        }
    }

    /// Adds `gained` milli-tokens, saturating at the bucket capacity.
    fn credit(&self, gained: u64) {
        let mut cur = self.tokens.load(Ordering::Relaxed);
        loop {
            let next = cur.saturating_add(gained).min(self.capacity_milli);
            if next == cur {
                return;
            }
            match self
                .tokens
                .compare_exchange_weak(cur, next, Ordering::Relaxed, Ordering::Relaxed)
            {
                Ok(_) => return,
                Err(actual) => cur = actual,
            }
        }
    }

    /// Removes one event worth of tokens, or reports the bucket as empty.
    fn take(&self) -> bool {
        let mut cur = self.tokens.load(Ordering::Relaxed);
        loop {
            if cur < 1000 {
                return false;
            }
            match self.tokens.compare_exchange_weak(
                cur,
                cur - 1000,
                Ordering::Relaxed,
                Ordering::Relaxed,
            ) {
                Ok(_) => return true,
                Err(actual) => cur = actual,
            }
        }
    }
}
