use fuse_ntfs::StreamBackend;
use fuse_ntfs::contract::{TS_PKT_SZ, RESUME_MIN_LEAD};
use fuse_ntfs::engine::live_engine::LiveEngine;
use fuse_ntfs::engine::state_machine::{StateMachine, SmDecision, SmCtx, select_resume_anchor};
use fuse_ntfs::engine::ts_xform::{
    TsXformState, pts_to_bytes, pts_from_bytes, ts_write_pcr, ts_pcr, ts_pes_ts, ts_pid,
    SPLICE_PTS_EPS, PTS_MOD as XPTS_MOD, PCR_MOD,
};

const READ: usize = 16_384;

fn make_stream(base_pts: u64, n_packets: usize) -> Vec<u8> {
    let mut v = Vec::with_capacity(n_packets * TS_PKT_SZ);
    let mut cc_video = 0u8;
    let mut cc_audio = 0u8;
    let mut cc_pat = 0u8;
    for i in 0..n_packets {
        if i % 10 == 0 {
            // PAT
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = 0x40; // PUSI, PID 0x0000
            p[2] = 0x00;
            p[3] = 0x10 | (cc_pat & 0x0F);
            p[4] = 0x00;
            p[5] = 0x00;
            cc_pat = cc_pat.wrapping_add(1) & 0x0F;
            v.extend_from_slice(&p);
        } else if i % 5 == 0 {
            // video PES + PCR
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = 0x41 | 0x40; // PUSI, PID 0x0100
            p[2] = 0x00;
            p[3] = 0x30 | (cc_video & 0x0F); // AFC=3
            p[4] = 0x07;
            p[5] = 0x10;
            let pts = base_pts + (i as u64) * 3000;
            let pcr = pts * 300;
            ts_write_pcr(pcr % PCR_MOD, &mut p);
            let off = 12usize;
            p[off] = 0x00;
            p[off + 1] = 0x00;
            p[off + 2] = 0x01;
            p[off + 3] = 0xE0;
            p[off + 4] = 0x00;
            p[off + 5] = 0x00;
            p[off + 6] = 0x80;
            p[off + 7] = 0x80;
            p[off + 8] = 0x05;
            let mut b = [0u8; 5];
            pts_to_bytes(pts % XPTS_MOD, &mut b);
            p[off + 9..off + 14].copy_from_slice(&b);
            cc_video = cc_video.wrapping_add(1) & 0x0F;
            v.extend_from_slice(&p);
        } else if i % 2 == 0 {
            // video PES
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = 0x41 | 0x40;
            p[2] = 0x00;
            p[3] = 0x10 | (cc_video & 0x0F);
            p[4] = 0x00;
            p[5] = 0x00;
            p[6] = 0x01;
            p[7] = 0xE0;
            p[8] = 0x00;
            p[9] = 0x00;
            p[10] = 0x80;
            p[11] = 0x80;
            p[12] = 0x05;
            let pts = base_pts + (i as u64) * 3000;
            let mut b = [0u8; 5];
            pts_to_bytes(pts % XPTS_MOD, &mut b);
            p[13..18].copy_from_slice(&b);
            cc_video = cc_video.wrapping_add(1) & 0x0F;
            v.extend_from_slice(&p);
        } else if i % 3 == 0 {
            // audio PES (AC-3)
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = 0x41 | 0x40;
            p[2] = 0x01;
            p[3] = 0x10 | (cc_audio & 0x0F);
            p[4] = 0x00;
            p[5] = 0x00;
            p[6] = 0x01;
            p[7] = 0xBD;
            p[8] = 0x00;
            p[9] = 0x00;
            p[10] = 0x80;
            p[11] = 0x80;
            p[12] = 0x05;
            let pts = base_pts + (i as u64) * 3000;
            let mut b = [0u8; 5];
            pts_to_bytes(pts % XPTS_MOD, &mut b);
            p[13..18].copy_from_slice(&b);
            cc_audio = cc_audio.wrapping_add(1) & 0x0F;
            v.extend_from_slice(&p);
        } else {
            // null packet
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = 0x1F;
            p[2] = 0xFF;
            p[3] = 0x10;
            v.extend_from_slice(&p);
        }
    }
    v
}

