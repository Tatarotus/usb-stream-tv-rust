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
/// Number of consecutive 0x47 sync bytes required to validate MPEG-TS framing.
/// A 3-point check reduces false-latch probability in video payload entropy to 1 in 1.67e7.
const SYNC_CONFIRM_PKTS: usize = 3;
const MIN_RESYNC_SPAN: usize = (SYNC_CONFIRM_PKTS - 1) * TS_PKT_SZ + 1; // 377 bytes
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
                        // Retain up to MIN_RESYNC_SPAN - 1 (376 bytes) so a valid 3-packet sequence
                        // split across chunk boundaries can be verified on the next feed().
                        let keep = remain.min(MIN_RESYNC_SPAN - 1);
                        let dropped = remain - keep;
                        self.total_resync_drops += dropped as u64;
                        p += dropped;
                        break;
                    }
                }
            }
        }
        let new_len = self.acc.len() - p;
        self.acc.copy_within(p.., 0);
        self.acc.truncate(new_len);
        Ok(())
    }
}

fn find_resync(buf: &[u8], from: usize) -> Option<usize> {
    if buf.len() < from + MIN_RESYNC_SPAN {
        return None;
    }
    let limit = buf.len() - (SYNC_CONFIRM_PKTS - 1) * TS_PKT_SZ;
    for s in from..limit {
        if buf[s] == SYNC
            && buf[s + TS_PKT_SZ] == SYNC
            && buf[s + 2 * TS_PKT_SZ] == SYNC
        {
            return Some(s);
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

    #[test]
    fn false_two_point_sync_is_rejected_until_three_points_confirmed() {
        // Construct garbage with 2 false 0x47 bytes separated by 188 bytes, but 3rd is NOT 0x47.
        // Followed by 5 valid packets.
        let good = packets(5);
        let mut data = vec![0xAAu8; 188 * 2];
        data[10] = SYNC;
        data[10 + TS_PKT_SZ] = SYNC;
        // data[10 + 2 * TS_PKT_SZ] does not exist or is not 0x47
        data.extend_from_slice(&good);

        let mut a = TsAligner::new();
        let mut s = VecSink(Vec::new());
        a.feed(&data, &mut s).unwrap();

        // Must output only the 5 valid packets, rejecting the false 2-point pair
        assert_eq!(s.0, good);
        assert_eq!(a.total_resync_drops, (188 * 2) as u64);
    }

    #[test]
    fn unconfirmed_tail_does_not_falsely_latch_packet() {
        // Feed 100 bytes of garbage ending with 0x47.
        // Must NOT emit anything and NOT panic.
        let mut data = vec![0x55u8; 100];
        data[99] = SYNC;

        let mut a = TsAligner::new();
        let mut s = VecSink(Vec::new());
        a.feed(&data, &mut s).unwrap();
        assert_eq!(s.0.len(), 0);

        // Now feed valid packets; aligner resyncs and outputs them cleanly
        let good = packets(4);
        a.feed(&good, &mut s).unwrap();
        assert_eq!(s.0, good);
    }
}
