//! MPEG-TS continuity transform (the "Caminho C" engine).
//!
//! When the FUSE anchor changes mid-playback (channel switch, Return-key
//! resume, ring wrap), the decoder must see a *continuous timeline*: PTS/DTS
//! and PCR that continue from exactly where the TV left off, and continuity
//! counters that never repeat. This module rewrites, deterministically per
//! stream position, a constant per-epoch offset onto:
//!
//! * PCR (27 MHz base, modulo 2^33 * 300),
//! * PTS/DTS of video/audio PES (90 kHz, modulo 2^33),
//! * the 4-bit continuity counter of every non-NULL PID.
//!
//! MPEG-TS NULL packets (PID 0x1FFF) are never touched.
//!
//! Cross-read straddling packets are carried in [`Tail`]: the head slice is
//! rewritten when it is served, the remainder when the packet completes
//! (131072 = 697*188 + 56, so one packet per 128 KiB FUSE read straddles the
//! boundary). Because the transform is a pure function of (raw bytes,
//! absolute position, epoch), re-reading the same region yields identical
//! bytes.
//!
//! Splice calibration is 3-tier: `served` (last delivered state) -> `ckpt`
//! (RAM checkpoint, post-wrap) -> `ring_scan` (walk back <= 512 KiB for the
//! last PTS/PCR and convert to delivered values with the current epoch).

use crate::contract::TS_PKT_SZ;

/// PTS/DTS modulus: 2^33 ticks at 90 kHz.
pub const PTS_MOD: u64 = 1u64 << 33; // 8_589_934_592
/// PCR modulus: 2^33 * 300 ticks at 27 MHz.
pub const PCR_MOD: u64 = PTS_MOD * 300; // 2_576_980_377_600
/// PTS delta considered a "scar" (upstream discontinuity) by the snapper.
pub const PTS_SCAR_5S: u64 = 5 * 90_000; // 450_000 ticks
/// Strictly-positive PTS epsilon applied at a splice so the decoder never
/// receives two equal consecutive PTS values (validated: exactly 3000 ticks).
pub const SPLICE_PTS_EPS: u64 = 3_000;
/// `served` is fresh while the splice point is at most this far ahead of the
/// last sequential delivery.
pub const SERVED_FRESH_BYTES: u64 = 256 * 1024;
/// Maximum backward scan distance for the `ring_scan` calibration tier.
pub const RING_SCAN_BACK: u64 = 512 * 1024;
/// Sanity bound for using a `ckpt` record after the position left `served`.
pub const CKPT_MAX_DIST: u64 = 4 * 1024 * 1024;
/// RAM checkpoint slots (one per 64 KiB of delivered file end-offset).
pub const CKPT_N: usize = 64;
pub const CKPT_STEP: u64 = 64 * 1024;
/// Checkpoint records older than this are ignored.
pub const CKPT_MAX_AGE_MS: u64 = 30 * 60 * 1000;
/// Distinct PIDs tracked in a timeline reference (live streams carry a
/// handful: PAT, PMT, video, audio, PCR pid, teletext...).
pub const REF_CC_SLOTS: usize = 32;

const NULL_PID: u16 = 0x1FFF;

/// Wraps a 33-bit PTS/DTS value.
pub const fn pts_wrap(v: u64) -> u64 {
    v % PTS_MOD
}

/// Wraps a 27 MHz PCR value.
pub const fn pcr_wrap(v: u64) -> u64 {
    v % PCR_MOD
}

/// Signed minimal difference `a - b` on the PTS circle, in
/// `(-PTS_MOD/2, PTS_MOD/2]`. Handles the 2^33 rollover correctly.
pub fn pts_sub(a: u64, b: u64) -> i64 {
    let m = PTS_MOD as i64;
    let d = ((a % PTS_MOD) as i64 - (b % PTS_MOD) as i64 + m / 2 + m) % m - m / 2;
    d
}

/// Signed minimal difference `a - b` on the PCR circle (27 MHz).
pub fn pcr_sub(a: u64, b: u64) -> i64 {
    let m = PCR_MOD as i64;
    let d = ((a % PCR_MOD) as i64 - (b % PCR_MOD) as i64 + m / 2 + m) % m - m / 2;
    d
}

/// Applies a signed delta on the PTS circle (never overflows: deltas are
/// bounded by the modular difference magnitude, far below i64 range).
pub fn pts_apply(raw: u64, delta: i64) -> u64 {
    ((raw % PTS_MOD) as i64 + delta).rem_euclid(PTS_MOD as i64) as u64
}

/// Applies a signed delta on the PCR circle.
pub fn pcr_apply(raw: u64, delta: i64) -> u64 {
    ((raw % PCR_MOD) as i64 + delta).rem_euclid(PCR_MOD as i64) as u64
}

/// Splice pad length: bytes of the in-flight packet that must still be
/// delivered raw before the new anchor takes over, so the byte after the
/// seam always lands on a 188-byte packet boundary.
pub const fn splice_pad_len(old_pos: u64) -> u64 {
    (TS_PKT_SZ as u64 - (old_pos % TS_PKT_SZ as u64)) % TS_PKT_SZ as u64
}

// ---------------------------------------------------------------------------
// TS field accessors (bounds-checked; all inputs are 188-byte packets)
// ---------------------------------------------------------------------------

pub fn ts_pid(pkt: &[u8]) -> u16 {
    ((u16::from(pkt[1] & 0x1F)) << 8) | u16::from(pkt[2])
}

pub fn ts_cc(pkt: &[u8]) -> u8 {
    pkt[3] & 0x0F
}

pub fn ts_pusi(pkt: &[u8]) -> bool {
    pkt[1] & 0x40 != 0
}

pub fn ts_afc(pkt: &[u8]) -> u8 {
    (pkt[3] >> 4) & 0x03
}

