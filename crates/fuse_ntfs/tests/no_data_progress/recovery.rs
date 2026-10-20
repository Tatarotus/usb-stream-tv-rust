//! TASK-002 recovery scenarios: bounded overwritten confirmation, ineligible
//! interruptions, invalid-reference noncommit, empty-request reset, symmetric
//! reference mapping and request-observation accounting.
//!
//! Child module of the `integration_no_data_progress` target: shared fixtures
//! and helpers come from the parent through `super::*`.

use std::time::Duration;

use super::*;
use fuse_ntfs::contract::PROBE_ISOLATION_FOFF;

// ============================== test 3: mixed range crossing ring_old
//                                              breaks the overwritten chain

/// A mixed reply crossing `ring_old` (NULL prefix, real tail) is ineligible for
/// the bounded overwritten recovery. A pending two-block chain is discarded, so
/// a fresh run of three consecutive eligible requests is required before the
/// single warm recovery, which calibrates from the genuine served state.
#[test]
fn mixed_range_crossing_ring_old_breaks_the_overwritten_chain() {
    let (engine, base, old, sw, e0) = wrapped_fixture();
    let mut rb = vec![0u8; READ];

    // Reads 1 and 2: wholly overwritten -> all-NULL, nothing moves.
    for i in 0..2u64 {
        let f = BM + i * READ as u64;
        let n = engine.serve_live(0, f, &mut rb, 1000);
        assert_eq!(n, READ, "speculative read {i}");
        assert!(all_null(&rb), "speculative read {i} is NULL-only");
        assert_eq!(ec(&engine), e0);
        assert_eq!(last_fend(&engine), PLAY_END);
    }
    assert_eq!(null_bytes(&engine), 0, "prefix NULLs are not counted");
    assert_eq!(sm_confirms(&engine), 0, "two blocks must not confirm");

    // Interrupter: a mixed reply whose first 120 packets are overwritten and
    // whose tail is real. It is never eligible, so the pending chain is dropped.
    let prefix = 120 * TS; // 22_560 B, 120 whole NULL packets
    let f_mix = old - prefix - base;
    assert!(
        f_mix < PROBE_ISOLATION_FOFF,
        "mixed read stays below the probe floor"
    );
    let mut big = vec![0u8; BLOCK];
    let n = engine.serve_live(0, f_mix, &mut big, 1000);
    assert_eq!(n, BLOCK);
    assert!(
        all_null(&big[..prefix as usize]),
        "overwritten head is NULL"
    );
    assert!(
        !all_null(&big[prefix as usize..]),
        "tail delivers live bytes"
    );
    assert_eq!(last_fend(&engine), f_mix + BLOCK as u64);
    assert_eq!(ec(&engine), e0, "mixed reply must not re-anchor");
    assert_eq!(sm_confirms(&engine), 0, "ineligible reply drops the chain");

    // A fresh chain: two more eligible blocks still do not confirm.
    for i in 2..4u64 {
        let f = BM + i * READ as u64;
        let n = engine.serve_live(0, f, &mut rb, 1000);
        assert_eq!(n, READ, "restarted chain read {i}");
        assert!(all_null(&rb), "restarted chain read {i} is NULL-only");
        assert_eq!(ec(&engine), e0);
        assert_eq!(sm_confirms(&engine), 0, "chain restarted at 1");
    }

    // Third consecutive eligible request: exactly one warm recovery.
    let f3 = BM + 4 * READ as u64;
    let before = refs(&engine);
    let n = engine.serve_live(0, f3, &mut rb, 1000);
    assert_eq!(n, READ);
    assert_eq!(sm_confirms(&engine), 1, "3rd eligible request confirms");
    assert_eq!(ec(&engine), e0 + 1, "exactly one epoch on recovery");
    assert!(!all_null(&rb), "recovery delivers live data");
    let target = sw - fuse_ntfs::contract::LEADBACK;
    let snapped = snap188(target);
    let base1 = base_spos(&engine);
    assert!(
        base1 <= snapped && snapped - base1 < 2 * 10 * TS,
        "live-band anchor"
    );
    let after = refs(&engine);
    assert_eq!(after.0, before.0 + 1, "recovery calibrates from served");
    assert_eq!(after.1, before.1, "no checkpoint selected");
    assert_eq!(after.2, before.2, "no ring-scan tier");
    assert_eq!(after.3, before.3, "no invalid-calibration fallback");
}