fn fetch_mem(mem: &[u8]) -> impl FnMut(u64, &mut [u8; TS_PKT_SZ]) -> bool + '_ {
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

fn last_video_pts(buf: &[u8]) -> u64 {
    let mut last = 0u64;
    let mut i = 0usize;
    while i + TS_PKT_SZ <= buf.len() {
        if let Some((pts, _)) = ts_pes_ts(&buf[i..i + TS_PKT_SZ]) {
            let pid = ts_pid(&buf[i..i + TS_PKT_SZ]);
            if pid == 0x0100 {
                last = pts;
            }
        }
        i += TS_PKT_SZ;
    }
    last
}

fn first_video_pts(buf: &[u8]) -> u64 {
    let mut i = 0usize;
    while i + TS_PKT_SZ <= buf.len() {
        if let Some((pts, _)) = ts_pes_ts(&buf[i..i + TS_PKT_SZ]) {
            let pid = ts_pid(&buf[i..i + TS_PKT_SZ]);
            if pid == 0x0100 {
                return pts;
            }
        }
        i += TS_PKT_SZ;
    }
    0
}

fn video_pts_monotonic(buf: &[u8]) -> bool {
    let mut prev = 0u64;
    let mut have = false;
    let mut i = 0usize;
    while i + TS_PKT_SZ <= buf.len() {
        if let Some((pts, _)) = ts_pes_ts(&buf[i..i + TS_PKT_SZ]) {
            let pid = ts_pid(&buf[i..i + TS_PKT_SZ]);
            if pid == 0x0100 {
                if have {
                    let m = XPTS_MOD as i64;
                    let dd = ((pts % XPTS_MOD) as i64 - (prev % XPTS_MOD) as i64 + m / 2 + m) % m - m / 2;
                    if dd <= 0 {
                        return false;
                    }
                }
                prev = pts;
                have = true;
            }
        }
        i += TS_PKT_SZ;
    }
    let _ = prev;
    true
}

fn pcr_monotonic(buf: &[u8]) -> bool {
    let mut prev = 0u64;
    let mut have = false;
    let mut i = 0usize;
    while i + TS_PKT_SZ <= buf.len() {
        if let Some(pcr) = ts_pcr(&buf[i..i + TS_PKT_SZ]) {
            if have {
                let m = (XPTS_MOD * 300) as i64;
                let dd = ((pcr % (XPTS_MOD * 300)) as i64 - (prev % (XPTS_MOD * 300)) as i64 + m / 2 + m) % m - m / 2;
                if dd <= 0 {
                    return false;
                }
            }
            prev = pcr;
            have = true;
        }
        i += TS_PKT_SZ;
    }
    true
}

#[test]
fn scenario1_modular_math() {
    // PTS/PCR roundtrip and modular math.
    assert_eq!(XPTS_MOD, 8_589_934_592);
    assert_eq!(PCR_MOD, 2_576_980_377_600);
    // wrap
    assert_eq!(fuse_ntfs::engine::ts_xform::pts_wrap(XPTS_MOD), 0);
    assert_eq!(fuse_ntfs::engine::ts_xform::pts_wrap(XPTS_MOD + 1234), 1234);
    // signed diff
    assert_eq!(fuse_ntfs::engine::ts_xform::pts_sub(1000, 900), 100);
    assert_eq!(fuse_ntfs::engine::ts_xform::pts_sub(900, 1000), -100);
    assert_eq!(fuse_ntfs::engine::ts_xform::pts_sub(100, XPTS_MOD - 100), 200);
    // roundtrip encoding
    for &v in &[0u64, 1, 12345, 900_000, XPTS_MOD - 1, XPTS_MOD / 2] {
        let mut b = [0u8; 5];
        pts_to_bytes(v, &mut b);
        assert_eq!(pts_from_bytes(&b), v);
    }
    // PCR roundtrip
    let mut p = [0u8; TS_PKT_SZ];
    p[0] = 0x47;
    p[3] = 0x30;
    p[4] = 0x07;
    p[5] = 0x10;
    let v = 27_000_000u64;
    ts_write_pcr(v, &mut p);
    assert_eq!(ts_pcr(&p).unwrap(), v);
    // apply
    assert_eq!(fuse_ntfs::engine::ts_xform::pts_apply(XPTS_MOD - 50, 150), 100);
    let _ = video_pts_monotonic;
}