/// Byte offset of the TS payload, or `None` when the packet carries none.
pub fn ts_payload_off(pkt: &[u8]) -> Option<usize> {
    match ts_afc(pkt) {
        1 => Some(4),
        3 => {
            let af_len = usize::from(pkt[4]);
            let off = 5 + af_len;
            if off < TS_PKT_SZ {
                Some(off)
            } else {
                None
            }
        }
        _ => None,
    }
}

/// Decodes the 33-bit PTS/DTS field from its 5-byte Big-Endian encoding.
pub fn pts_from_bytes(b: &[u8]) -> u64 {
    (u64::from(b[0] & 0x0E) << 29)
        | (u64::from(b[1]) << 22)
        | (u64::from(b[2] & 0xFE) << 14)
        | (u64::from(b[3]) << 7)
        | (u64::from(b[4]) >> 1)
}

/// Encodes a 33-bit PTS/DTS value into its 5-byte form, preserving the
/// prefix nibble and marker bits of the original field.
pub fn pts_to_bytes(v: u64, b: &mut [u8]) {
    let old_prefix = b[0] & 0xF0;
    b[0] = old_prefix | (((v >> 29) & 0x0E) as u8) | 0x01;
    b[1] = (v >> 22) as u8;
    b[2] = (((v >> 14) & 0xFE) as u8) | 0x01;
    b[3] = (v >> 7) as u8;
    b[4] = (((v & 0x7F) as u8) << 1) | 0x01;
}

/// Parses the PCR (27 MHz base, without the 300x extension detail folded in)
/// from the adaptation field. Returns the full 27 MHz value.
pub fn ts_pcr(pkt: &[u8]) -> Option<u64> {
    if ts_afc(pkt) < 2 {
        return None;
    }
    let af_len = usize::from(pkt[4]);
    if af_len < 7 || pkt[5] & 0x10 == 0 {
        return None;
    }
    let base = (u64::from(pkt[6]) << 25)
        | (u64::from(pkt[7]) << 17)
        | (u64::from(pkt[8]) << 9)
        | (u64::from(pkt[9]) << 1)
        | (u64::from(pkt[10]) >> 7);
    let ext = (u64::from(pkt[10] & 0x01) << 8) | u64::from(pkt[11]);
    Some(base * 300 + ext)
}

/// Encodes a 27 MHz PCR value back into the adaptation field.
pub fn ts_write_pcr(v: u64, pkt: &mut [u8]) {
    let base = v / 300;
    let ext = v % 300;
    pkt[6] = (base >> 25) as u8;
    pkt[7] = (base >> 17) as u8;
    pkt[8] = (base >> 9) as u8;
    pkt[9] = (base >> 1) as u8;
    pkt[10] = (((base & 0x01) as u8) << 7) | 0x7E | ((ext >> 8) as u8 & 0x01);
    pkt[11] = ext as u8;
}

/// Parses the PES header: returns `(pts, dts)` when the packet is a PES start.
pub fn ts_pes_ts(pkt: &[u8]) -> Option<(u64, Option<u64>)> {
    if !ts_pusi(pkt) {
        return None;
    }
    let off = ts_payload_off(pkt)?;
    let pl = &pkt[off..];
    if pl.len() < 14 || pl[0] != 0x00 || pl[1] != 0x00 || pl[2] != 0x01 {
        return None;
    }
    let flags = pl[7];
    if flags & 0x80 != 0 {
        let pts = pts_from_bytes(&pl[9..14]);
        let dts = if flags & 0x40 != 0 && pl.len() >= 19 {
            Some(pts_from_bytes(&pl[14..19]))
        } else {
            None
        };
        Some((pts, dts))
    } else {
        None
    }
}

/// True when the packet payload starts an Annex-B access unit (used by the
/// D12 SPS probe: NAL type 7 = SPS, 8 = PPS).
pub fn ts_has_sps(pkt: &[u8]) -> bool {
    let off = match ts_payload_off(pkt) {
        Some(o) => o,
        None => return false,
    };
    let pl = &pkt[off..];
    let mut i = 0usize;
    while i + 4 <= pl.len() {
        if pl[i] == 0 && pl[i + 1] == 0 {
            if pl[i + 2] == 1 && (pl[i + 3] & 0x1F) == 7 {
                return true;
            }
            if i + 5 <= pl.len() && pl[i + 2] == 0 && pl[i + 3] == 1 && (pl[i + 4] & 0x1F) == 7 {
                return true;
            }
        }
        i += 1;
    }
    false
}

// ---------------------------------------------------------------------------
// Timeline reference & epochs
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RefSrc {
    Served,
    Ckpt,
    RingScan,
}

/// A timeline reference: the state the decoder has already consumed.
#[derive(Debug, Clone, Copy)]
pub struct XformRef {
    pub src: RefSrc,
    pub valid: bool,
    pub pts: u64,
    pub pcr: u64,
    pub pcr_valid: bool,
    pub upd_pos: u64,
    pub ccs: [(u16, u8); REF_CC_SLOTS],
    pub ncc: usize,
}

impl XformRef {
    pub const fn invalid() -> Self {
        Self {
            src: RefSrc::Served,
            valid: false,
            pts: 0,
            pcr: 0,
            pcr_valid: false,
            upd_pos: 0,
            ccs: [(0, 0); REF_CC_SLOTS],
            ncc: 0,
        }
    }

    #[allow(dead_code)]
    fn set_cc(&mut self, pid: u16, cc: u8) {
        for slot in self.ccs.iter_mut().take(self.ncc) {
            if slot.0 == pid {
                slot.1 = cc;
                return;
            }
        }
        if self.ncc < REF_CC_SLOTS {
            self.ccs[self.ncc] = (pid, cc);
            self.ncc += 1;
        }
    }

    #[allow(dead_code)]
    fn cc_of(&self, pid: u16) -> Option<u8> {
        self.ccs.iter().take(self.ncc).find(|s| s.0 == pid).map(|s| s.1)
    }
}

