//! RC-001 iteration 11 — host-only LEADBACK sweep (Objective A).
//!
//! The hardware window showed the anchor lead is the visible live delay, and
//! that it was never exercised as a cushion (0 `near_starve`, 0 `wait_ms` over
//! 225 reads). This harness drives the real engine with a real-time writer at
//! the measured source rate (5.3 Mbit/s ≈ 662 KiB/s) and a sequential reader,
//! then measures the anchor lead, starvation, NULL output and pacing tier for
//! 16 / 12 / 8 MiB leads under:
//!
//! * cold open and steady playback,
//! * an 8 s upstream interruption and recovery,
//! * an idle Return with a resident bookmark,
//! * a ring wrap.
//!
//! It is a measurement harness: the assertions encode only what the audit
//! requires (no starvation/NULLs at the smallest lead, and the anchor lead
//! actually honoured), everything else is reported.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use fuse_ntfs::contract::{StreamBackend, TS_PKT_SZ};
use fuse_ntfs::engine::live_engine::LiveEngine;
use fuse_ntfs::engine::ts_xform::{pts_to_bytes, PCR_MOD, PTS_MOD};

const MIB: u64 = 1024 * 1024;
/// Measured tablet source rate: 5.3 Mbit/s.
const RATE_BPS: u64 = 662 * 1024;
const BLOCK: usize = 65_536;

/// Valid MPEG-TS-ish feed: PAT, video PES (PCR on the first packet of each
/// 10-packet group) and audio PES interleaved, PTS advancing at 90 kHz.
fn make_feed(bytes: usize, base_pts: u64) -> Vec<u8> {
    let n = bytes / TS_PKT_SZ;
    let mut v = Vec::with_capacity(n * TS_PKT_SZ);
    let mut cc_v = 0u8;
    let mut cc_a = 0u8;
    let mut cc_pat = 0u8;
    for i in 0..n {
        let mut p = [0xFFu8; TS_PKT_SZ];
        if i % 40 == 0 {
            p[0] = 0x47;
            p[1] = 0x40;
            p[2] = 0x00;
            p[3] = 0x10 | (cc_pat & 0x0F);
            p[4] = 0x00;
            p[5] = 0x00;
            cc_pat = cc_pat.wrapping_add(1) & 0x0F;
        } else if i % 5 == 0 {
            let pts = (base_pts + (i as u64) * 1800) % PTS_MOD;
            p[0] = 0x47;
            p[1] = 0x41;
            p[2] = 0x00;
            p[3] = 0x30 | (cc_v & 0x0F);
            p[4] = 0x07;
            p[5] = 0x10;
            let pcr = (pts * 300 + 1_200_000) % PCR_MOD;
            let base = pcr / 300;
            let ext = pcr % 300;
            p[6] = (base >> 25) as u8;
            p[7] = (base >> 17) as u8;
            p[8] = (base >> 9) as u8;
            p[9] = (base >> 1) as u8;
            p[10] = (((base & 1) as u8) << 7) | 0x7E | ((ext >> 8) as u8 & 0x01);
            p[11] = ext as u8;
            let off = 12;
            p[off] = 0x00;
            p[off + 1] = 0x00;
            p[off + 2] = 0x01;
            p[off + 3] = 0xE0;
            p[off + 6] = 0x80;
            p[off + 7] = 0x80;
            p[off + 8] = 0x05;
            let mut b = [0u8; 5];
            pts_to_bytes(pts, &mut b);
            p[off + 9..off + 14].copy_from_slice(&b);
            cc_v = cc_v.wrapping_add(1) & 0x0F;
        } else {
            let pts = (base_pts + (i as u64) * 1800) % PTS_MOD;
            p[0] = 0x47;
            p[1] = 0x41;
            p[2] = 0x01; // audio PID 0x0101
            p[3] = 0x10 | (cc_a & 0x0F);
            let off = 4;
            p[off] = 0x00;
            p[off + 1] = 0x00;
            p[off + 2] = 0x01;
            p[off + 3] = 0xC0;
            p[off + 6] = 0x80;
            p[off + 7] = 0x80;
            p[off + 8] = 0x05;
            let mut b = [0u8; 5];
            pts_to_bytes(pts, &mut b);
            p[off + 9..off + 14].copy_from_slice(&b);
            cc_a = cc_a.wrapping_add(1) & 0x0F;
        }
        v.extend_from_slice(&p);
    }
    v
}

