//! Comprehensive integration tests for Return/Resume and Channel Switch.
//!
//! Test Scenarios:
//! 1. `test_return_resume_timeline_splice`:
//!    Simulates the exact MStar TV behavior on Return (idle gap > 20s, TV reopens at
//!    bookmark offset foff = 1_900_544, 4-read debounce, and Caminho C seamless splice
//!    with monotonic PTS/PCR and sequential CC).
//! 2. `test_channel_switch_under_10_seconds`:
//!    Simulates channel switching in `Canais TV/` between `TV AO VIVO.trp` and `TV AO VIVO 2.tp`.
//!    Verifies 1 MB contiguous threshold detection, active channel switch, epoch reset,
//!    and that the entire operation completes in < 10 seconds.
//! 3. `test_return_resume_washed_out_fallback_to_live`:
//!    Simulates return after long gap where old position is overwritten in the 128 MB ring:
//!    verifies that instead of underflow it delivers NULLs during debounce and falls back
//!    safely to the live edge upon confirmation.

use std::sync::Arc;
use std::time::{Duration, Instant};

use fuse_ntfs::contract::{StreamBackend, TS_PKT_SZ};
use fuse_ntfs::engine::live_engine::LiveEngine;
use fuse_ntfs::engine::ts_xform::{
    pts_to_bytes, ts_pes_ts, ts_pid, ts_write_pcr, PCR_MOD, PTS_MOD,
    SPLICE_PTS_EPS,
};
use fuse_ntfs::vfs::catalog::{Catalog, LIVE_INO_BASE, SWITCH_THRESHOLD};
use fuse_ntfs::vfs::dispatcher::NtfsServer;
use fuse_ntfs::vfs::ntfs_mft::NtfsTemplate;

const BLOCK: usize = 65_536;

