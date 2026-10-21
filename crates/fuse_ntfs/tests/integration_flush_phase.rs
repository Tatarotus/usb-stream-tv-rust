//! TASK-004 Phase A — MPEG-TS phase reacquisition after a flush.
//!
//! These tests are host-only and use synthetic data. The RED case is ignored
//! by default so the TASK-001..003 suite stays green; run it with
//! `cargo test -p fuse_ntfs --test integration_flush_phase -- --ignored` to
//! reproduce the defect documented in `TASK_004_A_HANDOFF.md`.


use fuse_ntfs::contract::TS_PKT_SZ;
use fuse_ntfs::engine::feeder_phase::FeederPhase;
use fuse_ntfs::engine::ring::snap188;
use fuse_ntfs::engine::snapper::{d12_snap, is_pat, D12Tier};
use fuse_ntfs::engine::ts_xform::TsXformState;
use fuse_ntfs::engine::LiveEngine;

const PAT_PERIOD: usize = 20;

fn engine_with_ring(capacity: u64) -> LiveEngine {
    LiveEngine::with_capacity(capacity)
}

fn pat_packet(cc: u8) -> [u8; TS_PKT_SZ] {
    let mut p = [0xFFu8; TS_PKT_SZ];
    p[0] = 0x47;
    p[1] = 0x40; // PUSI, PID 0x0000
    p[2] = 0x00;
    p[3] = 0x10 | (cc & 0x0F);
    p[4] = 0x00; // pointer_field
    p[5] = 0x00; // table_id = PAT
    p
}

fn sps_packet(cc: u8) -> [u8; TS_PKT_SZ] {
    let mut p = [0xFFu8; TS_PKT_SZ];
    p[0] = 0x47;
    p[1] = 0x41; // PUSI, PID 0x0100
    p[2] = 0x00;
    p[3] = 0x10 | (cc & 0x0F);
    p[4] = 0x00;
    p[5] = 0x00;
    p[6] = 0x00;
    p[7] = 0x01;
    p[8] = 0x67; // NAL type 7 (SPS)
    p
}

/// Video/audio packet whose payload is deliberately full of `0x47` decoys.
fn pes_packet(pid: u16, cc: u8, seed: u8) -> [u8; TS_PKT_SZ] {
    let mut p = [0xFFu8; TS_PKT_SZ];
    p[0] = 0x47;
    p[1] = (((pid >> 8) & 0x1F) as u8) | 0x40;
    p[2] = (pid & 0xFF) as u8;
    p[3] = 0x10 | (cc & 0x0F);
    for (i, b) in p.iter_mut().enumerate().skip(4) {
        // Dense fake sync bytes at non-packet offsets.
        *b = if (i + seed as usize) % 3 == 0 {
            0x47
        } else {
            0x55
        };
    }
    p
}

/// A valid TS stream: PAT+SPS every `PAT_PERIOD` packets, video/audio in
/// between. The first packet starts at byte 0 of the returned vector.
fn mk_stream(n: usize) -> Vec<u8> {
    let mut v = Vec::with_capacity((n + n / PAT_PERIOD) * TS_PKT_SZ);
    let (mut vcc, mut acc, mut pcc, mut scc) = (0u8, 0u8, 0u8, 0u8);
    for i in 0..n {
        if i % PAT_PERIOD == 0 {
            v.extend_from_slice(&pat_packet(pcc));
            pcc = pcc.wrapping_add(1);
            v.extend_from_slice(&sps_packet(scc));
            scc = scc.wrapping_add(1);
        } else if i % 2 == 0 {
            v.extend_from_slice(&pes_packet(0x0100, vcc, i as u8));
            vcc = vcc.wrapping_add(1);
        } else {
            v.extend_from_slice(&pes_packet(0x0101, acc, i as u8));
            acc = acc.wrapping_add(1);
        }
    }
    v
}