#[test]
fn scenario2_pure_splice() {
    let a = make_stream(100_000, 1500);
    let b = make_stream(9_000_000, 1500);

    let mut x = TsXformState::new();
    let mut a_buf = a.clone();
    x.apply_block(&mut a_buf, 0, true, fetch_mem(&a));

    let last_pts_a = last_video_pts(&a_buf);
    assert!(last_pts_a > 0);

    let raw = TsXformState::collect_raw(&b, 0, 0, 1024 * 1024);
    let r = x.calibrate(a.len() as u64, a.len() as u64, &b, 0, 1000);
    assert!(r.valid, "calibration must succeed");
    assert!(x.begin_splice_epoch(1, &r, &raw), "splice epoch must connect");

    let mut b_buf = b.clone();
    let b_raw = b.clone();
    x.apply_block(&mut b_buf, 0, true, |abs, pkt| {
        let start = abs as usize;
        if start + TS_PKT_SZ <= b_raw.len() {
            pkt.copy_from_slice(&b_raw[start..start + TS_PKT_SZ]);
            true
        } else {
            false
        }
    });

    let first_new = first_video_pts(&b_buf);
    let expect = (last_pts_a + SPLICE_PTS_EPS) % XPTS_MOD;
    assert_eq!(first_new, expect, "first new PTS must be ref + 3000");

    assert!(video_pts_monotonic(&b_buf), "PTS must be monotonic");
    assert!(pcr_monotonic(&b_buf), "PCR must be monotonic");

    // NULL packets untouched.
    let mut null_diff = 0usize;
    let mut i = 0usize;
    while i + TS_PKT_SZ <= b.len() {
        let pid = ts_pid(&b[i..i + TS_PKT_SZ]);
        if pid == 0x1FFF {
            if b_buf[i..i + TS_PKT_SZ] != b[i..i + TS_PKT_SZ] {
                null_diff += 1;
            }
        }
        i += TS_PKT_SZ;
    }
    assert_eq!(null_diff, 0, "NULL packets must be untouched");
}

