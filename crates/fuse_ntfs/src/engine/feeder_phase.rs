//! TASK-004 Phase B — feeder-side MPEG-TS phase reacquisition.
//!
//! A FIFO read may begin at any byte of a 188-byte packet, and a flush may
//! restart the producer at a different phase. Publishing raw bytes into the
//! ring makes every ring offset depend on that phase, while the engine's
//! anchor/leadback logic is defined on the absolute 188 grid.
//!
//! `FeederPhase` is the publication gate: it discards the leading partial
//! packet, confirms the sync grid with several consecutive packets, and emits
//! only complete 188-byte packets. The ring therefore always starts on a
//! packet boundary, and `snap188`/`leadback_guard` keep their existing
//! semantics. The writer is still unthrottled: `push` never sleeps and only
//! copies bytes that are about to be published.

/// MPEG-TS packet size.
const PKT: usize = 188;
/// Consecutive headers required to accept a sync grid. Payload data can contain
/// isolated `0x47` bytes (and even short 188-spaced runs); five points make a
/// false lock unlikely without delaying the feed by more than 940 bytes.
const CONFIRM: usize = 5;
/// Bytes kept while unsynchronized so a boundary split across reads is not
/// lost.
const KEEP: usize = CONFIRM * PKT - 1;

/// Stateful FIFO → ring alignment gate.
pub struct FeederPhase {
    pending: Vec<u8>,
    aligned: bool,
}

impl Default for FeederPhase {
    fn default() -> Self {
        Self::new()
    }
}

impl FeederPhase {
    pub fn new() -> Self {
        Self {
            pending: Vec::with_capacity(CONFIRM * PKT),
            aligned: false,
        }
    }

    /// Forgets the current phase. Called after a flush, EOF/reopen and at the
    /// start of a new FIFO generation.
    pub fn reset(&mut self) {
        self.pending.clear();
        self.aligned = false;
    }

    pub fn is_aligned(&self) -> bool {
        self.aligned
    }

    /// Accepts arbitrary FIFO bytes and returns zero or more complete,
    /// packet-aligned bytes ready for `Ring::write_chunk`. Bytes before the
    /// confirmed sync grid are discarded; bytes after it are preserved.
    pub fn push(&mut self, data: &[u8]) -> Vec<u8> {
        if data.is_empty() {
            return Vec::new();
        }
        self.pending.extend_from_slice(data);
        let mut out = Vec::new();

        loop {
            if !self.aligned {
                match find_sync(&self.pending) {
                    Some(i) => {
                        self.pending.drain(..i);
                        self.aligned = true;
                    }
                    None => {
                        if self.pending.len() > KEEP {
                            let start = self.pending.len() - KEEP;
                            self.pending.drain(..start);
                        }
                        break;
                    }
                }
            }

            let mut i = 0usize;
            while i + PKT <= self.pending.len() {
                if self.pending[i] != 0x47 {
                    // Upstream corruption: drop the broken prefix and
                    // re-confirm a sync grid from this byte.
                    self.aligned = false;
                    self.pending.drain(..i);
                    break;
                }
                out.extend_from_slice(&self.pending[i..i + PKT]);
                i += PKT;
            }
            if self.aligned {
                self.pending.drain(..i);
                break;
            }
        }
        out
    }
}

fn header_ok(buf: &[u8], off: usize) -> bool {
    off + PKT <= buf.len() && buf[off] == 0x47 && (buf[off + 1] & 0x80) == 0
}