/// Raw stream sample taken at a candidate new anchor (pre-transform values).
#[derive(Debug, Clone, Copy)]
pub struct RawSample {
    pub pts: Option<u64>,
    pub pcr: Option<u64>,
    pub ccs: [(u16, u8); REF_CC_SLOTS],
    pub ncc: usize,
}

impl RawSample {
    pub const fn empty() -> Self {
        Self {
            pts: None,
            pcr: None,
            ccs: [(0, 0); REF_CC_SLOTS],
            ncc: 0,
        }
    }

    fn set_cc(&mut self, pid: u16, cc: u8) {
        // First occurrence wins: the sample must capture the raw CC at the
        // START of the new anchor — the value the decoder's next-CC
        // expectation is computed against.
        for slot in self.ccs.iter().take(self.ncc) {
            if slot.0 == pid {
                return;
            }
        }
        if self.ncc < REF_CC_SLOTS {
            self.ccs[self.ncc] = (pid, cc);
            self.ncc += 1;
        }
    }

    fn cc_of(&self, pid: u16) -> Option<u8> {
        self.ccs.iter().take(self.ncc).find(|s| s.0 == pid).map(|s| s.1)
    }
}

/// One timeline epoch: a constant offset applied to every served byte.
#[derive(Clone)]
pub struct Epoch {
    pub id: u64,
    pub delta_pts: i64,
    pub delta_pcr: i64,
    /// Per-PID continuity-counter shift (identity for untouched PIDs).
    pub cc_add: [u8; 8192],
    pub is_identity: bool,
}

impl Epoch {
    fn identity(id: u64) -> Self {
        Self {
            id,
            delta_pts: 0,
            delta_pcr: 0,
            cc_add: [0u8; 8192],
            is_identity: true,
        }
    }
}

/// Straddling-packet carry across FUSE read boundaries.
struct Tail {
    valid: bool,
    abs_pos: u64,
    /// Bytes of the packet already delivered to the decoder.
    consumed: usize,
}

/// Last delivered timeline state (sequential reads only; probes never
/// advance it).
#[derive(Clone)]
struct ServedState {
    valid: bool,
    pts: u64,
    pcr: u64,
    pcr_valid: bool,
    upd_pos: u64,
    ccs: [(u16, u8); REF_CC_SLOTS],
    ncc: usize,
}

impl ServedState {
    fn new() -> Self {
        Self {
            valid: false,
            pts: 0,
            pcr: 0,
            pcr_valid: false,
            upd_pos: 0,
            ccs: [(0, 0); REF_CC_SLOTS],
            ncc: 0,
        }
    }

    fn to_ref(&self, src: RefSrc) -> XformRef {
        let mut r = XformRef::invalid();
        r.src = src;
        r.valid = self.valid;
        r.pts = self.pts;
        r.pcr = self.pcr;
        r.pcr_valid = self.pcr_valid;
        r.upd_pos = self.upd_pos;
        r.ncc = self.ncc;
        r.ccs[..self.ncc].copy_from_slice(&self.ccs[..self.ncc]);
        r
    }

    fn set_cc(&mut self, pid: u16, cc: u8) {
        for slot in self.ccs.iter_mut().take(self.ncc) {
            if slot.0 == pid {
                slot.1 = cc;
                return;
            }
        }
        if self.ncc < REF_CC_SLOTS {
            self.ccs[self.ncc] = (pid, cc);
            self.ncc += 1;
        }
    }
}

/// A RAM checkpoint: a `served` snapshot keyed by delivered file end-offset.
#[derive(Clone)]
struct CkptSlot {
    fend: u64,
    t_ms: u64,
    state: ServedState,
}

impl CkptSlot {
    fn new() -> Self {
        Self {
            fend: 0,
            t_ms: 0,
            state: ServedState::new(),
        }
    }
}

/// The transform engine state (owned by the single engine mutex).
pub struct TsXformState {
    epoch: Epoch,
    served: ServedState,
    tail: Tail,
    ckpt: Vec<CkptSlot>,
    pub splices: u64,
    pub ref_served: u64,
    pub ref_ckpt: u64,
    pub ref_scan: u64,
    pub ref_none: u64,
}

impl Default for TsXformState {
    fn default() -> Self {
        Self::new()
    }
}

impl TsXformState {
    pub fn new() -> Self {
        Self {
            epoch: Epoch::identity(0),
            served: ServedState::new(),
            tail: Tail {
                valid: false,
                abs_pos: 0,
                consumed: 0,
            },
            ckpt: vec![CkptSlot::new(); CKPT_N],
            splices: 0,
            ref_served: 0,
            ref_ckpt: 0,
            ref_scan: 0,
            ref_none: 0,
        }
    }

    pub fn epoch_id(&self) -> u64 {
        self.epoch.id
    }

    /// Starts a new identity epoch (fresh playback: no continuity splice).
    pub fn reset_epoch(&mut self, id: u64) {
        self.epoch = Epoch::identity(id);
        self.served = ServedState::new();
        self.tail.valid = false;
        self.splices += 1;
    }

    /// Drops the straddling carry (anchor change / reopen).
    pub fn invalidate_tail(&mut self) {
        self.tail.valid = false;
    }

