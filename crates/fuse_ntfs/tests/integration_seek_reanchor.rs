use fuse_ntfs::StreamBackend;
use fuse_ntfs::contract::TS_PKT_SZ;
use fuse_ntfs::engine::live_engine::LiveEngine;
use fuse_ntfs::engine::ring;

const BLOCK: usize = 65_536;

fn make_stream(n_packets: usize) -> Vec<u8> {
    let mut v = Vec::with_capacity(n_packets * TS_PKT_SZ);
    let mut cc_video = 0u8;
    let mut cc_audio = 0u8;
    for i in 0..n_packets {
        if i % 10 == 0 {
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = 0x40;
            p[2] = 0x00;
            p[3] = 0x10;
            p[4] = 0x00;
            p[5] = 0x00;
            v.extend_from_slice(&p);
        } else if i % 3 == 0 {
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
            p[12] = 0x0A;
            let pts = 900_000u64 + (i as u64) * 3000;
            let mut b = [0u8; 5];
            fuse_ntfs::engine::ts_xform::pts_to_bytes(pts, &mut b);
            p[13..18].copy_from_slice(&b);
            cc_video = cc_video.wrapping_add(1) & 0x0F;
            v.extend_from_slice(&p);
        } else if i % 7 == 0 {
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = 0x41;
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
            p[12] = 0x0A;
            let pts = 900_000u64 + (i as u64) * 3000;
            let mut b = [0u8; 5];
            fuse_ntfs::engine::ts_xform::pts_to_bytes(pts, &mut b);
            p[13..18].copy_from_slice(&b);
            cc_audio = cc_audio.wrapping_add(1) & 0x0F;
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

fn is_null_block(buf: &[u8], abs_off: u64) -> bool {
    let mut expected = vec![0u8; buf.len()];
    ring::fill_null_at(&mut expected, abs_off);
    buf == expected.as_slice()
}

#[test]
fn seek_reanchor_flow() {
    let engine = LiveEngine::new();
    let data = make_stream((20 * 1024 * 1024) / TS_PKT_SZ);
    engine.ring.write_chunk(&data);

    // Phase 1: sequential playback up to foff = 901120.
    let mut buf = vec![0u8; BLOCK];
    // open at 0
    let n = engine.serve_live(0, 0, &mut buf, 0);
    assert_eq!(n, BLOCK);
    assert!(!is_null_block(&buf, 0));

    let mut foff = BLOCK as u64;
    while foff < 901_120 {
        let want = if foff + BLOCK as u64 > 901_120 {
            (901_120 - foff) as usize
        } else {
            BLOCK
        };
        if want == 0 {
            break;
        }
        if buf.len() != want {
            buf.resize(want, 0);
        }
        let n = engine.serve_live(0, foff, &mut buf, 0);
        assert_eq!(n, want);
        assert!(!is_null_block(&buf, foff), "sequential read must serve real data");
        foff += want as u64;
    }

    let anchor_foff_before = engine.anchor.lock().unwrap_or_else(|e| e.into_inner()).base_foff;
    let anchor_spos_before = engine.anchor.lock().unwrap_or_else(|e| e.into_inner()).base_spos;
    let last_fend_before = engine.anchor.lock().unwrap_or_else(|e| e.into_inner()).last_fend;
    let epoch_before = engine.anchor.lock().unwrap_or_else(|e| e.into_inner()).epoch_seq;

    // Phase 2: isolated bookmark probe at 800 MB.
    let probe_size = 65_536usize;
    let probe_off = 799_997_952u64;
    let mut pbuf = vec![0u8; probe_size];
    engine.serve_live(0, probe_off, &mut pbuf, 0);
    assert!(
        is_null_block(&pbuf, probe_off),
        "isolated probe must be NULL TS packets"
    );

    {
        let a = engine.anchor.lock().unwrap_or_else(|e| e.into_inner());
        assert_eq!(a.last_fend, last_fend_before, "chain must not advance");
        assert_eq!(a.base_foff, anchor_foff_before, "anchor foff must not move");
        assert_eq!(a.base_spos, anchor_spos_before, "anchor spos must not move");
        assert_eq!(a.epoch_seq, epoch_before, "epoch must not change");
    }

    // Phase 3: consecutive probe sequence reaching K >= 3.
    let mut pbuf2 = vec![0u8; 4096];
    engine.serve_live(0, probe_off + 4096, &mut pbuf2, 0);

    let mut pbuf3 = vec![0u8; 8192];
    engine.serve_live(0, probe_off + 4096 + 8192, &mut pbuf3, 0);

    {
        let a = engine.anchor.lock().unwrap_or_else(|e| e.into_inner());
        assert!(
            a.epoch_seq > epoch_before,
            "re-anchor to live edge must be confirmed"
        );
    }
    assert!(
        !is_null_block(&pbuf3, probe_off + 4096 + 8192),
        "playback must resume with fresh D12-snapped packets"
    );

    let _ = anchor_foff_before;
}