fn find_sync(buf: &[u8]) -> Option<usize> {
    if buf.len() < CONFIRM * PKT {
        return None;
    }
    (0..=buf.len() - CONFIRM * PKT).find(|&i| (0..CONFIRM).all(|k| header_ok(buf, i + k * PKT)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mk_stream(n: usize) -> Vec<u8> {
        let mut v = Vec::with_capacity(n * PKT);
        for i in 0..n {
            let mut p = [0u8; PKT];
            p[0] = 0x47;
            p[1] = 0x41; // PUSI, no TEI
            p[2] = (i % 0x1F) as u8;
            p[3] = 0x10 | (i as u8 & 0x0F);
            for (j, b) in p.iter_mut().enumerate().skip(4) {
                // Dense decoys at non-packet offsets.
                *b = if (i + j) % 3 == 0 { 0x47 } else { 0x5A };
            }
            v.extend_from_slice(&p);
        }
        v
    }

    fn assert_suffix(stream: &[u8], out: &[u8]) {
        assert!(!out.is_empty(), "aligner produced no bytes");
        assert_eq!(out.len() % PKT, 0, "output is not a packet multiple");
        assert!(out.len() <= stream.len());
        let start = stream.len() - out.len();
        assert_eq!(start % PKT, 0, "output is not a stream suffix");
        assert_eq!(&stream[start..], out, "valid packet bytes were altered");
    }

    fn feed_in_chunks(feeder: &mut FeederPhase, data: &[u8], size: usize) -> Vec<u8> {
        let mut out = Vec::new();
        for chunk in data.chunks(size) {
            out.extend_from_slice(&feeder.push(chunk));
        }
        out
    }

    #[test]
    fn all_phases_with_large_chunks() {
        let stream = mk_stream(700);
        for phase in 0..PKT {
            let mut shifted = vec![0xAAu8; phase];
            shifted.extend_from_slice(&stream);
            for size in [65_536usize, 16_384, 131_072] {
                let mut f = FeederPhase::new();
                let out = feed_in_chunks(&mut f, &shifted, size);
                assert_suffix(&stream, &out);
                assert!(f.is_aligned());
            }
        }
    }

    #[test]
    fn small_chunks_and_boundaries() {
        let stream = mk_stream(24);
        for &phase in &[0usize, 1, 47, 93, 187] {
            let mut shifted = vec![0xAAu8; phase];
            shifted.extend_from_slice(&stream);
            for size in [1usize, 187, 188, 189] {
                let mut f = FeederPhase::new();
                let out = feed_in_chunks(&mut f, &shifted, size);
                assert_suffix(&stream, &out);
            }
        }
    }

    #[test]
    fn reset_reacquires_a_new_phase() {
        let a = mk_stream(40);
        let b = mk_stream(40);
        let mut f = FeederPhase::new();
        let mut shifted = vec![0xAAu8; 123];
        shifted.extend_from_slice(&a);
        let out_a = feed_in_chunks(&mut f, &shifted, 7);
        assert_suffix(&a, &out_a);

        f.reset();
        assert!(!f.is_aligned());
        let mut shifted = vec![0xAAu8; 17];
        shifted.extend_from_slice(&b);
        let out_b = feed_in_chunks(&mut f, &shifted, 13);
        assert_suffix(&b, &out_b);
    }

    #[test]
    fn reset_after_partial_old_source_drops_old_bytes() {
        // Simulates a poll/read error mid-packet: the caller resets the gate
        // before reopening the FIFO, and the new producer starts at a different
        // phase. No byte from the old source may reach the new output.
        let old = mk_stream(12);
        let mut f = FeederPhase::new();
        let cut = old.len() / 2 + 37;
        let out_old = f.push(&old[..cut]);
        assert!(!out_old.is_empty());
        assert!(f.is_aligned());

        f.reset();
        let new = mk_stream(40);
        let mut shifted = vec![0xAAu8; 91];
        shifted.extend_from_slice(&new);
        let out = feed_in_chunks(&mut f, &shifted, 11);
        assert_suffix(&new, &out);
    }

    #[test]
    fn short_fake_run_is_not_accepted() {
        // Three 0x47 bytes at 188 spacing, but not five: a naive 3-point
        // scanner would lock here. The real stream starts later.
        let stream = mk_stream(40);
        let mut fake = vec![0xAAu8; 700];
        fake[0] = 0x47;
        fake[PKT] = 0x47;
        fake[2 * PKT] = 0x47;
        let mut input = fake.clone();
        input.extend_from_slice(&stream);
        let mut f = FeederPhase::new();
        let out = f.push(&input);
        assert_suffix(&stream, &out);
    }

    #[test]
    fn empty_input_is_a_noop() {
        let mut f = FeederPhase::new();
        assert!(f.push(&[]).is_empty());
        assert!(!f.is_aligned());
        f.reset();
        assert!(f.push(&[]).is_empty());
    }
}
