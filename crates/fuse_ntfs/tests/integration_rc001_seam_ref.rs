//! RC-001 iteration 11 — seam reference must be the latest delivered PES.
//!
//! Hardware observation (iteration 9/10 window): at a Return splice the first
//! post-splice video PTS was 8_702 ticks (96.7 ms) BEHIND the last delivered
//! video PTS, while PCR advanced correctly. Root cause: `served.pts` was the
//! last PES in byte order, and an audio PES (interleaved ~130 ms behind video)
//! ends the block, so the splice shifted the whole timeline from the audio
//! value and video regressed.
//!
//! The contract asserted here: after a splice, the first delivered video PTS is
//! exactly `last_delivered_video_pts + SPLICE_PTS_EPS`, for a block whose final
//! PES is audio.

use fuse_ntfs::contract::TS_PKT_SZ;
use fuse_ntfs::engine::ts_xform::{
    pts_to_bytes, ts_pes_ts, ts_pid, ts_write_pcr, RawSample, TsXformState, PCR_MOD, PTS_MOD,
    SPLICE_PTS_EPS,
};

const VIDEO_PID: u16 = 0x0100;
const AUDIO_PID: u16 = 0x0101;
const PKTS: usize = 300;

fn video_pkt(cc: u8, pts: u64, with_pcr: bool) -> [u8; TS_PKT_SZ] {
    let mut p = [0xFFu8; TS_PKT_SZ];
    p[0] = 0x47;
    p[1] = (((VIDEO_PID >> 8) & 0x1F) as u8) | 0x40;
    p[2] = VIDEO_PID as u8;
    if with_pcr {
        p[3] = 0x30 | (cc & 0x0F);
        p[4] = 0x07;
        p[5] = 0x10;
        ts_write_pcr((pts * 300) % PCR_MOD, &mut p);
    } else {
        p[3] = 0x10 | (cc & 0x0F);
    }
    let off = if with_pcr { 12 } else { 4 };
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
    pts_to_bytes(pts % PTS_MOD, &mut b);
    p[off + 9..off + 14].copy_from_slice(&b);
    p
}

fn audio_pkt(cc: u8, pts: u64) -> [u8; TS_PKT_SZ] {
    let mut p = [0xFFu8; TS_PKT_SZ];
    p[0] = 0x47;
    p[1] = (((AUDIO_PID >> 8) & 0x1F) as u8) | 0x40;
    p[2] = AUDIO_PID as u8;
    p[3] = 0x10 | (cc & 0x0F);
    let off = 4;
    p[off] = 0x00;
    p[off + 1] = 0x00;
    p[off + 2] = 0x01;
    p[off + 3] = 0xC0;
    p[off + 4] = 0x00;
    p[off + 5] = 0x00;
    p[off + 6] = 0x80;
    p[off + 7] = 0x80;
    p[off + 8] = 0x05;
    let mut b = [0u8; 5];
    pts_to_bytes(pts % PTS_MOD, &mut b);
    p[off + 9..off + 14].copy_from_slice(&b);
    p
}

/// Builds a packet-aligned block; `audio_last` puts an audio PES at the very
/// end (the interleave shape observed on the device).
fn block(video_pts: u64, audio_pts: Option<u64>) -> Vec<u8> {
    let mut v = Vec::with_capacity(PKTS * TS_PKT_SZ);
    let mut cc_v = 0u8;
    for i in 0..PKTS {
        if let Some(a) = audio_pts {
            if i == PKTS - 1 {
                v.extend_from_slice(&audio_pkt(0, a));
                continue;
            }
        }
        let with_pcr = i == 0;
        let pts = video_pts + (i as u64) * 3000;
        v.extend_from_slice(&video_pkt(cc_v, pts, with_pcr));
        cc_v = cc_v.wrapping_add(1) & 0x0F;
    }
    v
}

fn last_video_pts(buf: &[u8]) -> u64 {
    let mut out = 0u64;
    for p in buf.chunks_exact(TS_PKT_SZ) {
        if p[0] == 0x47 && ts_pid(p) == VIDEO_PID {
            if let Some((pts, _)) = ts_pes_ts(p) {
                out = pts;
            }
        }
    }
    out
}

fn first_video_pts(buf: &[u8]) -> Option<u64> {
    for p in buf.chunks_exact(TS_PKT_SZ) {
        if p[0] == 0x47 && ts_pid(p) == VIDEO_PID {
            if let Some((pts, _)) = ts_pes_ts(p) {
                return Some(pts);
            }
        }
    }
    None
}

fn no_full(_abs: u64, _pkt: &mut [u8; TS_PKT_SZ]) -> bool {
    false
}

#[test]
fn seam_reference_is_latest_delivered_pes_not_trailing_audio() {
    let mut x = TsXformState::new();

    // One full delivered block whose FINAL PES is audio, 11_702 ticks behind
    // the last video PES (the measured device interleave offset).
    let v0 = 900_000u64;
    let a0 = v0 + (PKTS as u64 - 2) * 3000 - 11_702;
    let old = block(v0, Some(a0));
    let mut buf = old.clone();
    x.apply_block(&mut buf, 0, true, no_full);

    let last_v = last_video_pts(&buf);
    assert_eq!(last_v, v0 + (PKTS as u64 - 2) * 3000);

    // Tier-1 calibration at the delivered end (served state is fresh).
    let old_pos = (PKTS as u64) * TS_PKT_SZ as u64;
    let r = x.calibrate(old_pos, old_pos, &[], 0, 0);
    assert!(r.valid, "served reference must be usable at the delivered end");

    // New anchor: a fresh position whose first raw PES is video at raw_v.
    let raw_v = 5_000_000u64;
    let new = block(raw_v, Some(raw_v - 11_702));
    let raw: RawSample = TsXformState::collect_raw(&new, 0, 0, new.len() as u64);
    assert_eq!(raw.pts, Some(raw_v), "anchor sample starts on video");

    x.begin_splice_epoch(1, &r, &raw);

    let mut out = new.clone();
    x.apply_block(&mut out, old_pos, true, no_full);
    let first_post = first_video_pts(&out).expect("post-splice video PES");

    assert_eq!(
        first_post,
        last_v + SPLICE_PTS_EPS,
        "first post-splice video PTS must continue at last delivered video PTS + EPS \
         (a trailing audio PES must not become the reference)"
    );
    assert!(
        first_post > last_v,
        "video PTS must never regress across a splice (post {first_post}, last {last_v})"
    );
}
