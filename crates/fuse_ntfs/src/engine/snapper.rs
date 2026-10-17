//! D12 anchor search: the 4-tier keyframe snapper for the 2013 MStar decoder.
//!
//! When the FUSE engine must (re)anchor — fresh open, confirmed seek, return
//! resume, ring wrap — it must land on a byte position the hardware demuxer
//! can decode from cold. The D12 search walks outward from the desired
//! position, degrading gracefully:
//!
//! 1. `PAT_SPS`  — backward up to 2 MiB for a PAT packet whose GOP contains
//!    an SPS/PPS (a clean IDR entry point, per docs/ARCHITECTURE.md §2.3).
//! 2. `PAT`      — forward up to 2 MiB for any PAT packet start.
//! 3. `TRANSPORT`— three consecutive sync bytes (0x47 @ 188 B) on the grid.
//! 4. `snap188`  — last resort: the desired position on the 188 grid.
//!
//! All searches operate on a caller-provided ring window (`win`, with the
//! absolute stream position of `win[0]` given by `base`), so the snapper is
//! pure and fully unit-testable. The window must cover the searched range;
//! the engine clamps requests to ring residency before calling.
//!
//! Two guards wrap the raw snap:
//! * [`validate_scar`] rejects an anchor sitting on an upstream PTS
//!   discontinuity (> 5 s jump within 2 MiB) and advances 1 MiB past it.
//! * [`leadback_guard`] guarantees the final anchor keeps at least
//!   `RESUME_MIN_LEAD` cushion behind the live writer.

use crate::contract::TS_PKT_SZ;
use crate::engine::ring::snap188;
use crate::engine::ts_xform::{ts_payload_off, ts_pes_ts, ts_pusi, ts_has_sps, PTS_SCAR_5S};

