//! Output sinks for the observation worker (TASK-003, corrected in TASK-003-R2).
//!
//! Sinks are only ever called from the single writer thread; producers never
//! reach them. Every sink is bounded: a slow or unavailable output can only
//! produce losses, never backpressure or unbounded storage.

use std::io::{self, Write};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::Mutex;

/// Output side of the worker. Implementations must not be called on producers.
pub trait Sink: Send {
    fn write_line(&mut self, line: &[u8]) -> io::Result<()>;
}

/// Existing process stderr stream.
pub struct StderrSink;

impl Sink for StderrSink {
    fn write_line(&mut self, line: &[u8]) -> io::Result<()> {
        let mut err = io::stderr();
        err.write_all(line)?;
        err.flush()
    }
}

/// Default line cap for [`MemorySink`]: far above any test need, still finite.
pub const MEMORY_SINK_LINES: usize = 4096;
/// Default byte cap for [`MemorySink`] (2 MiB of formatted lines).
pub const MEMORY_SINK_BYTES: usize = 2 * 1024 * 1024;

/// Bounded in-memory sink for tests and harnesses.
///
/// Storage is capped by both line count and total bytes. When a cap is reached
/// the sink returns an error (counted as a sink error by the worker) and the
/// rejected line is dropped; it is never stored partially.
pub struct MemorySink {
    lines: Mutex<Vec<Vec<u8>>>,
    bytes: AtomicUsize,
    max_lines: usize,
    max_bytes: usize,
    full: AtomicU64,
    fail: AtomicBool,
}

impl MemorySink {
    pub fn new() -> Self {
        Self::with_limits(MEMORY_SINK_LINES, MEMORY_SINK_BYTES)
    }

    /// Sink with explicit caps (documented bounds for sustained-output tests).
    pub fn with_limits(max_lines: usize, max_bytes: usize) -> Self {
        Self {
            lines: Mutex::new(Vec::new()),
            bytes: AtomicUsize::new(0),
            max_lines,
            max_bytes,
            full: AtomicU64::new(0),
            fail: AtomicBool::new(false),
        }
    }

    pub fn fail_writes(&self, fail: bool) {
        self.fail.store(fail, Ordering::SeqCst);
    }

    pub fn len(&self) -> usize {
        self.lines.lock().unwrap_or_else(|e| e.into_inner()).len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Bytes currently stored.
    pub fn bytes(&self) -> usize {
        self.bytes.load(Ordering::Relaxed)
    }

    /// Lines rejected because a cap was reached.
    pub fn full(&self) -> u64 {
        self.full.load(Ordering::Relaxed)
    }

    /// Stored caps, for assertions.
    pub fn limits(&self) -> (usize, usize) {
        (self.max_lines, self.max_bytes)
    }

    fn record(&self, line: &[u8]) -> io::Result<()> {
        if self.fail.load(Ordering::SeqCst) {
            return Err(io::Error::new(io::ErrorKind::BrokenPipe, "sink failed"));
        }
        let mut lines = self.lines.lock().unwrap_or_else(|e| e.into_inner());
        if lines.len() >= self.max_lines
            || self.bytes.load(Ordering::Relaxed) + line.len() > self.max_bytes
        {
            self.full.fetch_add(1, Ordering::Relaxed);
            return Err(io::Error::new(io::ErrorKind::WriteZero, "memory sink full"));
        }
        self.bytes.fetch_add(line.len(), Ordering::Relaxed);
        lines.push(line.to_vec());
        Ok(())
    }

    pub fn lines(&self) -> Vec<String> {
        self.lines
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .map(|l| String::from_utf8_lossy(l).into_owned())
            .collect()
    }
}

impl Default for MemorySink {
    fn default() -> Self {
        Self::new()
    }
}

impl Sink for MemorySink {
    fn write_line(&mut self, line: &[u8]) -> io::Result<()> {
        self.record(line)
    }
}

impl Sink for std::sync::Arc<MemorySink> {
    fn write_line(&mut self, line: &[u8]) -> io::Result<()> {
        self.record(line)
    }
}