#[test]
fn scenario3_return_resume() {
    // StateMachine: play 2 MB sequential, 65s gap, reopen at 1_900_544.
    let mut sm = StateMachine::new();
    sm.note_open(0);
    let s_write = 400 * 1024 * 1024u64;
    let ring_old = s_write - 128 * 1024 * 1024;

    // Play ~2 MB sequential.
    let mut fend = 0u64;
    for i in 0..160u64 {
        let foff = i * 16_384;
        let ctx = SmCtx {
            anchor_valid: true,
            prev_fend: fend,
            probe_pos: None,
            s_write,
            ring_old,
        };
        let _ = sm.evaluate(foff, 16_384, 1000 + i * 100, &ctx);
        fend = foff + 16_384;
    }

    let reopen = 1_900_544u64;
    // Step 1: gap jump.
    let ctx = SmCtx {
        anchor_valid: true,
        prev_fend: fend,
        probe_pos: None,
        s_write,
        ring_old,
    };
    let d0 = sm.evaluate(reopen, 16_384, 66_000, &ctx);
    assert_eq!(d0, SmDecision::Continue);
    assert_eq!(sm.phase(), fuse_ntfs::engine::state_machine::SmPhase::GapWatch);

    // Step 2: chained reads -> ResumeAfterGap on 4th.
    let mut prev = reopen + 16_384;
    let mut decision = SmDecision::Continue;
    for i in 1..4u64 {
        let ctx2 = SmCtx {
            anchor_valid: true,
            prev_fend: prev,
            probe_pos: None,
            s_write,
            ring_old,
        };
        decision = sm.evaluate(prev, 16_384, 66_000 + i * 100, &ctx2);
        prev += 16_384;
    }
    assert_eq!(decision, SmDecision::ResumeAfterGap);

    // Position preserved via select_resume_anchor.
    let mapped = s_write - 40 * 1024 * 1024;
    let anchor = select_resume_anchor(mapped, ring_old, s_write);
    assert!(anchor.is_some());
    let a = anchor.unwrap();
    assert!(a >= ring_old);
    assert!(s_write - a >= RESUME_MIN_LEAD || s_write <= RESUME_MIN_LEAD);

    // Caminho C splice epoch connects timeline seamlessly.
    let a_stream = make_stream(100_000, 800);
    let b_stream = make_stream(9_000_000, 800);
    let mut x = TsXformState::new();
    let mut a_buf = a_stream.clone();
    x.apply_block(&mut a_buf, 0, true, fetch_mem(&a_stream));
    let last_pts_a = last_video_pts(&a_buf);
    let raw = TsXformState::collect_raw(&b_stream, 0, 0, 512 * 1024);
    let r = x.calibrate(a_stream.len() as u64, a_stream.len() as u64, &b_stream, 0, 1000);
    assert!(r.valid);
    assert!(x.begin_splice_epoch(7, &r, &raw));
    let mut b_buf = b_stream.clone();
    let b_raw = b_stream.clone();
    x.apply_block(&mut b_buf, 0, true, |abs, pkt| {
        let start = abs as usize;
        if start + TS_PKT_SZ <= b_raw.len() {
            pkt.copy_from_slice(&b_raw[start..start + TS_PKT_SZ]);
            true
        } else {
            false
        }
    });
    let first_new = first_video_pts(&b_buf);
    assert_eq!(first_new, (last_pts_a + SPLICE_PTS_EPS) % XPTS_MOD);
}

#[test]
fn scenario4_determinism() {
    let engine = LiveEngine::new();
    let data = make_stream(100_000, 2000);
    engine.ring.write_chunk(&data);

    let mut buf1 = vec![0u8; READ];
    let mut buf2 = vec![0u8; READ];
    engine.serve_live(0, 0, &mut buf1, 0);
    engine.serve_live(0, 0, &mut buf2, 0);
    assert_eq!(buf1, buf2, "re-reading same range must be byte-identical");

    // TsXform determinism too.
    let a = make_stream(100_000, 200);
    let b = make_stream(9_000_000, 200);
    let mut x1 = TsXformState::new();
    let mut x2 = TsXformState::new();
    let mut a1 = a.clone();
    let mut a2 = a.clone();
    x1.apply_block(&mut a1, 0, true, fetch_mem(&a));
    x2.apply_block(&mut a2, 0, true, fetch_mem(&a));
    let raw1 = TsXformState::collect_raw(&b, 0, 0, 1024 * 1024);
    let raw2 = TsXformState::collect_raw(&b, 0, 0, 1024 * 1024);
    let r1 = x1.calibrate(a.len() as u64, a.len() as u64, &b, 0, 1000);
    let r2 = x2.calibrate(a.len() as u64, a.len() as u64, &b, 0, 1000);
    x1.begin_splice_epoch(1, &r1, &raw1);
    x2.begin_splice_epoch(1, &r2, &raw2);
    let mut b1 = b.clone();
    let mut b2b = b.clone();
    let braw = b.clone();
    x1.apply_block(&mut b1, 0, true, |abs, pkt| {
        let s = abs as usize;
        if s + TS_PKT_SZ <= braw.len() {
            pkt.copy_from_slice(&braw[s..s + TS_PKT_SZ]);
            true
        } else {
            false
        }
    });
    x2.apply_block(&mut b2b, 0, true, |abs, pkt| {
        let s = abs as usize;
        if s + TS_PKT_SZ <= braw.len() {
            pkt.copy_from_slice(&braw[s..s + TS_PKT_SZ]);
            true
        } else {
            false
        }
    });
    assert_eq!(b1, b2b, "TsXform output must be deterministic");
}