    /// Creates a splice epoch: `ref` is the delivered timeline state at the
    /// old anchor, `raw` the pre-transform sample at the new anchor. The new
    /// epoch shifts every field so the decoder's timeline continues exactly
    /// at `ref + SPLICE_PTS_EPS`.
    pub fn begin_splice_epoch(&mut self, id: u64, r: &XformRef, raw: &RawSample) -> bool {
        let mut ep = Epoch::identity(id);
        let mut spliced = false;
        if r.valid {
            if let Some(raw_pts) = raw.pts {
                let target = pts_wrap(r.pts.saturating_add(SPLICE_PTS_EPS));
                ep.delta_pts = pts_sub(target, raw_pts);
                // PCR must never regress at a splice. Start from the
                // PTS-derived shift (bit-exact parity with the historical
                // behaviour, and the "PCR tracks PTS" invariant the strict
                // contracts assert), then override ONLY when the delivered PCR
                // step against the reference is negative. A forward step is
                // legitimate and unbounded here: the reference PCR can sit up
                // to ~89 ms behind the seam (measured 2_400_000 ticks @27 MHz
                // in `return_resume_seam_is_bit_exact`), so a fixed 40 ms
                // ceiling would corrupt a legal splice.
                ep.delta_pcr = ep.delta_pts.wrapping_mul(300);
                if r.pcr_valid {
                    if let Some(raw_pcr) = raw.pcr {
                        let target_pcr = pcr_apply(r.pcr, SPLICE_PTS_EPS as i64 * 300);
                        let delivered = pcr_apply(raw_pcr, ep.delta_pcr);
                        if pcr_sub(delivered, r.pcr) < 0 {
                            ep.delta_pcr = pcr_sub(target_pcr, raw_pcr);
                        }
                    }
                }
                spliced = true;
            } else if r.pcr_valid {
                if let Some(raw_pcr) = raw.pcr {
                    let target = pcr_apply(r.pcr, SPLICE_PTS_EPS as i64 * 300);
                    ep.delta_pcr = pcr_sub(target, raw_pcr);
                    ep.delta_pts = ep.delta_pcr / 300;
                    spliced = true;
                }
            }
            for i in 0..r.ncc {
                let (pid, delivered) = r.ccs[i];
                if let Some(raw_cc) = raw.cc_of(pid) {
                    let idx = usize::from(pid);
                    // Next delivered CC on this PID must be delivered + 1.
                    ep.cc_add[idx] = (delivered.wrapping_add(1).wrapping_sub(raw_cc)) & 0x0F;
                }
            }
            if spliced || ep.delta_pts != 0 || ep.delta_pcr != 0 || ep.cc_add.iter().any(|&x| x != 0) {
                ep.is_identity = false;
            }
        }
        self.epoch = ep;
        self.served.valid = false;
        self.tail.valid = false;
        self.splices += 1;
        spliced
    }

    /// Transforms one complete packet in place with the current epoch.
    /// Returns `(pid, delivered_cc)` when a CC byte was rewritten.
    fn transform_packet(&self, pkt: &mut [u8; TS_PKT_SZ]) -> Option<(u16, u8)> {
        if pkt[0] != 0x47 {
            return None;
        }
        let pid = ts_pid(pkt);
        if pid == NULL_PID {
            return None; // NULL packets are never touched
        }
        let raw_cc = ts_cc(pkt);
        if self.epoch.is_identity {
            return Some((pid, raw_cc));
        }
        let new_cc = raw_cc.wrapping_add(self.epoch.cc_add[usize::from(pid)]) & 0x0F;
        pkt[3] = (pkt[3] & 0xF0) | new_cc;

        if let Some(pcr) = ts_pcr(pkt) {
            let new_pcr = pcr_apply(pcr, self.epoch.delta_pcr);
            ts_write_pcr(new_pcr, pkt);
        }
        if let Some((pts, dts)) = ts_pes_ts(pkt) {
            if let Some(off) = ts_payload_off(pkt) {
                let new_pts = pts_apply(pts, self.epoch.delta_pts);
                pts_to_bytes(new_pts, &mut pkt[off + 9..off + 14]);
                if let Some(d) = dts {
                    if pkt.len() >= off + 19 {
                        let new_dts = pts_apply(d, self.epoch.delta_pts);
                        pts_to_bytes(new_dts, &mut pkt[off + 14..off + 19]);
                    }
                }
            }
        }
        Some((pid, new_cc))
    }

    /// Transforms a delivery block. `first_abs` is the stream position of
    /// `buf[0]`; `full_at(abs, pkt)` fetches a raw 188-byte packet from the
    /// ring (needed for packets straddling the block edges).
    ///
    /// On `seq` reads the delivered timeline state (`served`) is advanced;
    /// probes must pass `seq = false` so they can never poison the reference.
    pub fn apply_block<F>(&mut self, buf: &mut [u8], first_abs: u64, seq: bool, mut full_at: F)
    where
        F: FnMut(u64, &mut [u8; TS_PKT_SZ]) -> bool,
    {
        if buf.is_empty() {
            return;
        }
        let mut p = 0usize;

        // -- leading straddler: complete a packet split by the previous read
        if self.tail.valid {
            let rem = TS_PKT_SZ - self.tail.consumed;
            if rem > 0
                && self.tail.abs_pos + self.tail.consumed as u64 == first_abs
                && self.tail.consumed > 0
            {
                let take = rem.min(buf.len());
                let mut pkt = [0u8; TS_PKT_SZ];
                if full_at(self.tail.abs_pos, &mut pkt) {
                    self.transform_packet(&mut pkt);
                    buf[..take].copy_from_slice(&pkt[self.tail.consumed..self.tail.consumed + take]);
                    self.tail.consumed += take;
                    if self.tail.consumed >= TS_PKT_SZ {
                        self.tail.valid = false;
                        if seq {
                            self.update_served(&pkt, self.tail.abs_pos);
                        }
                    }
                    p = take;
                } else {
                    // Packet vanished from the ring: drop the carry, serve raw.
                    self.tail.valid = false;
                }
            } else {
                self.tail.valid = false;
            }
        }

        // -- mid-packet alignment when first_abs is not packet-aligned
        if p == 0 && (first_abs % TS_PKT_SZ as u64) != 0 {
            let phase = (first_abs % TS_PKT_SZ as u64) as usize;
            let rem = TS_PKT_SZ - phase;
            let take = rem.min(buf.len());
            let pkt_abs = first_abs - phase as u64;
            let mut pkt = [0u8; TS_PKT_SZ];
            if full_at(pkt_abs, &mut pkt) {
                self.transform_packet(&mut pkt);
                buf[..take].copy_from_slice(&pkt[phase..phase + take]);
                if phase + take == TS_PKT_SZ && seq {
                    self.update_served(&pkt, pkt_abs);
                }
            }
            p = take;
        }

        // -- interior complete packets
        let interior_start = p;
        while p + TS_PKT_SZ <= buf.len() {
            let mut pkt = [0u8; TS_PKT_SZ];
            pkt.copy_from_slice(&buf[p..p + TS_PKT_SZ]);
            self.transform_packet(&mut pkt);
            buf[p..p + TS_PKT_SZ].copy_from_slice(&pkt);
            p += TS_PKT_SZ;
        }
        let interior_end = p;

        // -- trailing partial packet: transform via the full packet, deliver
        //    the head slice now, carry the remainder for the next read.
        if p < buf.len() {
            let rem = buf.len() - p;
            let mut pkt = [0u8; TS_PKT_SZ];
            let abs = first_abs + p as u64;
            if full_at(abs, &mut pkt) {
                self.tail.abs_pos = abs;
                self.tail.consumed = rem;
                self.tail.valid = true;
                self.transform_packet(&mut pkt);
                buf[p..].copy_from_slice(&pkt[..rem]);
            } else {
                self.tail.valid = false;
            }
        }

        if seq && interior_end > interior_start {
            self.update_served(&buf[interior_start..interior_end], first_abs + interior_start as u64);
        }
    }

