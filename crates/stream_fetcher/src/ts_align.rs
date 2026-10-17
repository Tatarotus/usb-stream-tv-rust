//! MPEG-TS 188-byte alignment and resync, independent of any I/O.
//!
//! Behaviour mirrors `feed_ts_bytes()` from the C `stream_fetcher.c`:
//! - contiguous runs of packets starting with 0x47 are handed to the sink as a
//!   single slice (the sink is responsible for splitting into <= PIPE_BUF writes);
//! - on loss of sync, scan for the next 0x47 that is confirmed by another 0x47
//!   exactly 188 bytes later (or that sits too close to the end to confirm);
//! - if no candidate exists, drop whole 188-byte multiples and keep the tail
//!   for the next call.

pub const TS_PKT_SZ: usize = 188;
const SYNC: u8 = 0x47;
/// Upper bound of bytes appended to the accumulator per processing step.
const MAX_PIECE: usize = 64 * 1024;

/// Returned by a sink when it cannot accept data any more (reader gone,
/// backpressure timeout, shutdown...). The caller is expected to `reset()`.
#[derive(Debug)]
pub struct SinkError;

pub trait Sink {
    /// `run` is a non-empty multiple of 188 bytes, every packet starting with 0x47.
    fn write_run(&mut self, run: &[u8]) -> Result<(), SinkError>;
}

pub struct TsAligner {
    acc: Vec<u8>,
    pub total_valid_bytes: u64,
    pub total_resync_drops: u64,
}

impl TsAligner {
    pub fn new() -> Self {
        Self {
            acc: Vec::with_capacity(MAX_PIECE + 4096),
            total_valid_bytes: 0,
            total_resync_drops: 0,
        }
    }

    /// Discard any partial packet (new connection / new FIFO).
    pub fn reset(&mut self) {
        self.acc.clear();
    }

    pub fn feed<S: Sink>(&mut self, data: &[u8], sink: &mut S) -> Result<(), SinkError> {
        for piece in data.chunks(MAX_PIECE) {
            self.acc.extend_from_slice(piece);
            self.process(sink)?;
        }
        Ok(())
    }

    fn process<S: Sink>(&mut self, sink: &mut S) -> Result<(), SinkError> {
        let mut p = 0usize;
        while p + TS_PKT_SZ <= self.acc.len() {
            if self.acc[p] == SYNC {
                let mut end = p;
                while end + TS_PKT_SZ <= self.acc.len() && self.acc[end] == SYNC {
                    end += TS_PKT_SZ;
                }
                sink.write_run(&self.acc[p..end])?;
                self.total_valid_bytes += (end - p) as u64;
                p = end;
            } else {
                match find_resync(&self.acc, p + 1) {
                    Some(s) => {
                        let dropped = s - p;
                        self.total_resync_drops += dropped as u64;
                        eprintln!("[!] Resynced TS stream: dropped {} misaligned bytes", dropped);
                        p = s;
                    }
                    None => {
                        let remain = self.acc.len() - p;
                        let dropped = remain - (remain % TS_PKT_SZ);
                        self.total_resync_drops += dropped as u64;
                        p += dropped;
                        break;
                    }
                }
            }
        }
        self.acc.drain(..p);
        Ok(())
    }
}

fn find_resync(buf: &[u8], from: usize) -> Option<usize> {
    for s in from..buf.len() {
        if buf[s] == SYNC {
            if s + TS_PKT_SZ < buf.len() {
                if buf[s + TS_PKT_SZ] == SYNC {
                    return Some(s);
                }
            } else {
                return Some(s);
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    struct VecSink(Vec<u8>);
    impl Sink for VecSink {
        fn write_run(&mut self, run: &[u8]) -> Result<(), SinkError> {
            assert!(!run.is_empty() && run.len() % TS_PKT_SZ == 0);
            assert!(run.chunks(TS_PKT_SZ).all(|p| p[0] == SYNC));
            self.0.extend_from_slice(run);
            Ok(())
        }
    }

    struct FailSink;
    impl Sink for FailSink {
        fn write_run(&mut self, _: &[u8]) -> Result<(), SinkError> {
            Err(SinkError)
        }
    }

    fn packets(n: usize) -> Vec<u8> {
        let mut v = Vec::new();
        for i in 0..n {
            let mut p = vec![(i % 251) as u8; TS_PKT_SZ];
            p[0] = SYNC;
            v.extend_from_slice(&p);
        }
        v
    }

    #[test]
    fn aligned_stream_passes_through_with_odd_split_sizes() {
        let data = packets(500);
        let mut a = TsAligner::new();
        let mut s = VecSink(Vec::new());
        for piece in data.chunks(1000) {
            a.feed(piece, &mut s).unwrap();
        }
        assert_eq!(s.0, data);
        assert_eq!(a.total_resync_drops, 0);
    }

    #[test]
    fn one_byte_at_a_time() {
        let data = packets(5);
        let mut a = TsAligner::new();
        let mut s = VecSink(Vec::new());
        for b in &data {
            a.feed(std::slice::from_ref(b), &mut s).unwrap();
        }
        assert_eq!(s.0, data);
    }

    #[test]
    fn garbage_prefix_is_dropped_and_stream_resyncs() {
        let good = packets(10);
        let mut data = vec![0x00u8, 0x11, 0x22, 0x33, 0x44];
        data.extend_from_slice(&good);
        let mut a = TsAligner::new();
        let mut s = VecSink(Vec::new());
        a.feed(&data, &mut s).unwrap();
        assert_eq!(s.0, good);
        assert_eq!(a.total_resync_drops, 5);
    }

    #[test]
    fn sink_error_is_propagated() {
        let mut a = TsAligner::new();
        assert!(a.feed(&packets(3), &mut FailSink).is_err());
    }

    #[test]
    fn partial_tail_is_kept_until_completed() {
        let data = packets(2);
        let mut a = TsAligner::new();
        let mut s = VecSink(Vec::new());
        a.feed(&data[..300], &mut s).unwrap();
        assert_eq!(s.0.len(), TS_PKT_SZ);
        a.feed(&data[300..], &mut s).unwrap();
        assert_eq!(s.0, data);
    }
}