// ==================================== test 4: invalid reference -> noncommit

/// A confirmed recovery with no usable delivered reference must not commit:
/// the reply is explicit NULLs, the epoch, successful cursor and anchor mapping
/// stay intact, no calibration tier is selected and the invalid reference is
/// reported (`ref_none`). The delivered-reference state is force-invalidated
/// (the state a cold epoch reset leaves) and every surviving checkpoint is made
/// older than the age limit, so all three calibration tiers are unavailable.
#[test]
fn invalid_reference_recovery_does_not_commit() {
    let (engine, base, _old, _sw, e0) = wrapped_fixture();
    let fend = last_fend(&engine);
    assert_eq!(fend, PLAY_END);

    engine
        .ring
        .lock()
        .shift_time_back(Duration::from_secs(7200));
    engine.xform.lock().unwrap().reset_epoch(e0);

    let refs0 = refs(&engine);
    let mut rb = vec![0u8; READ];
    for i in 0..2u64 {
        let f = BM + i * READ as u64;
        let n = engine.serve_live(0, f, &mut rb, 1000);
        assert_eq!(n, READ, "speculative read {i}");
        assert!(all_null(&rb));
        assert_eq!(ec(&engine), e0);
        assert_eq!(last_fend(&engine), fend);
    }
    assert_eq!(
        sm_confirms(&engine),
        0,
        "no confirmation before the 3rd request"
    );
    let t0 = engine.anchor.lock().unwrap().last_read_ms;
    engine.ring.lock().shift_time_back(Duration::from_secs(5)); // R5: observation

    // 3rd consecutive eligible request: the tracker confirms, the recovery
    // cannot reference anything real, so the noncommit path is taken.
    let f3 = BM + 2 * READ as u64;
    let n = engine.serve_live(0, f3, &mut rb, 1000);
    assert_eq!(n, READ);
    assert!(all_null(&rb), "noncommit returns explicit NULLs");
    assert_eq!(sm_confirms(&engine), 1, "the tracker did confirm");
    assert_eq!(ec(&engine), e0, "noncommit must not advance the epoch");
    assert_eq!(engine.xform.lock().unwrap().epoch_id(), e0);
    assert_eq!(
        last_fend(&engine),
        fend,
        "noncommit must not move the cursor"
    );
    assert_eq!(base_spos(&engine), base, "noncommit must not re-anchor");
    let after = refs(&engine);
    assert_eq!(
        (after.0, after.1, after.2),
        (refs0.0, refs0.1, refs0.2),
        "no calibration tier may be selected"
    );
    assert_eq!(after.3, refs0.3 + 1, "invalid reference is reported");
    let observed = engine.anchor.lock().unwrap().last_read_ms;
    assert!(
        observed >= t0 + 5_000,
        "observation time advanced (not progress)"
    );
    assert_eq!(
        null_bytes(&engine),
        READ as u64,
        "noncommit NULLs are explicitly accounted"
    );
}

// ======================================= test 7: empty request resets chain

/// R2: an empty request is not progress. The engine returns before delivery and
/// must reset the pending overwritten chain, so the next eligible block cannot
/// be the 3rd confirmation; a fresh run of three still confirms exactly once.
#[test]
fn empty_request_resets_pending_overwritten_chain() {
    let (engine, _base, _old, _sw, e0) = wrapped_fixture();
    let mut rb = vec![0u8; READ];
    for i in 0..2u64 {
        assert_eq!(
            engine.serve_live(0, BM + i * READ as u64, &mut rb, 1000),
            READ
        );
        assert!(all_null(&rb));
    }
    let f2 = BM + 2 * READ as u64;
    assert_eq!(engine.serve_live(0, f2, &mut [], 1000), 0, "empty request");
    assert_eq!(engine.serve_live(0, f2, &mut rb, 1000), READ);
    assert_eq!(
        sm_confirms(&engine),
        0,
        "empty request must reset the chain"
    );
    assert_eq!(ec(&engine), e0);
    assert_eq!(
        engine.serve_live(0, BM + 3 * READ as u64, &mut rb, 1000),
        READ
    );
    assert_eq!(sm_confirms(&engine), 0, "fresh chain is at two");
    assert_eq!(
        engine.serve_live(0, BM + 4 * READ as u64, &mut rb, 1000),
        READ
    );
    assert_eq!(sm_confirms(&engine), 1, "fresh chain of three confirms");
    assert_eq!(ec(&engine), e0 + 1, "recovery after the fresh chain");
}