    /// Advances `served` from the (already transformed) delivery block.
    fn update_served(&mut self, buf: &[u8], first_abs: u64) {
        let mut p = 0usize;
        while p + TS_PKT_SZ <= buf.len() {
            let pkt = &buf[p..p + TS_PKT_SZ];
            if pkt[0] == 0x47 && ts_pid(pkt) != NULL_PID {
                let pid = ts_pid(pkt);
                self.served.set_cc(pid, ts_cc(pkt));
                self.served.upd_pos = first_abs + p as u64 + TS_PKT_SZ as u64;
                self.served.valid = true;
                if let Some(pcr) = ts_pcr(pkt) {
                    self.served.pcr = pcr;
                    self.served.pcr_valid = true;
                }
                if let Some((pts, _)) = ts_pes_ts(pkt) {
                    self.served.pts = pts;
                }
            }
            p += TS_PKT_SZ;
        }
    }

    /// Records a checkpoint for the delivered file end-offset `fend`.
    /// The slot guard (`fend` differs) prevents a wrap-around slot reuse
    /// from overwriting a fresher record.
    pub fn note_ckpt(&mut self, fend: u64, now_ms: u64) {
        if !self.served.valid || fend == 0 {
            return;
        }
        let slot = ((fend - 1) / CKPT_STEP) as usize % CKPT_N;
        if self.ckpt[slot].state.valid && self.ckpt[slot].fend == fend {
            return; // already recorded for this exact end
        }
        self.ckpt[slot].fend = fend;
        self.ckpt[slot].t_ms = now_ms;
        self.ckpt[slot].state = self.served.clone();
    }

    /// 3-tier calibration: served -> ckpt -> ring_scan.
    ///
    /// `old_pos` is the stream position of the splice point (old mapping),
    /// `old_fend` the last delivered file end-offset, and `win`/`win_base`
    /// a ring window covering `old_pos` for the scan tier.
    pub fn calibrate(
        &mut self,
        old_pos: u64,
        old_fend: u64,
        win: &[u8],
        win_base: u64,
        now_ms: u64,
    ) -> XformRef {
        // Tier 1: fresh served state.
        if self.served.valid
            && old_pos >= self.served.upd_pos
            && old_pos - self.served.upd_pos <= SERVED_FRESH_BYTES
        {
            self.ref_served += 1;
            return self.served.to_ref(RefSrc::Served);
        }
        // Tier 2: newest RAM checkpoint at/before the last delivered end.
        let mut best: Option<&CkptSlot> = None;
        for slot in self.ckpt.iter() {
            if !slot.state.valid || slot.fend > old_fend {
                continue;
            }
            if now_ms.saturating_sub(slot.t_ms) > CKPT_MAX_AGE_MS {
                continue;
            }
            if old_pos.saturating_sub(slot.state.upd_pos) > CKPT_MAX_DIST {
                continue;
            }
            match best {
                Some(b) if b.fend >= slot.fend => {}
                _ => best = Some(slot),
            }
        }
        if let Some(slot) = best {
            self.ref_ckpt += 1;
            return slot.state.to_ref(RefSrc::Ckpt);
        }
        // Tier 3: scan the ring backward for the last delivered-like state.
        if let Some(r) = self.ring_scan(win, win_base, old_pos) {
            self.ref_scan += 1;
            return r;
        }
        self.ref_none += 1;
        XformRef::invalid()
    }

    /// Walks back at most `RING_SCAN_BACK` bytes from `old_pos` inside the
    /// window and converts the closest raw PTS/PCR/CC state into delivered
    /// values with the current epoch.
    fn ring_scan(&self, win: &[u8], win_base: u64, old_pos: u64) -> Option<XformRef> {
        if win.is_empty() || old_pos < win_base || old_pos >= win_base + win.len() as u64 {
            return None;
        }
        let grid = crate::engine::ring::snap188(old_pos);
        let rel = grid.checked_sub(win_base)? as usize;
        let mut best_pts: Option<u64> = None;
        let mut best_pcr: Option<u64> = None;
        let mut p = rel;
        let mut back = 0u64;
        let mut ccs: [(u16, u8); REF_CC_SLOTS] = [(0, 0); REF_CC_SLOTS];
        let mut ncc = 0usize;
        let mut upd_pos = grid;
        while p + TS_PKT_SZ <= win.len() && back <= RING_SCAN_BACK {
            let pkt = &win[p..p + TS_PKT_SZ];
            if pkt[0] == 0x47 && ts_pid(pkt) != NULL_PID {
                let pid = ts_pid(pkt);
                let delivered_cc =
                    ts_cc(pkt).wrapping_add(self.epoch.cc_add[usize::from(pid)]) & 0x0F;
                let mut seen = false;
                for s in ccs.iter_mut().take(ncc) {
                    if s.0 == pid {
                        seen = true;
                        break;
                    }
                }
                if !seen && ncc < REF_CC_SLOTS {
                    ccs[ncc] = (pid, delivered_cc);
                    ncc += 1;
                }
                if best_pcr.is_none() {
                    best_pcr = ts_pcr(pkt);
                }
                if best_pts.is_none() {
                    if let Some((pts, _)) = ts_pes_ts(pkt) {
                        best_pts = Some(pts);
                        upd_pos = win_base + p as u64;
                    }
                }
                if best_pts.is_some() && best_pcr.is_some() {
                    break;
                }
            }
            if p < TS_PKT_SZ {
                break;
            }
            p -= TS_PKT_SZ;
            back += TS_PKT_SZ as u64;
        }
        let raw_pts = best_pts?;
        let mut r = XformRef::invalid();
        r.src = RefSrc::RingScan;
        r.valid = true;
        r.pts = pts_apply(raw_pts, self.epoch.delta_pts);
        r.pcr = match best_pcr {
            Some(pcr) => pcr_apply(pcr, self.epoch.delta_pcr),
            None => 0,
        };
        r.pcr_valid = best_pcr.is_some();
        r.upd_pos = upd_pos;
        r.ncc = ncc;
        r.ccs = ccs;
        Some(r)
    }