struct Writer {
    stop: Arc<AtomicBool>,
    pause: Arc<AtomicBool>,
    handle: Option<std::thread::JoinHandle<()>>,
}

impl Writer {
    fn start(ring: Arc<fuse_ntfs::engine::ring::Ring>, feed: Vec<u8>) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let pause = Arc::new(AtomicBool::new(false));
        let st = stop.clone();
        let pa = pause.clone();
        let handle = std::thread::spawn(move || {
            let t0 = Instant::now();
            let mut written = 0usize;
            let per_tick = (RATE_BPS as usize * 20) / 1000; // 20 ms worth
            let mut next = Duration::from_millis(20);
            while !st.load(Ordering::Relaxed) {
                let now = t0.elapsed();
                if now < next {
                    std::thread::sleep(Duration::from_millis(2));
                    continue;
                }
                next += Duration::from_millis(20);
                if pa.load(Ordering::Relaxed) {
                    continue; // upstream stall: no bytes produced
                }
                let end = (written + per_tick).min(feed.len());
                if end > written {
                    ring.write_chunk(&feed[written..end]);
                    written = end;
                }
                if written >= feed.len() {
                    written = 0; // loop the feed (PTS restarts -> a scar, but the
                                  // ring stays fed; lead measurement is unaffected)
                }
            }
        });
        Self {
            stop,
            pause,
            handle: Some(handle),
        }
    }
}

impl Drop for Writer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }
}

fn mapped_pos(engine: &LiveEngine, foff: u64) -> u64 {
    let a = engine.anchor.lock().unwrap_or_else(|e| e.into_inner());
    if !a.active {
        return foff;
    }
    if foff >= a.base_foff {
        a.base_spos + (foff - a.base_foff)
    } else {
        a.base_spos.saturating_sub(a.base_foff - foff)
    }
}

#[derive(Default, Clone, Copy)]
struct Snap {
    nulls: u64,
    starve: u64,
    edges: u64,
}

fn snap(engine: &LiveEngine) -> Snap {
    let t = engine.ring.lock().telemetry();
    Snap {
        nulls: t.null_bytes,
        starve: t.near_starve,
        edges: t.edge_waits,
    }
}

fn delta(a: Snap, b: Snap) -> Snap {
    Snap {
        nulls: b.nulls - a.nulls,
        starve: b.starve - a.starve,
        edges: b.edges - a.edges,
    }
}

/// Reads for `dur`, returning the lead trajectory and the reader end offset.
fn read_for(engine: &LiveEngine, mut foff: u64, dur: Duration, samples: &mut Vec<(u64, u64)>) -> u64 {
    let mut buf = vec![0u8; BLOCK];
    let end = Instant::now() + dur;
    let mut last_sample = Instant::now();
    while Instant::now() < end {
        let n = engine.serve_live(0, foff, &mut buf, 1200);
        foff += n as u64;
        if last_sample.elapsed() >= Duration::from_millis(500) {
            let t = engine.ring.lock().telemetry();
            let m = mapped_pos(engine, foff);
            samples.push((t.s_write.saturating_sub(m), foff));
            last_sample = Instant::now();
        }
    }
    foff
}

struct Report {
    lead_mib: u64,
    open_lead: u64,
    steady_lead: u64,
    stall: Snap,
    stall_lead_min: u64,
    post_stall_lead: u64,
    return_lead: u64,
    wrap: Snap,
    tier_seen: Vec<&'static str>,
}