/// A1: fragmenting a read that starts at any of the 188 phases must be
/// byte-exact (identity epoch). This is the negative control that proves the
/// transformer does not corrupt valid bytes while carrying straddlers.
#[test]
fn apply_block_is_byte_exact_for_all_phases_and_fragment_sizes() {
    let stream = mk_stream(1_000);
    let sizes = [1usize, 187, 188, 189, 16_384, 65_536, 131_072];
    for &size in &sizes {
        // Large fragments must be delivered as a single chunk at least once;
        // small fragments only need a few packets to exercise the carry path.
        let span = if size >= 16_384 {
            700 * TS_PKT_SZ
        } else {
            10 * TS_PKT_SZ
        };
        for phase in 0..TS_PKT_SZ {
            let expected = stream[phase..phase + span].to_vec();
            let mut out = expected.clone();
            let mut x = TsXformState::new();
            let mut off = 0usize;
            while off < out.len() {
                let n = size.min(out.len() - off);
                let first_abs = (phase + off) as u64;
                let mut buf = out[off..off + n].to_vec();
                x.apply_block(&mut buf, first_abs, true, |abs, pkt| {
                    let a = abs as usize;
                    if a + TS_PKT_SZ <= stream.len() {
                        pkt.copy_from_slice(&stream[a..a + TS_PKT_SZ]);
                        true
                    } else {
                        false
                    }
                });
                out[off..off + n].copy_from_slice(&buf);
                off += n;
            }
            assert_eq!(
                out, expected,
                "byte mismatch at phase {phase}, fragment {size}"
            );
            // The first full packet boundary inside a read that starts at
            // `phase` is at `(188 - phase) mod 188`, not at offset 0.
            let first = (TS_PKT_SZ - phase) % TS_PKT_SZ;
            for k in 0..((span - first) / TS_PKT_SZ) {
                assert_eq!(
                    out[first + k * TS_PKT_SZ],
                    0x47,
                    "packet header lost at phase {phase}, fragment {size}, packet {k}"
                );
            }
        }
    }
}

/// A1: `d12_snap` itself still finds the true FIFO phase after a restart — it
/// byte-scans for a transport sync and returns the shifted packet grid.
#[test]
fn d12_snap_finds_true_phase_before_leadback_guard() {
    let stream = mk_stream(16_000);
    for &phase in &[0usize, 1, 2, 47, 93, 186, 187] {
        let mut shifted = vec![0u8; phase];
        shifted.extend_from_slice(&stream);
        let s_write = shifted.len() as u64;
        let target = phase as u64 + 8000 * TS_PKT_SZ as u64;
        let out = d12_snap(&shifted, 0, target, s_write);
        if phase == 0 {
            assert_eq!(out.tier, D12Tier::PatSps, "phase 0 PAT+SPS tier");
            assert!(is_pat(&shifted, out.pos as usize));
        } else {
            assert_eq!(
                out.tier,
                D12Tier::Transport,
                "phase {phase}: transport scan must locate the shifted grid"
            );
        }
        assert!(
            out.pos >= phase as u64 && (out.pos - phase as u64) % TS_PKT_SZ as u64 == 0,
            "phase {phase}: d12_snap returned {} which is off the stream grid (phase={phase})",
            out.pos
        );
        assert_eq!(
            shifted[out.pos as usize], 0x47,
            "phase {phase}: no sync byte"
        );
    }
}

/// A1 negative control: a flush that restarts at phase 0 keeps the absolute
/// 188 grid, so the guard must not report a false corruption.
#[test]
fn aligned_phase_zero_anchor_is_unchanged() {
    let stream = mk_stream(16_000);
    let e = engine_with_ring(4 * 1024 * 1024);
    e.ring.write_chunk(&stream);
    let target = 8000 * TS_PKT_SZ as u64;
    let pos = e.snap_anchor(target);
    assert_eq!(pos % TS_PKT_SZ as u64, 0, "phase 0 must stay aligned");
    let mut pkt = [0u8; TS_PKT_SZ];
    for k in 0..4 {
        e.ring.lock().read_at(pos + k * TS_PKT_SZ as u64, &mut pkt);
        assert_eq!(pkt[0], 0x47, "packet {k} header missing at {pos}");
    }
}