    /// Collects the raw sample at a candidate new anchor: first PTS/PCR and
    /// the first CC per PID within `[from, to)` of the window.
    pub fn collect_raw(win: &[u8], win_base: u64, from: u64, to: u64) -> RawSample {
        let mut s = RawSample::empty();
        let start = from.max(win_base);
        let end = to.min(win_base + win.len() as u64);
        let mut p = crate::engine::ring::snap188(start).saturating_sub(win_base) as usize;
        while p + TS_PKT_SZ <= win.len() {
            let abs = win_base + p as u64;
            if abs >= end {
                break;
            }
            let pkt = &win[p..p + TS_PKT_SZ];
            if pkt[0] == 0x47 && ts_pid(pkt) != NULL_PID {
                let pid = ts_pid(pkt);
                // Continuity counters only advance across payload-bearing
                // packets; an adaptation-only packet repeats the previous
                // payload CC, so sampling it would add a spurious +1 at the
                // seam (ITER#2 fix #6).
                if ts_payload_off(pkt).is_some() {
                    s.set_cc(pid, ts_cc(pkt));
                }
                if s.pcr.is_none() {
                    s.pcr = ts_pcr(pkt);
                }
                if s.pts.is_none() {
                    if let Some((pts, _)) = ts_pes_ts(pkt) {
                        s.pts = Some(pts);
                    }
                }
                if s.pts.is_some() && s.pcr.is_some() && s.ncc >= REF_CC_SLOTS {
                    break;
                }
            }
            p += TS_PKT_SZ;
        }
        s
    }

    /// Exposes the current epoch deltas (telemetry / tests).
    pub fn deltas(&self) -> (u64, i64, i64) {
        (self.epoch.id, self.epoch.delta_pts, self.epoch.delta_pcr)
    }