// ============ test 8: checked reference map below a non-zero base_foff

/// R4: the successful end is mapped backward when it sits below `base_foff`.
/// Shifting the virtual origin and the stream base by +4 MiB is an equivalent
/// reparameterization (every ordinary mapping is unchanged), but the successful
/// end (2 MiB) is now below the origin. Checkpoints are expired, so only the
/// genuine served reference can validate the confirmed recovery: a clamped
/// (saturating) mapping lands ~2 MiB away and must not commit.
#[test]
fn reference_below_base_foff_recovers_from_served() {
    let (engine, _base, _old, _sw, e0) = wrapped_fixture();
    {
        let mut a = engine.anchor.lock().unwrap();
        a.base_foff += 4 * MI;
        a.base_spos += 4 * MI;
    }
    assert_eq!(
        last_fend(&engine),
        PLAY_END,
        "successful end below the origin"
    );
    engine
        .ring
        .lock()
        .shift_time_back(Duration::from_secs(7200)); // expire ckpts
    let refs0 = refs(&engine);
    let mut rb = vec![0u8; READ];
    for i in 0..2u64 {
        assert_eq!(
            engine.serve_live(0, BM + i * READ as u64, &mut rb, 1000),
            READ
        );
        assert!(all_null(&rb), "speculative read {i} is NULL-only");
        assert_eq!(ec(&engine), e0);
    }
    let f2 = BM + 2 * READ as u64;
    assert_eq!(engine.serve_live(0, f2, &mut rb, 1000), READ);
    assert_eq!(sm_confirms(&engine), 1);
    assert_eq!(ec(&engine), e0 + 1, "backward map commits via served");
    assert_eq!(refs(&engine), (refs0.0 + 1, refs0.1, refs0.2, refs0.3));
    assert!(!all_null(&rb), "recovery delivers live data");
}

// ================= test 9: noncommit records observation, not progress

/// R5: request time is observation, not success. The virtual origin moves past
/// the successful end so the backward mapping of that end underflows; after two
/// pending requests a 5 s clock advance (below the resume gap) lets the third
/// request confirm. The noncommit path must record the observation time and drop
/// the zero-copy tail while the cursor, epoch and mapping stay frozen, and the
/// confirmed chain is consumed so a retry is not another confirmation.
#[test]
fn noncommit_records_observation_without_progress() {
    let (engine, _base, _old, _sw, e0) = wrapped_fixture();
    {
        let mut a = engine.anchor.lock().unwrap();
        a.base_foff += 6 * MI;
    }
    let fend = last_fend(&engine);
    let bs = base_spos(&engine);
    assert!(bs < 6 * MI - fend, "backward mapping would underflow");
    let refs0 = refs(&engine);
    let mut rb = vec![0u8; READ];
    for i in 0..2u64 {
        assert_eq!(
            engine.serve_live(0, BM + i * READ as u64, &mut rb, 1000),
            READ
        );
        assert!(all_null(&rb));
    }
    let t0 = engine.anchor.lock().unwrap().last_read_ms;
    engine.ring.lock().shift_time_back(Duration::from_secs(5)); // below gap_ms
    let f2 = BM + 2 * READ as u64;
    assert_eq!(engine.serve_live(0, f2, &mut rb, 1000), READ);
    assert!(all_null(&rb), "noncommit returns explicit NULLs");
    assert_eq!(sm_confirms(&engine), 1, "the tracker did confirm");
    assert_eq!(ec(&engine), e0, "no commit without a usable reference");
    assert_eq!(last_fend(&engine), fend, "no successful progress");
    assert_eq!(base_spos(&engine), bs, "mapping untouched");
    assert_eq!(refs(&engine), refs0, "no calibration tier selected");
    let observed = engine.anchor.lock().unwrap().last_read_ms;
    assert!(observed >= t0 + 5_000, "request observation time advanced");
    assert_eq!(
        engine.serve_live(0, BM + 3 * READ as u64, &mut rb, 1000),
        READ
    );
    assert_eq!(sm_confirms(&engine), 1, "confirmation consumed the chain");
}