/// Tier 1 backward search span.
pub const D12_BACK: u64 = 2 * 1024 * 1024;
/// Tier 2 forward search span.
pub const D12_FWD: u64 = 2 * 1024 * 1024;
/// Maximum distance from a PAT to an SPS for tier 1 to accept it.
pub const SPS_LOOKAHEAD: u64 = 1024 * 1024;
/// Scar scan window from the anchor head.
pub const SCAR_SCAN: u64 = 2 * 1024 * 1024;
/// Advance distance when a scar is detected.
pub const SCAR_SKIP: u64 = 1024 * 1024;
/// Maximum PES samples collected during a scar scan.
pub const SCAR_MAX_SAMPLES: usize = 24;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum D12Tier {
    PatSps,
    Pat,
    Transport,
    Snap188,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SnapOut {
    pub pos: u64,
    pub tier: D12Tier,
}

/// True when the packet at window offset `off` is a PAT start
/// (sync byte, PID 0x0000, PUSI, table_id 0x00 after the pointer field).
pub fn is_pat(win: &[u8], off: usize) -> bool {
    if off + TS_PKT_SZ > win.len() || win[off] != 0x47 {
        return false;
    }
    let pid = ((u16::from(win[off + 1] & 0x1F)) << 8) | u16::from(win[off + 2]);
    if pid != 0x0000 || !ts_pusi(&win[off..off + TS_PKT_SZ]) {
        return false;
    }
    let pkt = &win[off..off + TS_PKT_SZ];
    let payload = match ts_payload_off(pkt) {
        Some(p) => p,
        None => return false,
    };
    let ptr = usize::from(pkt[payload]);
    let table_off = payload + 1 + ptr;
    table_off < TS_PKT_SZ && pkt[table_off] == 0x00
}

/// True when an SPS/PPS access unit starts within `lookahead` bytes of the
/// packet at window offset `off` (inclusive).
pub fn has_sps_after(win: &[u8], off: usize, lookahead: u64) -> bool {
    let mut p = off;
    let limit = off.saturating_add(lookahead as usize);
    while p + TS_PKT_SZ <= win.len().min(limit) {
        if ts_has_sps(&win[p..p + TS_PKT_SZ]) {
            return true;
        }
        p += TS_PKT_SZ;
    }
    false
}

/// Finds the first 3-point sync confirmation (0x47 every 188 bytes) at or
/// after window offset `from`.
pub fn find_transport(win: &[u8], from: usize) -> Option<usize> {
    if win.len() < 3 * TS_PKT_SZ {
        return None;
    }
    let limit = win.len() - (2 * TS_PKT_SZ);
    let mut i = from.min(limit);
    while i < limit {
        if win[i] == 0x47 && win[i + TS_PKT_SZ] == 0x47 && win[i + 2 * TS_PKT_SZ] == 0x47 {
            return Some(i);
        }
        i += 1;
    }
    None
}

/// The D12 4-tier snap. `target` is the desired stream position; the window
/// must contain the searched range (engine clamps to ring residency).
pub fn d12_snap(win: &[u8], base: u64, target: u64, s_write: u64) -> SnapOut {
    let snapped = snap188(target).min(s_write);
    let off0 = snapped.saturating_sub(base).min(win.len() as u64) as usize;

    // -- Tier 1: PAT + SPS, walking backward up to D12_BACK.
    if !win.is_empty() {
        let start_p = off0.min(win.len().saturating_sub(TS_PKT_SZ));
        let mut p = start_p;
        while p + TS_PKT_SZ <= win.len() {
            if is_pat(win, p) && has_sps_after(win, p, SPS_LOOKAHEAD) {
                return SnapOut {
                    pos: base + p as u64,
                    tier: D12Tier::PatSps,
                };
            }
            if p < TS_PKT_SZ {
                break;
            }
            p -= TS_PKT_SZ;
            if snapped.saturating_sub(base + p as u64) > D12_BACK {
                break;
            }
        }
    }

    // -- Tier 2: any PAT, walking forward up to D12_FWD.
    let mut p = off0;
    while p + TS_PKT_SZ <= win.len() {
        if is_pat(win, p) {
            return SnapOut {
                pos: base + p as u64,
                tier: D12Tier::Pat,
            };
        }
        p = p.saturating_add(TS_PKT_SZ);
        if (base + p as u64).saturating_sub(snapped) > D12_FWD {
            break;
        }
    }

    // -- Tier 3: 3-point transport sync confirmation.
    if let Some(off) = find_transport(win, off0) {
        return SnapOut {
            pos: base + off as u64,
            tier: D12Tier::Transport,
        };
    }

    // -- Tier 4: the desired position on the 188 grid.
    SnapOut {
        pos: snapped,
        tier: D12Tier::Snap188,
    }
}

/// Scar validator: walks at most 2 MiB forward from `pos` looking for a PTS
/// discontinuity larger than 5 s (an upstream splice/gap the decoder cannot
/// ride through). Returns the advanced position and whether a skip happened.
pub fn validate_scar(win: &[u8], base: u64, pos: u64, s_write: u64) -> (u64, bool) {
    let mut p = match snap188(pos).checked_sub(base) {
        Some(rel) => rel as usize,
        None => return (pos, false),
    };
    let mut prev_pts: Option<u64> = None;
    let mut samples = 0usize;
    let scan_limit = pos.saturating_add(SCAR_SCAN).min(s_write.saturating_sub(TS_PKT_SZ as u64));
    let mut tracked_pid: Option<u16> = None;

    while p + TS_PKT_SZ <= win.len() && samples < SCAR_MAX_SAMPLES {
        let abs = base + p as u64;
        if abs >= scan_limit {
            break;
        }
        let pkt = &win[p..p + TS_PKT_SZ];
        if pkt[0] == 0x47 {
            let pid = crate::engine::ts_xform::ts_pid(pkt);
            if let Some((pts, _)) = ts_pes_ts(pkt) {
                if *tracked_pid.get_or_insert(pid) == pid {
                    if let Some(prev) = prev_pts {
                        let delta = crate::engine::ts_xform::pts_sub(pts, prev);
                        if delta.unsigned_abs() > PTS_SCAR_5S {
                            let skip = snap188(pos.saturating_add(SCAR_SKIP));
                            return (nearest_pat_forward(win, base, skip), true);
                        }
                    }
                    prev_pts = Some(pts);
                    samples += 1;
                }
            }
        }
        p += TS_PKT_SZ;
    }
    (pos, false)
}

/// Post-snap leadback guard: pulls the anchor back so at least
/// `RESUME_MIN_LEAD` bytes of cushion separate it from the live writer, then
/// snaps to the 188 grid.
pub fn leadback_guard(snapped: u64, s_write: u64) -> u64 {
    let floor = s_write.saturating_sub(crate::contract::RESUME_MIN_LEAD);
    snap188(snapped.min(floor))
}

/// Nearest PAT at or after `pos` (position-preserving resume anchor), or the
/// 188-grid snap when no PAT is reachable inside the window.
pub fn nearest_pat_forward(win: &[u8], base: u64, pos: u64) -> u64 {
    let snapped = snap188(pos);
    let mut p = snapped.saturating_sub(base).min(win.len() as u64) as usize;
    while p + TS_PKT_SZ <= win.len() {
        if is_pat(win, p) {
            return base + p as u64;
        }
        p += TS_PKT_SZ;
    }
    snapped
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::ts_xform::{pts_to_bytes, ts_afc};

    /// Builds a synthetic stream: PAT (+SPS) every `pat_period` packets,
    /// interleaved video PES (PID 0x100) and audio PES (PID 0x101).
    fn mk_stream(n: usize, pat_period: usize, with_sps: bool) -> Vec<u8> {
        let mut v = Vec::with_capacity((n + 1) * TS_PKT_SZ);
        for i in 0..n {
            if i % pat_period == 0 {
                let mut p = [0xFFu8; TS_PKT_SZ];
                p[0] = 0x47;
                p[1] = 0x40; // PUSI, PID 0x0000
                p[2] = 0x00;
                p[3] = 0x10;
                p[4] = 0x00; // pointer_field
                p[5] = 0x00; // table_id = PAT
                v.extend_from_slice(&p);
                if with_sps {
                    // Immediately followed by an SPS access unit.
                    let mut s = [0xFFu8; TS_PKT_SZ];
                    s[0] = 0x47;
                    s[1] = 0x40; // PUSI
                    s[2] = 0x00; // PID 0x0100 (video)
                    s[3] = 0x10;
                    s[4] = 0x00;
                    s[5] = 0x00;
                    s[6] = 0x00;
                    s[7] = 0x01;
                    s[8] = 0x67; // NAL type 7 (SPS)
                    v.extend_from_slice(&s);
                }
                continue;
            }
            let pid = if i % 2 == 0 { 0x0100 } else { 0x0101 };
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = ((pid >> 8) & 0x1F) as u8 | 0x40;
            p[2] = pid as u8;
            p[3] = 0x10;
            p[4] = 0x00;
            p[5] = 0x00;
            p[6] = 0x01;
            p[7] = 0xE0;
            let mut b = [0u8; 5];
            pts_to_bytes(900_000 + (i as u64) * 3000, &mut b);
            // PES header: pl = pkt[4..]; PTS lives at pl[9..14] => pkt[13..18].
            p[13..18].copy_from_slice(&b);
            v.extend_from_slice(&p);
        }
        v
    }

    

    #[test]
    fn d12_tier2_pat_forward() {
        // PATs exist but carry no SPS, so tier 1 is rejected everywhere and
        // tier 2 must find the next PAT forward from the target.
        let s = mk_stream(500, 100, false); // PATs at 0, 100, 200, 300, 400
        let s_write = s.len() as u64;
        let target = 350 * TS_PKT_SZ as u64;
        let out = d12_snap(&s, 0, target, s_write);
        assert_eq!(out.tier, D12Tier::Pat);
        assert_eq!(out.pos, 400 * TS_PKT_SZ as u64);
    }

    #[test]
    fn d12_tier3_transport() {
        // No PAT anywhere: only sync-grid packets.
        let mut s = vec![0x00u8; TS_PKT_SZ * 200];
        for p in (0..s.len()).step_by(TS_PKT_SZ) {
            s[p] = 0x47;
            s[p + 1] = 0x01;
            s[p + 2] = 0x00; // PID 0x0100, no PUSI, not PAT
            s[p + 3] = 0x10;
        }
        let s_write = s.len() as u64;
        let out = d12_snap(&s, 0, 50 * TS_PKT_SZ as u64, s_write);
        assert_eq!(out.tier, D12Tier::Transport);
        assert_eq!(out.pos, 50 * TS_PKT_SZ as u64);
    }

    #[test]
    fn d12_tier4_snap188_fallback() {
        let s = vec![0x11u8; TS_PKT_SZ * 50];
        let out = d12_snap(&s, 0, 10 * TS_PKT_SZ as u64 + 7, s.len() as u64);
        assert_eq!(out.tier, D12Tier::Snap188);
        assert_eq!(out.pos, 10 * TS_PKT_SZ as u64);
    }

    #[test]
    fn transport_needs_three_points() {
        let mut s = vec![0u8; 3 * TS_PKT_SZ];
        s[0] = 0x47;
        s[TS_PKT_SZ] = 0x47;
        // Third sync byte deliberately absent.
        assert_eq!(find_transport(&s, 0), None);
        s[2 * TS_PKT_SZ] = 0x47;
        assert_eq!(find_transport(&s, 0), Some(0));
    }

    #[test]
    fn scar_validator_skips_past_discontinuity() {
        let mut s = Vec::new();
        // 20 clean PES packets, then a +60 s PTS scar, then more packets.
        for i in 0..30usize {
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = 0x40; // PUSI
            p[2] = 0x00; // PID 0x0100
            p[3] = 0x10;
            p[4] = 0x00;
            p[5] = 0x00;
            p[6] = 0x01;
            p[7] = 0xE0;
            let pts = if i >= 20 {
                900_000 + i as u64 * 3000 + 60 * 90_000
            } else {
                900_000 + i as u64 * 3000
            };
            let mut b = [0u8; 5];
            pts_to_bytes(pts, &mut b);
            p[13..18].copy_from_slice(&b);
            s.extend_from_slice(&p);
        }
        let start = 19 * TS_PKT_SZ as u64; // just before the scar
        let (pos, skipped) = validate_scar(&s, 0, start, s.len() as u64);
        assert!(skipped);
        assert_eq!(pos, snap188(start + SCAR_SKIP));

        // A clean window never skips.
        let (pos2, skipped2) = validate_scar(&s, 0, 0, 19 * TS_PKT_SZ as u64);
        assert!(!skipped2);
        assert_eq!(pos2, 0);
    }

    #[test]
    fn leadback_guard_enforces_min_cushion() {
        let s_write = 100_000_000;
        // Anchor too close to the live edge: pulled back to the floor (the
        // 188 snap only increases the cushion, never violates it).
        let guarded = leadback_guard(s_write - 1024, s_write);
        assert!(s_write - guarded >= crate::contract::RESUME_MIN_LEAD);
        assert!(s_write - guarded < crate::contract::RESUME_MIN_LEAD + TS_PKT_SZ as u64);
        assert_eq!(guarded, snap188(100_000_000 - crate::contract::RESUME_MIN_LEAD));
        // Deep anchor: untouched.
        let deep = snap188(s_write - 16 * 1024 * 1024);
        assert_eq!(leadback_guard(deep, s_write), deep);
        // Degenerate: writer below the cushion.
        assert_eq!(leadback_guard(1024, 1024), 0);
    }

    #[test]
    fn nearest_pat_forward_finds_entry() {
        let s = mk_stream(100, 25, false);
        // Between PATs: forward search must land on the next PAT.
        let pos = 30 * TS_PKT_SZ as u64;
        let hit = nearest_pat_forward(&s, 0, pos);
        assert_eq!(hit, 50 * TS_PKT_SZ as u64);
        // Exactly on a PAT: itself.
        assert_eq!(nearest_pat_forward(&s, 0, 50 * TS_PKT_SZ as u64), 50 * TS_PKT_SZ as u64);
    }

    #[test]
    fn payload_offset_handles_adaptation_field() {
        let mut p = [0xFFu8; TS_PKT_SZ];
        p[0] = 0x47;
        p[3] = 0x30; // AFC = 3 (AF + payload)
        p[4] = 0x05; // AF length
        assert_eq!(ts_afc(&p), 3);
        assert_eq!(ts_payload_off(&p), Some(4 + 1 + 5));
        p[3] = 0x10; // AFC = 1 (payload only)
        assert_eq!(ts_payload_off(&p), Some(4));
        p[3] = 0x20; // AFC = 2 (AF only)
        assert_eq!(ts_payload_off(&p), None);
    }

    #[test]
    fn pat_with_pointer_field_is_detected() {
        let mut p = [0xFFu8; TS_PKT_SZ];
        p[0] = 0x47;
        p[1] = 0x40;
        p[2] = 0x00;
        p[3] = 0x10;
        p[4] = 0x03; // pointer_field = 3
        p[8] = 0x00; // table_id at payload+1+3
        let mut win = vec![0u8; TS_PKT_SZ * 2];
        win[..TS_PKT_SZ].copy_from_slice(&p);
        assert!(is_pat(&win, 0));
        p[8] = 0x42; // not PAT
        win[..TS_PKT_SZ].copy_from_slice(&p);
        assert!(!is_pat(&win, 0));
    }
}