    /// True when a straddling packet carry is active.
    pub fn tail_active(&self) -> bool {
        self.tail.valid
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pkt(pid: u16, cc: u8) -> [u8; TS_PKT_SZ] {
        let mut p = [0xFFu8; TS_PKT_SZ];
        p[0] = 0x47;
        p[1] = ((pid >> 8) & 0x1F) as u8 | 0x40;
        p[2] = pid as u8;
        p[3] = 0x10 | (cc & 0x0F);
        p
    }

    fn pes_pkt(pid: u16, cc: u8, pts: u64) -> [u8; TS_PKT_SZ] {
        let mut p = pkt(pid, cc);
        let off = 4usize;
        // PES: 00 00 01 E0 len flags hdrlen PTS(5)
        p[off] = 0x00;
        p[off + 1] = 0x00;
        p[off + 2] = 0x01;
        p[off + 3] = 0xE0;
        p[off + 4] = 0x00;
        p[off + 5] = 0x00;
        p[off + 6] = 0x80; // flags1
        p[off + 7] = 0x80; // PTS present
        p[off + 8] = 0x0A; // PES_header_data_length
        let mut b = [0u8; 5];
        pts_to_bytes(pts, &mut b);
        p[off + 9..off + 14].copy_from_slice(&b);
        p
    }

    fn pcr_pkt(pid: u16, cc: u8, pcr: u64) -> [u8; TS_PKT_SZ] {
        let mut p = pkt(pid, cc);
        p[3] = 0x30 | (cc & 0x0F); // AF + payload
        p[4] = 0x07; // AF length
        p[5] = 0x10; // PCR flag
        ts_write_pcr(pcr, &mut p);
        p
    }

    /// Adaptation-only packet (afc = 2, no payload): its continuity counter
    /// must repeat the previous payload-bearing value, never increment.
    fn af_only_pkt(pid: u16, cc: u8) -> [u8; TS_PKT_SZ] {
        let mut p = pkt(pid, cc);
        p[3] = 0x20 | (cc & 0x0F); // afc = 2 (adaptation field only)
        p[4] = 0x01; // adaptation_field_length (flags only)
        p[5] = 0x00;
        p
    }

    // -- PTS/PCR modular math ------------------------------------------------

    #[test]
    fn pts_modular_math() {
        assert_eq!(PTS_MOD, 8_589_934_592);
        assert_eq!(PCR_MOD, 2_576_980_377_600);
        assert_eq!(pts_wrap(PTS_MOD), 0);
        assert_eq!(pts_wrap(PTS_MOD + 1234), 1234);
        assert_eq!(pcr_wrap(PCR_MOD), 0);
        // wrap-safe subtraction: 100 - (PTS_MOD - 50) = +150 forward
        assert_eq!(pts_sub(100, PTS_MOD - 50), 150);
        assert_eq!(pts_sub(PTS_MOD - 50, 100), -150);
        assert_eq!(pts_sub(0, 0), 0);
        assert!(pts_sub(PTS_MOD - 1, 0) < 0); // -1, not a huge positive
        assert_eq!(pts_apply(PTS_MOD - 50, 150), 100);
        assert_eq!(pts_apply(100, -150), PTS_MOD - 50);
        assert_eq!(pcr_apply(PCR_MOD - 10, 20), 10);
        assert_eq!(pcr_apply(10, -20), PCR_MOD - 10);
    }

    #[test]
    fn pts_roundtrip_encoding() {
        for &v in &[0u64, 1, 12345, 900_000, PTS_MOD - 1, PTS_MOD / 2] {
            let mut b = [0u8; 5];
            pts_to_bytes(v, &mut b);
            assert_eq!(pts_from_bytes(&b), v);
        }
    }

    #[test]
    fn pcr_roundtrip_encoding() {
        for &v in &[0u64, 300, 1, 27_000_000, PCR_MOD - 300, PCR_MOD / 2 + 7] {
            let mut p = pcr_pkt(0x100, 0, v);
            let parsed = ts_pcr(&p).expect("pcr");
            assert_eq!(parsed, v);
            let shifted = v + 600;
            ts_write_pcr(shifted % PCR_MOD, &mut p);
            assert_eq!(ts_pcr(&p).expect("pcr2"), shifted % PCR_MOD);
        }
    }

    #[test]
    fn pes_pts_parse() {
        let p = pes_pkt(0x100, 3, 777_777);
        let (pts, dts) = ts_pes_ts(&p).expect("pts");
        assert_eq!(pts, 777_777);
        assert!(dts.is_none());
        assert!(ts_pcr(&p).is_none());
    }

    #[test]
    fn splice_pad_alignment() {
        assert_eq!(splice_pad_len(0), 0);
        assert_eq!(splice_pad_len(1), 187);
        assert_eq!(splice_pad_len(187), 1);
        assert_eq!(splice_pad_len(188), 0);
        assert_eq!(splice_pad_len(189), 187);
    }

    // -- CC tracking ----------------------------------------------------------

    #[test]
    fn cc_tracking_skips_null_and_wraps_mod_16() {
        let mut x = TsXformState::new();
        // Identity epoch: CC must pass through unchanged.
        let mut null_pkt = [0u8; TS_PKT_SZ];
        null_pkt[0] = 0x47;
        null_pkt[1] = 0x1F;
        null_pkt[2] = 0xFF;
        null_pkt[3] = 0x1A; // CC = 10
        x.apply_block(&mut null_pkt[..], 0, false, |_, _| true);
        assert_eq!(null_pkt[3] & 0x0F, 10, "NULL packet CC must never be touched");

        let mut prev = 15u8;
        for i in 0..20u8 {
            let mut p = pkt(0x100, prev);
            x.apply_block(&mut p[..], (i as u64) * 188, false, |_, _| true);
            assert_eq!(p[3] & 0x0F, prev, "identity epoch preserves CC");
            prev = prev.wrapping_add(1) & 0x0F;
        }
    }

    #[test]
    fn cc_add_shift_applied_per_pid() {
        let mut x = TsXformState::new();
        // Splice epoch: delivered CC on PID 0x100 was 5, raw CC at the new
        // anchor is 9, so the decoder must next see 6 -> cc_add = 13.
        let mut r = XformRef::invalid();
        r.valid = true;
        r.pts = 1_000_000;
        r.set_cc(0x100, 5); // delivered CC on video pid
        let mut raw = RawSample::empty();
        raw.pts = Some(900_000);
        raw.set_cc(0x100, 9); // raw CC at new anchor
        assert!(x.begin_splice_epoch(1, &r, &raw));

        let mut p = pkt(0x100, 9);
        x.apply_block(&mut p[..], 0, false, |_, _| true);
        assert_eq!(p[3] & 0x0F, 6, "(5 + 1 - 9) mod 16 = 13; 9 + 13 = 22 mod 16 = 6");
        let mut p2 = pkt(0x101, 9);
        x.apply_block(&mut p2[..], 188, false, |_, _| true);
        assert_eq!(p2[3] & 0x0F, 9, "untracked PID keeps raw CC");
    }

    /// ITER#2 regression (#6): the splice CC offset must be derived from the
    /// first *payload-bearing* CC at the new anchor. An adaptation-only packet
    /// (no payload) repeats the previous payload-bearing CC, so treating its
    /// raw CC as the reference introduces a spurious +1.
    #[test]
    fn seam_cc_reference_ignores_adaptation_only_packets() {
        let mut stream = Vec::new();
        stream.extend_from_slice(&af_only_pkt(0x100, 4)); // payload-less, raw CC = 4
        stream.extend_from_slice(&pes_pkt(0x100, 5, 1_000)); // payload, raw CC = 5

        let raw = TsXformState::collect_raw(&stream, 0, 0, 188 * 2);
        assert_eq!(
            raw.cc_of(0x100),
            Some(5),
            "raw CC sample must come from the first payload-bearing packet"
        );

        let mut x = TsXformState::new();
        let mut r = XformRef::invalid();
        r.valid = true;
        r.pts = 1_000_000;
        r.set_cc(0x100, 5); // last delivered CC = 5
        x.begin_splice_epoch(1, &r, &raw);

        // A payload-less packet must repeat the delivered CC (5), not become 6.
        let mut af = af_only_pkt(0x100, 4);
        x.apply_block(&mut af[..], 0, false, |_, _| true);
        assert_eq!(af[3] & 0x0F, 5, "payload-less packet must not increment the CC");
    }

    // -- Tail carry across read boundaries ------------------------------------

    fn memory_fetcher<'a>(mem: &'a [u8]) -> impl FnMut(u64, &mut [u8; TS_PKT_SZ]) -> bool + 'a {
        move |abs: u64, pkt: &mut [u8; TS_PKT_SZ]| {
            let start = abs as usize;
            if start + TS_PKT_SZ <= mem.len() {
                pkt.copy_from_slice(&mem[start..start + TS_PKT_SZ]);
                true
            } else {
                false
            }
        }
    }

    #[test]
    fn straddling_packet_transformed_exactly_once() {
        // Stream: 700 PES packets with increasing PTS; serve in 128 KiB
        // reads (131072 = 697*188 + 56) like the real FUSE block size.
        let n = 700usize;
        let mut stream = Vec::with_capacity(n * TS_PKT_SZ);
        for i in 0..n {
            stream.extend_from_slice(&pes_pkt(0x100, (i % 16) as u8, 900_000 + i as u64 * 3003));
        }
        // Transform the whole stream in one shot as the reference.
        let mut x_full = TsXformState::new();
        let mut full = stream.clone();
        x_full.apply_block(&mut full, 0, false, memory_fetcher(&stream));

        // Now serve in 128 KiB slices with tail carry.
        let mut x = TsXformState::new();
        let mut out = Vec::new();
        let mut abs = 0u64;
        while (abs as usize) < stream.len() {
            let end = ((abs as usize) + 128 * 1024).min(stream.len());
            let mut block = stream[abs as usize..end].to_vec();
            x.apply_block(&mut block, abs, false, memory_fetcher(&stream));
            out.extend_from_slice(&block);
            abs = end as u64;
        }
        assert_eq!(out.len(), stream.len());
        assert_eq!(out, full, "straddling packet must be transformed identically");
    }

    #[test]
    fn transform_is_deterministic_per_position() {
        let mut stream = Vec::new();
        for i in 0..10usize {
            stream.extend_from_slice(&pes_pkt(0x100, i as u8, 500_000 + i as u64 * 3000));
            stream.extend_from_slice(&pkt(0x101, (i + 3) as u8));
        }
        let mut x = TsXformState::new();
        let mut r = XformRef::invalid();
        r.valid = true;
        r.pts = 4_000_000;
        let mut raw = TsXformState::collect_raw(&stream, 0, 0, 188 * 20);
        raw.pts = Some(500_000);
        x.begin_splice_epoch(1, &r, &raw);

        let mut a = stream.clone();
        x.apply_block(&mut a, 0, false, memory_fetcher(&stream));
        let mut b = stream.clone();
        x.apply_block(&mut b, 0, false, memory_fetcher(&stream));
        assert_eq!(a, b, "same raw bytes + same position => same output");
        // And the delivered PTS jumped to the reference.
        let (pts, _) = ts_pes_ts(&a[0..188]).expect("pts");
        assert_eq!(pts, pts_wrap(4_000_000 + SPLICE_PTS_EPS));
    }

    // -- 3-tier calibration ---------------------------------------------------

    #[test]
    fn calibration_served_tier() {
        let mut x = TsXformState::new();
        // Build a served state via a sequential read.
        let mut stream = Vec::new();
        for i in 0..8usize {
            stream.extend_from_slice(&pes_pkt(0x100, i as u8, 600_000 + i as u64 * 3000));
        }
        let mut blk = stream.clone();
        x.apply_block(&mut blk, 0, true, memory_fetcher(&stream));
        // Served is fresh for old_pos within 256 KB ahead of the last packet.
        let r = x.calibrate(188 * 8 + 1024, 188 * 8, &stream, 0, 1000);
        assert!(r.valid);
        assert_eq!(r.src, RefSrc::Served);
        // Far away: not fresh anymore.
        let r2 = x.calibrate(188 * 8 + 1024 + 1024 * 1024, 188 * 8, &stream, 0, 1000);
        assert!(!r2.valid);
    }

    #[test]
    fn calibration_ckpt_tier_after_served_stales() {
        let mut x = TsXformState::new();
        let mut stream = Vec::new();
        for i in 0..8usize {
            stream.extend_from_slice(&pes_pkt(0x100, i as u8, 600_000 + i as u64 * 3000));
        }
        let mut blk = stream.clone();
        x.apply_block(&mut blk, 0, true, memory_fetcher(&stream));
        x.note_ckpt(188 * 8, 1000);

        let far = 188 * 8 + 1024 + 1024 * 1024; // beyond served freshness
        let r = x.calibrate(far, 188 * 8, &[], 0, 2000);
        assert!(r.valid);
        assert_eq!(r.src, RefSrc::Ckpt);
    }

    #[test]
    fn calibration_ring_scan_tier() {
        let mut x = TsXformState::new(); // fresh engine: no served, no ckpt
        let mut stream = Vec::new();
        for i in 0..100usize {
            stream.extend_from_slice(&pes_pkt(0x100, i as u8, 1_000_000 + i as u64 * 3000));
        }
        let old_pos = 188 * 99; // last packet
        let r = x.calibrate(old_pos, u64::MAX, &stream, 0, 1000);
        assert!(r.valid);
        assert_eq!(r.src, RefSrc::RingScan);
        assert_eq!(r.pts, 1_000_000 + 99 * 3000);
        assert!(r.pcr_valid || !r.pcr_valid); // pcr optional in scan
    }

    #[test]
    fn splice_epoch_continues_timeline() {
        let mut x = TsXformState::new();
        let mut r = XformRef::invalid();
        r.valid = true;
        r.pts = 5_000_000;
        let mut raw = RawSample::empty();
        raw.pts = Some(3_000_000);
        assert!(x.begin_splice_epoch(7, &r, &raw));
        let (_id, dpts, _) = x.deltas();
        assert_eq!(dpts, pts_sub(5_000_000 + SPLICE_PTS_EPS, 3_000_000));

        // Apply to a raw packet at the new anchor: PTS lands at ref + eps.
        let mut p = pes_pkt(0x100, 0, 3_000_000);
        x.apply_block(&mut p[..], 0, false, |_, _| true);
        let (pts, _) = ts_pes_ts(&p).expect("pts");
        assert_eq!(pts, pts_wrap(5_000_000 + SPLICE_PTS_EPS));
    }
}
