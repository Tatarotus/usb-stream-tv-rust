//! RC-001 ITERATION 8 - host-only diagnosis: Return / reopen behaviour.
//!
//! Physical observation: the operator played Live TV, pressed Return, waited
//! ~20 s, reopened the file -> the TV jumped back to the FIRST FRAME and froze
//! after ~5 s. FUSE telemetry showed reanchor reason=restart at foff=0
//! (mapped_spos=114534206) and later at foff=2228176.
//!
//! This test reproduces "the TV reopens the file and reads at foff = 0" on the
//! real engine, with a deterministic MPEG-TS fixture and a producer that keeps
//! advancing the live edge (as ffmpeg/FIFO do in production). It measures what
//! the engine actually returns.

use std::sync::Arc;

use fuse_ntfs::contract::StreamBackend;
use fuse_ntfs::contract::{LEADBACK, TS_PKT_SZ};
use fuse_ntfs::engine::live_engine::LiveEngine;
use fuse_ntfs::engine::ts_xform::{pts_to_bytes, ts_pes_ts, ts_pid, PTS_MOD};

const BLOCK: usize = 65_536;
const TV_FOFF: u64 = 17_800_000; // physical live_read_progress foff

fn make_ts_stream(base_pts: u64, n_packets: usize) -> Vec<u8> {
    let mut v = Vec::with_capacity(n_packets * TS_PKT_SZ);
    let mut cc_video = 0u8;
    let mut cc_pat = 0u8;
    for i in 0..n_packets {
        if i % 10 == 0 {
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = 0x40;
            p[2] = 0x00;
            p[3] = 0x10 | (cc_pat & 0x0F);
            cc_pat = cc_pat.wrapping_add(1) & 0x0F;
            v.extend_from_slice(&p);
        } else if i % 5 == 0 {
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = 0x41;
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
            let pts = (base_pts + (i as u64) * 3000) % PTS_MOD;
            let mut b = [0u8; 5];
            pts_to_bytes(pts, &mut b);
            p[13..18].copy_from_slice(&b);
            cc_video = cc_video.wrapping_add(1) & 0x0F;
            v.extend_from_slice(&p);
        } else {
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

fn count_pat(buf: &[u8], foff: u64) -> usize {
    let phase = (foff % TS_PKT_SZ as u64) as usize;
    let mut p = if phase == 0 { 0 } else { TS_PKT_SZ - phase };
    let mut n = 0;
    while p + TS_PKT_SZ <= buf.len() {
        let pkt = &buf[p..p + TS_PKT_SZ];
        if pkt[0] == 0x47 && ts_pid(pkt) == 0x0000 {
            n += 1;
        }
        p += TS_PKT_SZ;
    }
    n
}

fn video_pts(buf: &[u8], foff: u64) -> Vec<u64> {
    let phase = (foff % TS_PKT_SZ as u64) as usize;
    let mut p = if phase == 0 { 0 } else { TS_PKT_SZ - phase };
    let mut out = Vec::new();
    while p + TS_PKT_SZ <= buf.len() {
        let pkt = &buf[p..p + TS_PKT_SZ];
        if pkt[0] == 0x47 && ts_pid(pkt) == 0x0100 {
            if let Some((pts, _)) = ts_pes_ts(pkt) {
                out.push(pts);
            }
        }
        p += TS_PKT_SZ;
    }
    out
}

#[test]
fn rc001_return_at_zero_is_served_mid_stream_not_from_offset_zero() {
    let engine = Arc::new(LiveEngine::new());
    let fixture = make_ts_stream(1_000_000, (64 * 1024 * 1024) / TS_PKT_SZ);
    let mut written = 0usize;
    let mut buf = vec![0u8; BLOCK];

    // Producer pre-buffer: keep ~24 MiB in front of the reader (as in production,
    // where ffmpeg/FIFO keep the live edge ahead of the TV's read position).
    for _ in 0..384 {
        engine.ring.write_chunk(&fixture[written..written + BLOCK]);
        written += BLOCK;
    }

    // Sequential playback up to the physical foff, producer advancing 1:1.
    let mut foff = 0u64;
    let mut last_pts_before = 0u64;
    while foff < TV_FOFF {
        engine.ring.write_chunk(&fixture[written..written + BLOCK]);
        written += BLOCK;
        let n = engine.serve_live(0, foff, &mut buf, 1000);
        assert_eq!(n, BLOCK);
        if let Some(&p) = video_pts(&buf, foff).last() {
            last_pts_before = p;
        }
        foff += BLOCK as u64;
    }

    let (epoch_before, fend_before, spos_before) = {
        let a = engine.anchor.lock().unwrap_or_else(|e| e.into_inner());
        (a.epoch_seq, a.last_fend, a.base_spos + (foff - a.base_foff))
    };
    let s_write_before = engine.ring.lock().s_write();

    // --- Return: the TV reopens the file and issues its first read at foff = 0.
    let mut ret_buf = vec![0u8; 16_384];
    let n = engine.serve_live(0, 0, &mut ret_buf, 1000);
    assert_eq!(n, 16_384);

    let (epoch_after, base_foff_after, base_spos_after) = {
        let a = engine.anchor.lock().unwrap_or_else(|e| e.into_inner());
        (a.epoch_seq, a.base_foff, a.base_spos)
    };
    let s_write_after = engine.ring.lock().s_write();
    let leadback_used = s_write_after.saturating_sub(base_spos_after);

    let pat = count_pat(&ret_buf, 0);
    let pts = video_pts(&ret_buf, 0);
    let pts_delta = pts
        .first()
        .map(|&p| p as i64 - last_pts_before as i64)
        .unwrap_or(0);
    let served_vs_tv_position = base_spos_after as i64 - spos_before as i64;

    println!(
        "\n[DIAG-R] restart-at-zero reanchor\n\
         [DIAG-R]   before: epoch={epoch_before} last_fend={fend_before} tv_stream_pos={spos_before} s_write={s_write_before} last_video_pts={last_pts_before}\n\
         [DIAG-R]   after : epoch={epoch_after} base_foff={base_foff_after} base_spos={base_spos_after} s_write={s_write_after} leadback_used={leadback_used}\n\
         [DIAG-R]   served foff=0 from stream position {base_spos_after} (offset 0 would be 0); delta vs the TV's last position: {served_vs_tv_position} B\n\
         [DIAG-R]   first block: PAT_packets={pat} video_PTS={pts:?} pts_delta_vs_before={pts_delta} (90 kHz ticks)"
    );

    // FACTS established by this reproduction:
    // 1. A first read at foff=0 after playback is treated as a restart -> new epoch.
    assert!(
        epoch_after > epoch_before,
        "restart must publish a new epoch ({epoch_after} vs {epoch_before})"
    );
    // 2. The anchor is the LIVE EDGE minus the design cushion, never stream offset 0.
    assert_eq!(base_foff_after, 0);
    assert!(
        base_spos_after > 0,
        "foff=0 must not be served from stream position 0 on a restart"
    );
    let slack = TS_PKT_SZ as u64 * 2;
    assert!(
        leadback_used >= LEADBACK && leadback_used <= LEADBACK + slack,
        "restart anchors at s_write - LEADBACK (got {leadback_used}, expected ~{LEADBACK})"
    );
    // 3. The slice is real video for a ring-resident position (not NULLs).
    assert!(
        !pts.is_empty(),
        "restart must deliver real video for a ring-resident position"
    );
    // 4. The delivered slice carries PAT packets, so decoder re-init is possible.
    assert!(pat > 0, "the delivered slice should contain PAT packets");
}