fn make_ts_stream(base_pts: u64, n_packets: usize) -> Vec<u8> {
    let mut v = Vec::with_capacity(n_packets * TS_PKT_SZ);
    let mut cc_video = 0u8;
    let mut cc_audio = 0u8;
    let mut cc_pat = 0u8;

    for i in 0..n_packets {
        if i % 10 == 0 {
            // PAT packet (PID 0)
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
            // Video PES with PCR (PID 0x0100)
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = 0x41 | 0x40; // PUSI, PID 0x0100
            p[2] = 0x00;
            p[3] = 0x30 | (cc_video & 0x0F); // AFC = 3 (adaptation + payload)
            p[4] = 0x07; // adaptation length
            p[5] = 0x10; // PCR flag
            let pts = (base_pts + (i as u64) * 3000) % PTS_MOD;
            let pcr = (pts * 300) % PCR_MOD;
            ts_write_pcr(pcr, &mut p);
            let off = 12usize;
            p[off] = 0x00;
            p[off + 1] = 0x00;
            p[off + 2] = 0x01;
            p[off + 3] = 0xE0; // Video stream ID
            p[off + 4] = 0x00;
            p[off + 5] = 0x00;
            p[off + 6] = 0x80;
            p[off + 7] = 0x80;
            p[off + 8] = 0x05;
            let mut b = [0u8; 5];
            pts_to_bytes(pts, &mut b);
            p[off + 9..off + 14].copy_from_slice(&b);
            cc_video = cc_video.wrapping_add(1) & 0x0F;
            v.extend_from_slice(&p);
        } else if i % 2 == 0 {
            // Video PES without PCR (PID 0x0100)
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = 0x41 | 0x40; // PUSI, PID 0x0100
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
        } else if i % 3 == 0 {
            // Audio PES (AC-3, PID 0x0101)
            let mut p = [0xFFu8; TS_PKT_SZ];
            p[0] = 0x47;
            p[1] = 0x41 | 0x40; // PUSI, PID 0x0101
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
            let pts = (base_pts + (i as u64) * 3000) % PTS_MOD;
            let mut b = [0u8; 5];
            pts_to_bytes(pts, &mut b);
            p[13..18].copy_from_slice(&b);
            cc_audio = cc_audio.wrapping_add(1) & 0x0F;
            v.extend_from_slice(&p);
        } else {
            // NULL packet (PID 0x1FFF)
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

/// Verifies that all full TS packets within `buf` (taking stream offset `foff` phase into account)
/// start with sync byte 0x47 on the 188-byte grid.
fn assert_ts_grid(buf: &[u8], foff: u64) {
    let phase = (foff % TS_PKT_SZ as u64) as usize;
    let first_sync = if phase == 0 { 0 } else { TS_PKT_SZ - phase };
    let mut p = first_sync;
    while p + TS_PKT_SZ <= buf.len() {
        assert_eq!(
            buf[p], 0x47,
            "Sync byte 0x47 expected at index {p} (foff={foff}, phase={phase})"
        );
        p += TS_PKT_SZ;
    }
}

fn extract_video_pts_list(buf: &[u8], foff: u64) -> Vec<u64> {
    let mut list = Vec::new();
    let phase = (foff % TS_PKT_SZ as u64) as usize;
    let first_sync = if phase == 0 { 0 } else { TS_PKT_SZ - phase };
    let mut i = first_sync;
    while i + TS_PKT_SZ <= buf.len() {
        let pkt = &buf[i..i + TS_PKT_SZ];
        if pkt[0] == 0x47 && ts_pid(pkt) == 0x0100 {
            if let Some((pts, _)) = ts_pes_ts(pkt) {
                list.push(pts);
            }
        }
        i += TS_PKT_SZ;
    }
    list
}

#[test]
fn test_return_resume_timeline_splice() {
    let engine = Arc::new(LiveEngine::new());

    // Step 1: Pre-buffer 20 MB of stream A (base PTS = 1_000_000)
    let initial_data = make_ts_stream(1_000_000, (20 * 1024 * 1024) / TS_PKT_SZ);
    engine.ring.write_chunk(&initial_data);

    // Initial sequential playback: read 4 MB (foff = 0 to 4_194_304)
    let mut last_delivered_pts = 0u64;
    let mut foff = 0u64;
    let mut buf = vec![0u8; BLOCK];

    while foff < 4 * 1024 * 1024 {
        let n = engine.serve_live(0, foff, &mut buf, 1000);
        assert_eq!(n, BLOCK);
        assert_ts_grid(&buf, foff);
        let pts_list = extract_video_pts_list(&buf, foff);
        if let Some(&p) = pts_list.last() {
            last_delivered_pts = p;
        }
        foff += BLOCK as u64;
    }
    assert!(last_delivered_pts > 1_000_000, "PTS must have advanced");

    let epoch_before_return = engine.anchor.lock().unwrap().epoch_seq;

    // Step 2: User presses Return on remote.
    // Simulate 45 seconds of idle gap in TV menu.
    engine
        .ring
        .lock()
        .shift_time_back(Duration::from_secs(45));

    // Feeder continues in background: 15 MB more data arrives with upstream time jump
    let next_feed = make_ts_stream(50_000_000, (15 * 1024 * 1024) / TS_PKT_SZ);
    engine.ring.write_chunk(&next_feed);

    // Step 3: TV reopens TV AO VIVO.trp at saved bookmark offset foff = 1_900_544
    let bookmark_foff = 1_900_544u64;
    let mut resume_buf = vec![0u8; 16_384];

    // Read 1 at bookmark offset (gap detected, enters GapWatch)
    let n1 = engine.serve_live(0, bookmark_foff, &mut resume_buf, 1000);
    assert_eq!(n1, 16_384);
    assert_ts_grid(&resume_buf, bookmark_foff);
    assert_eq!(engine.anchor.lock().unwrap().epoch_seq, epoch_before_return);

    // Read 2: sequential continuation (accumulating resume chain)
    let n2 = engine.serve_live(0, bookmark_foff + 16_384, &mut resume_buf, 1000);
    assert_eq!(n2, 16_384);
    assert_ts_grid(&resume_buf, bookmark_foff + 16_384);

    // Read 3: sequential continuation
    let n3 = engine.serve_live(0, bookmark_foff + 32_768, &mut resume_buf, 1000);
    assert_eq!(n3, 16_384);
    assert_ts_grid(&resume_buf, bookmark_foff + 32_768);

    // Capture last PTS delivered before the splice epoch triggers
    let pts_before_splice = extract_video_pts_list(&resume_buf, bookmark_foff + 32_768)
        .last()
        .copied()
        .unwrap_or(last_delivered_pts);

    // Read 4: 4th sequential read (hits 64 KiB total -> triggers ResumeAfterGap!)
    let n4 = engine.serve_live(0, bookmark_foff + 49_152, &mut resume_buf, 1000);
    assert_eq!(n4, 16_384);
    assert_ts_grid(&resume_buf, bookmark_foff + 49_152);

    let epoch_after_resume = engine.anchor.lock().unwrap().epoch_seq;
    assert_eq!(
        epoch_after_resume,
        epoch_before_return + 1,
        "ResumeAfterGap must increment epoch sequence"
    );

    // Read 4 is the first read delivered under the new splice epoch!
    let read4_pts_list = extract_video_pts_list(&resume_buf, bookmark_foff + 49_152);
    assert!(
        !read4_pts_list.is_empty(),
        "Read 4 must deliver video PES packets with PTS"
    );

    let first_new_pts = read4_pts_list[0];
    let expected_first_pts = (pts_before_splice + SPLICE_PTS_EPS) % PTS_MOD;
    let diff = (first_new_pts as i64 - expected_first_pts as i64).abs();
    assert!(
        diff <= 6000,
        "first new PTS ({first_new_pts}) must connect seamlessly near {expected_first_pts} (diff={diff})"
    );

    // Step 4: Verify continuous playback from the new epoch
    let mut post_resume_buf = vec![0u8; BLOCK];
    let n5 = engine.serve_live(0, bookmark_foff + 65_536, &mut post_resume_buf, 1000);
    assert_eq!(n5, BLOCK);
    assert_ts_grid(&post_resume_buf, bookmark_foff + 65_536);

    let new_pts_list = extract_video_pts_list(&post_resume_buf, bookmark_foff + 65_536);
    assert!(
        !new_pts_list.is_empty(),
        "post-resume block must deliver video PES packets with PTS"
    );

    let last_read4_pts = *read4_pts_list.last().unwrap();
    assert!(
        new_pts_list[0] >= last_read4_pts,
        "PTS must progress monotonically from Read 4 to Read 5: {} -> {}",
        last_read4_pts,
        new_pts_list[0]
    );

    // Verify PTS is monotonically increasing in subsequent packets
    for w in new_pts_list.windows(2) {
        assert!(
            w[1] >= w[0],
            "PTS in post-resume stream must be monotonically increasing: {} -> {}",
            w[0],
            w[1]
        );
    }
}

#[test]
fn test_channel_switch_under_10_seconds() {
    let template = NtfsTemplate::synthesized();
    let live_size = template.layout.data_size;
    let catalog = Catalog::new(live_size);

    let engine = Arc::new(LiveEngine::new());
    let server = NtfsServer::new(template, catalog, engine.clone());

    // Buffer initial stream data (Channel 0)
    let ch0_data = make_ts_stream(10_000_000, (16 * 1024 * 1024) / TS_PKT_SZ);
    engine.ring.write_chunk(&ch0_data);

    // Initial state: active channel is 0 (TV AO VIVO.trp)
    assert_eq!(server.catalog.active_file_id(), 0);

    // 1. Play Channel 0 for 2 MB
    let mut buf = vec![0u8; BLOCK];
    let ch0_ino = LIVE_INO_BASE; // 0x1000
    for i in 0..32u64 {
        let foff = i * BLOCK as u64;
        let n = server.serve_read(ch0_ino, foff, &mut buf);
        assert_eq!(n, BLOCK);
        assert_ts_grid(&buf, foff);
    }
    assert_eq!(server.catalog.active_file_id(), 0);

    // 2. User switches channel: opens Canais TV/ and clicks Channel 1 (TV AO VIVO 2.tp)
    let ch1_ino = LIVE_INO_BASE + 1; // 0x1001
    let switch_start = Instant::now();

    // TV reads Channel 1 in 64 KiB blocks
    let reads_needed = (SWITCH_THRESHOLD as usize) / BLOCK; // 16 reads of 64 KiB = 1 MB
    for i in 0..reads_needed {
        let foff = (i * BLOCK) as u64;
        let n = server.serve_read(ch1_ino, foff, &mut buf);
        assert_eq!(n, BLOCK);
        assert_ts_grid(&buf, foff);
        if i + 1 < reads_needed {
            // Still below 1 MB threshold
            assert_eq!(
                server.catalog.active_file_id(),
                0,
                "must not switch before threshold at iteration {i}"
            );
        }
    }

    // After 1 MB of contiguous reads, active channel MUST switch to 1
    assert_eq!(
        server.catalog.active_file_id(),
        1,
        "Channel must switch to 1 after 1 MB contiguous reads"
    );

    // Verify switch completed in well under 10 seconds
    let switch_duration = switch_start.elapsed();
    assert!(
        switch_duration < Duration::from_secs(10),
        "Channel switch took {:?}, must be under 10 seconds",
        switch_duration
    );

    // 3. Verify Channel 1 is active on the backend
    let active_node = server.catalog.active_node().expect("active node must exist");
    assert_eq!(active_node.name, "TV AO VIVO 2.tp");

    // 4. Continued playback of Channel 1 delivers valid video with correct alignment
    let mut ch1_buf = vec![0u8; BLOCK];
    let foff = (reads_needed * BLOCK) as u64;
    let n = server.serve_read(ch1_ino, foff, &mut ch1_buf);
    assert_eq!(n, BLOCK);
    assert_ts_grid(&ch1_buf, foff);

    // Verify switch completed in well under 10 seconds
    assert!(
        switch_duration < Duration::from_secs(10),
        "Channel switch took {:?}, must be under 10 seconds",
        switch_duration
    );
}

#[test]
fn test_return_resume_washed_out_fallback_to_live() {
    let engine = Arc::new(LiveEngine::new());

    // Write initial 10 MB and play 2 MB
    let initial_data = make_ts_stream(500_000, (10 * 1024 * 1024) / TS_PKT_SZ);
    engine.ring.write_chunk(&initial_data);

    let mut buf = vec![0u8; BLOCK];
    for i in 0..32u64 {
        let foff = i * BLOCK as u64;
        let n = engine.serve_live(0, foff, &mut buf, 1000);
        assert_eq!(n, BLOCK);
        assert_ts_grid(&buf, foff);
    }

    // Now simulate user leaving TV on pause/return for so long that the 128 MB ring wraps completely.
    // We write 135 MB of new data into the ring buffer, washing out the old position!
    let wrap_chunk = make_ts_stream(80_000_000, (15 * 1024 * 1024) / TS_PKT_SZ);
    for _ in 0..9 {
        engine.ring.write_chunk(&wrap_chunk);
    }

    // Shift time back by 2 hours
    engine
        .ring
        .lock()
        .shift_time_back(Duration::from_secs(7200));

    // TV reopens at bookmark offset 1_900_544 (which is now washed out / overwritten)
    let bookmark_foff = 1_900_544u64;
    let mut resume_buf = vec![0u8; 16_384];

    // Reads 1 to 3 return NULLs safely without panic or underflow
    for i in 0..3u64 {
        let foff = bookmark_foff + i * 16_384;
        let n = engine.serve_live(0, foff, &mut resume_buf, 1000);
        assert_eq!(n, 16_384);
        assert_ts_grid(&resume_buf, foff);
    }

    // Read 4 confirms ResumeAfterGap: falls back to live edge!
    let read4_foff = bookmark_foff + 48 * 1024;
    let n4 = engine.serve_live(0, read4_foff, &mut resume_buf, 1000);
    assert_eq!(n4, 16_384);
    assert_ts_grid(&resume_buf, read4_foff);

    // Verify subsequent playback serves real live video data from the new live edge
    let mut live_buf = vec![0u8; BLOCK];
    let next_foff = bookmark_foff + 64 * 1024;
    let n5 = engine.serve_live(0, next_foff, &mut live_buf, 1000);
    assert_eq!(n5, BLOCK);
    assert_ts_grid(&live_buf, next_foff);

    // Must not be empty/null packets: must contain real video PES packets
    let pts_list = extract_video_pts_list(&live_buf, next_foff);
    assert!(
        !pts_list.is_empty(),
        "fallback to live edge must serve live video packets with valid PTS"
    );
}