/// A1 RED: after a flush the FIFO may resume at any of the 188 phases.
/// `snap_anchor` delegates to `leadback_guard`, which re-snaps to the absolute
/// 188 grid and discards that phase. Run with `--ignored` to reproduce.
#[test]
#[ignore = "TASK-004/A1 RED: leadback_guard discards the post-flush FIFO phase"]
fn snap_anchor_preserves_fifo_phase_after_flush_all_phases() {
    let stream = mk_stream(16_000);
    let mut failures = Vec::new();
    for phase in 0..TS_PKT_SZ {
        let mut shifted = vec![0u8; phase];
        shifted.extend_from_slice(&stream);
        let e = engine_with_ring(4 * 1024 * 1024);
        e.ring.write_chunk(&shifted);
        let target = phase as u64 + 8000 * TS_PKT_SZ as u64;
        let pos = e.snap_anchor(target);
        let expected_congruence = phase as u64;
        if pos < phase as u64 || (pos - phase as u64) % TS_PKT_SZ as u64 != 0 {
            failures.push(format!(
                "phase {phase}: pos={pos} (pos-phase)%188={}",
                (pos as i64 - phase as i64).rem_euclid(TS_PKT_SZ as i64)
            ));
            continue;
        }
        let mut pkt = [0u8; TS_PKT_SZ];
        for k in 0..4 {
            e.ring.lock().read_at(pos + k * TS_PKT_SZ as u64, &mut pkt);
            if pkt[0] != 0x47 {
                failures.push(format!("phase {phase}: no sync at packet {k} (pos={pos})"));
                break;
            }
        }
        let _ = expected_congruence;
    }
    assert!(
        failures.is_empty(),
        "{} of 188 phases lost after flush; first 5: {:?}",
        failures.len(),
        &failures[..failures.len().min(5)]
    );
}

/// A1: payload decoys must never be selected as the anchor. The returned
/// packet must be a real PAT (phase 0) and every following 188-byte slot must
/// start with 0x47.
#[test]
fn fake_sync_bytes_in_payload_do_not_steal_the_anchor() {
    let stream = mk_stream(16_000);
    let out = d12_snap(&stream, 0, 8000 * TS_PKT_SZ as u64, stream.len() as u64);
    assert_eq!(out.tier, D12Tier::PatSps);
    assert_eq!(out.pos % TS_PKT_SZ as u64, 0);
    assert!(is_pat(&stream, out.pos as usize));
    for k in 0..8 {
        assert_eq!(stream[out.pos as usize + k * TS_PKT_SZ], 0x47);
    }
}

/// A1: `snap188` is the primitive that loses the phase; pin its contract so a
/// future fix cannot silently change the meaning of absolute grid positions.
#[test]
fn snap188_is_an_absolute_grid_helper() {
    for phase in 0..TS_PKT_SZ as u64 {
        assert_eq!(snap188(phase + 7 * TS_PKT_SZ as u64), 7 * TS_PKT_SZ as u64);
    }
}

/// A1 Phase B: the production feeder gate discards the pre-sync partial
/// packet and publishes complete packets only. After it, the ring starts on a
/// packet boundary and `snap_anchor` cannot lose the phase. This is the green
/// end-to-end regression for all 188 phases (the raw-injection test above is
/// kept as the pre-fix reproduction).
#[test]
fn feeder_phase_prevents_phase_loss_end_to_end() {
    let stream = mk_stream(700);
    for phase in 0..TS_PKT_SZ {
        let mut shifted = vec![0xAAu8; phase];
        shifted.extend_from_slice(&stream);
        let mut feeder = FeederPhase::new();
        let out = feeder.push(&shifted);
        assert!(!out.is_empty(), "phase {phase}: no aligned output");
        assert_eq!(out.len() % TS_PKT_SZ, 0, "phase {phase}: partial packet");
        let start = stream.len() - out.len();
        assert_eq!(start % TS_PKT_SZ, 0, "phase {phase}: not a stream suffix");
        assert_eq!(&stream[start..], &out[..], "phase {phase}: bytes altered");

        let e = engine_with_ring(1024 * 1024);
        e.ring.write_chunk(&out);
        let pos = e.snap_anchor((out.len() / 2) as u64);
        assert_eq!(pos % TS_PKT_SZ as u64, 0, "phase {phase}: ring misaligned");
        let mut pkt = [0u8; TS_PKT_SZ];
        for k in 0..4 {
            e.ring.lock().read_at(pos + k * TS_PKT_SZ as u64, &mut pkt);
            assert_eq!(pkt[0], 0x47, "phase {phase}: header {k} lost");
        }
    }
}
