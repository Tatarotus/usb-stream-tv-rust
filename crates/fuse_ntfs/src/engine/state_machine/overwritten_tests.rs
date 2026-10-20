//! TASK-002 overwritten-recovery tracker tests (R1/R3 unit coverage).
//!
//! Extracted verbatim from the nested `tests` module so the tracker tests keep
//! private access (`super::*`) while `state_machine.rs` stays within its legacy
//! physical-line ceiling. Test names and assertions are unchanged.

use super::*;

const OVW_STEP: u64 = 64 * 1024;
const OVW_WIN: u64 = 32 * 1024 * 1024;

fn ovw(foff: u64, now_ms: u64) -> OvwCtx {
    raw(foff, now_ms, foff, OVW_STEP, OVW_WIN, true)
}

/// Explicit tracker input: virtual foff, now, mapped stream start, length,
/// ring_old and the engine eligibility ruling.
fn raw(foff: u64, now: u64, mapped: u64, len: u64, old: u64, ok: bool) -> OvwCtx {
    OvwCtx {
        foff,
        now_ms: now,
        mapped_start: mapped,
        len,
        ring_old: old,
        base_eligible: ok,
        ..Default::default()
    }
}

#[test]
fn overwritten_confirmation_needs_three_consecutive_requests() {
    let step = 64 * 1024;
    let mut sm = StateMachine::new();
    assert!(!sm.note_overwritten_request(ovw(0, 1_000)));
    assert!(
        !sm.note_overwritten_request(ovw(step, 1_100)),
        "2nd is not enough"
    );
    assert_eq!(sm.ovw_confirms, 0);
    assert!(
        sm.note_overwritten_request(ovw(2 * step, 1_200)),
        "3rd confirms"
    );
    assert_eq!(sm.ovw_confirms, 1);
    assert!(
        !sm.note_overwritten_request(ovw(3 * step, 1_300)),
        "chain consumed"
    );
    // The exact resume-gap boundary still continues the chain.
    assert!(!sm.note_overwritten_request(ovw(4 * step, 1_300 + RESUME_GAP_MS)));
    assert!(sm.note_overwritten_request(ovw(5 * step, 1_300 + 2 * RESUME_GAP_MS)));
    assert_eq!(sm.ovw_confirms, 2);
}

#[test]
fn overwritten_chain_resets_on_retry_gap_ineligible_and_lifecycle() {
    let step = 64 * 1024;
    let mut sm = StateMachine::new();
    // Retries/overlaps of the same request never accumulate.
    for now in [0, 10, 20] {
        assert!(!sm.note_overwritten_request(ovw(0, now)));
    }
    // Gapped or ineligible reads restart the chain.
    assert!(!sm.note_overwritten_request(ovw(4 * step, 30)));
    assert!(!sm.note_overwritten_request(ovw(5 * step, 40)));
    assert!(!sm.note_overwritten_request(OvwCtx {
        base_eligible: false,
        ..ovw(6 * step, 50)
    }));
    assert!(!sm.note_overwritten_request(ovw(7 * step, 60)));
    // A gap beyond the resume window, or another file/epoch, restarts.
    assert!(!sm.note_overwritten_request(ovw(8 * step, 60 + RESUME_GAP_MS + 1)));
    assert!(!sm.note_overwritten_request(ovw(9 * step, 60 + 2 * RESUME_GAP_MS + 2)));
    assert!(!sm.note_overwritten_request(OvwCtx {
        file_id: 2,
        ..ovw(10 * step, 70)
    }));
    assert!(!sm.note_overwritten_request(OvwCtx {
        epoch: 2,
        ..ovw(11 * step, 80)
    }));
    // Lifecycle resets (fresh open / flush) clear the chain.
    assert!(!sm.note_overwritten_request(ovw(12 * step, 90)));
    sm.note_open(100);
    assert!(!sm.note_overwritten_request(ovw(13 * step, 110)));
    assert!(!sm.note_overwritten_request(ovw(14 * step, 120)));
    sm.reset_probe_state();
    assert!(!sm.note_overwritten_request(ovw(15 * step, 130)));
    assert_eq!(sm.ovw_confirms, 0, "nothing ever confirmed");
}

/// R1/R3: the virtual 8 MiB floor and the stream `ring_old` bound are
/// separate, and a disqualifier injected at the exact expected next foff
/// (only it can block the 3rd request) must block confirmation and consume
/// the chain: a fresh run of three is required.
#[test]
fn overwritten_bounds_are_separate_and_disqualifiers_reset_the_chain() {
    // Low virtual offset mapped 64 MiB into a long stream is eligible; the
    // last legal block ends exactly at the virtual 8 MiB floor.
    for seed in [
        raw(0, 0, 64 * 1024 * 1024, OVW_STEP, 96 * 1024 * 1024, true),
        raw(
            PROBE_ISOLATION_FOFF - 3 * OVW_STEP,
            0,
            0,
            OVW_STEP,
            OVW_WIN,
            true,
        ),
    ] {
        let mut c = seed;
        let mut sm = StateMachine::new();
        assert!(!sm.note_overwritten_request(c));
        c.foff += OVW_STEP;
        assert!(!sm.note_overwritten_request(c));
        c.foff += OVW_STEP;
        assert!(sm.note_overwritten_request(c), "3rd confirms");
        assert_eq!(sm.ovw_confirms, 1);
    }
    // Each disqualifier fires where only it can block the 3rd request.
    let bad = [
        raw(2 * OVW_STEP, 20, 0, 0, OVW_WIN, true),
        raw(2 * OVW_STEP, 20, OVW_WIN, OVW_STEP, OVW_WIN, true),
        raw(
            2 * OVW_STEP,
            20,
            OVW_WIN - 32 * 1024,
            OVW_STEP,
            OVW_WIN,
            true,
        ),
        raw(2 * OVW_STEP, 20, 0, PROBE_ISOLATION_FOFF, OVW_WIN, true),
        raw(2 * OVW_STEP, 20, u64::MAX - 8, OVW_STEP, OVW_WIN, true),
        raw(2 * OVW_STEP, 20, 0, u64::MAX, OVW_WIN, true),
    ];
    for (i, b) in bad.into_iter().enumerate() {
        let mut sm = StateMachine::new();
        assert!(!sm.note_overwritten_request(ovw(0, 0)));
        assert!(!sm.note_overwritten_request(ovw(OVW_STEP, 10)));
        assert!(!sm.note_overwritten_request(b), "case {i} confirmed");
        assert_eq!((sm.ovw_confirms, sm.ovw_count), (0, 0), "case {i} reset");
        assert!(!sm.note_overwritten_request(ovw(3 * OVW_STEP, 20)));
        assert_eq!(sm.ovw_count, 1, "fresh chain starts at one");
        assert!(!sm.note_overwritten_request(ovw(4 * OVW_STEP, 30)));
        assert_eq!(sm.ovw_count, 2, "fresh chain length two");
        assert!(
            sm.note_overwritten_request(ovw(5 * OVW_STEP, 40)),
            "fresh 3"
        );
        assert_eq!(sm.ovw_confirms, 1);
    }
    assert!(core::mem::size_of::<OvwCtx>() <= 128);
}