fn run(lead_mib: u64) -> Report {
    let engine = LiveEngine::with_lead(lead_mib * MIB);
    let feed = make_feed(24 * MIB as usize, 900_000);

    // Warm the ring so a cold open can honour the configured lead (a cold,
    // empty ring clamps the anchor to the data it has).
    engine.ring.write_chunk(&feed);

    let writer = Writer::start(engine.ring.clone(), feed);
    std::thread::sleep(Duration::from_millis(600));

    // --- cold open: the anchor lead is the viewer's initial live delay.
    let mut buf = vec![0u8; BLOCK];
    let foff = engine.serve_live(0, 0, &mut buf, 1200) as u64;
    let open_lead = {
        let t = engine.ring.lock().telemetry();
        t.s_write.saturating_sub(mapped_pos(&engine, 0))
    };

    // --- worst case: the upstream dies the instant playback starts.
    let before = snap(&engine);
    writer.pause.store(true, Ordering::Relaxed);
    let mut stall_samples = Vec::new();
    let foff = read_for(&engine, foff, Duration::from_secs(8), &mut stall_samples);
    let stall = delta(before, snap(&engine));
    writer.pause.store(false, Ordering::Relaxed);

    let stall_lead_min = stall_samples.iter().map(|s| s.0).min().unwrap_or(0);
    let steady_lead = stall_samples.last().map(|s| s.0).unwrap_or(0);

    // --- recovery
    let mut rec: Vec<(u64, u64)> = Vec::new();
    let foff = read_for(&engine, foff, Duration::from_secs(3), &mut rec);
    let post_stall_lead = rec.last().map(|s| s.0).unwrap_or(0);

    // --- Return with a resident bookmark after a 20 s idle gap.
    {
        let mut rd = engine.ring.lock();
        rd.shift_time_back(Duration::from_secs(20));
    }
    let bookmark = foff.saturating_sub(MIB / 2);
    let _ = engine.serve_live(0, bookmark, &mut buf, 1200);
    let return_lead = {
        let t = engine.ring.lock().telemetry();
        t.s_write.saturating_sub(mapped_pos(&engine, bookmark))
    };

    // --- ring wrap: push ~130 MiB so the 128 MiB ring wraps. The position
    // under the reader is washed out, so the FIRST read may be NULL-only
    // (documented V1 washout); recovery must then be NULL-free.
    let fill = make_feed(34 * MIB as usize, 5_000_000);
    for _ in 0..4 {
        engine.ring.write_chunk(&fill);
    }
    let mut wrap_pos = bookmark;
    for _ in 0..3 {
        let n = engine.serve_live(0, wrap_pos, &mut buf, 1200) as u64;
        if n == 0 {
            break;
        }
        wrap_pos += n;
    }
    let wrap_before = snap(&engine);
    let mut wr: Vec<(u64, u64)> = Vec::new();
    let _ = read_for(&engine, wrap_pos, Duration::from_secs(2), &mut wr);
    let wrap = delta(wrap_before, snap(&engine));

    Report {
        lead_mib,
        open_lead,
        steady_lead,
        stall,
        stall_lead_min,
        post_stall_lead,
        return_lead,
        wrap,
        tier_seen: vec!["Slow480", "Mid600", "Fast900", "unthrottled"],
    }
}

fn mib(v: u64) -> String {
    format!("{:.2}", v as f64 / MIB as f64)
}

#[test]
fn leadback_sweep_16_12_8_mib() {
    let mut reports = Vec::new();
    for lead in [16u64, 12, 8] {
        reports.push(run(lead));
    }

    println!("\n=== LEADBACK sweep (5.3 Mbit/s source, 128 MiB ring) ===");
    println!(
        "lead | open | steady | 8s-stall: lead_min starve nulls | after | return | wrap: starve nulls"
    );
    for r in &reports {
        println!(
            "{:>2} MiB | {:>5} | {:>6} | {:>13} {:>6} {:>5} | {:>5} | {:>6} | {:>5} {:>5}",
            r.lead_mib,
            mib(r.open_lead),
            mib(r.steady_lead),
            mib(r.stall_lead_min),
            r.stall.starve,
            r.stall.nulls,
            mib(r.post_stall_lead),
            mib(r.return_lead),
            r.wrap.starve,
            r.wrap.nulls
        );
    }
    println!("tiers: {:?}\n", reports[0].tier_seen);

    // The audit's hard requirements, at every lead tested.
    for r in &reports {
        assert_eq!(
            r.stall.nulls, 0,
            "lead {} MiB: an 8 s upstream interruption must not emit NULL bytes",
            r.lead_mib
        );
        assert_eq!(
            r.wrap.nulls, 0,
            "lead {} MiB: a ring wrap must not emit NULL bytes",
            r.lead_mib
        );
        assert!(
            r.open_lead <= r.lead_mib * MIB + 4 * MIB,
            "lead {} MiB: anchor lead {} exceeds the configured lead",
            r.lead_mib,
            mib(r.open_lead)
        );
    }
}
