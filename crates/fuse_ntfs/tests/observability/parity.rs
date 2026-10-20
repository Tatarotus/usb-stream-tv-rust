//! TASK-003 R3 parity: the enabled observer must not change a single delivered
//! byte or any playback-state field, on the four paths the audit requires —
//! real delivery, NULL-only cold/starvation, confirmed low-offset overwritten
//! recovery and invalid-reference noncommit.
//!
//! Every scenario drives [`Pair`] (see [`super::fixtures`]), which applies the
//! same bytes to an observed and a disabled engine and compares the payload and
//! the whole state fingerprint after each step. The observed lines are then
//! checked to come from the real hooks that produced those outputs.

use fuse_ntfs::contract::PROBE_ISOLATION_FOFF;

use super::fixtures::*;

fn count(lines: &[String], needle: &str) -> usize {
    lines.iter().filter(|l| l.contains(needle)).count()
}

fn has(lines: &[String], needle: &str) -> bool {
    lines.iter().any(|l| l.contains(needle))
}

/// Post-delivery results only: `ring_underrun` and the summaries carry the same
/// numeric keys, so counts must be scoped to the result event.
fn progress<'a>(lines: &'a [String]) -> Vec<&'a String> {
    lines
        .iter()
        .filter(|l| l.contains("event=live_read_progress"))
        .collect()
}

/// Number of results carrying `needle`.
fn results_with(lines: &[String], needle: &str) -> usize {
    progress(lines)
        .iter()
        .filter(|l| l.contains(needle))
        .count()
}

/// True when `line` carries `key=value` as a whole field.
fn field_eq(line: &str, key: &str, value: &str) -> bool {
    let want = format!("{key}={value}");
    line.split(' ').any(|kv| kv.trim_end() == want)
}

/// Every read emits exactly one post-delivery result, so a missing hook, a
/// double emit or an extra unpaired event is visible.
fn assert_one_result_per_read_lines(reads: usize, lines: &[String]) {
    assert_eq!(
        count(lines, "event=live_read_progress"),
        reads,
        "one post-delivery result per read: {lines:?}"
    );
    for l in lines
        .iter()
        .filter(|l| l.contains("event=live_read_progress"))
    {
        assert!(l.contains("component=engine"), "{l}");
        assert!(l.contains("real_bytes="), "{l}");
    }
}

#[test]
fn real_delivery_is_byte_and_state_identical_with_observation() {
    let mut pair = Pair::start();
    let (_base, _e0) = pair.warm();
    let tip = pair.read(3 * MI, READ);
    assert!(!all_null(&tip), "resident read is real media");
    let reads = pair.read_count();
    let lines = pair.lines();

    assert!(reads > 30, "warm playback performed {reads} reads");
    assert_one_result_per_read_lines(reads, &lines);
    // Every observed read delivered real bytes and no synthetic filler: the 32
    // coarse reads report their whole `BLOCK`, the trailing fine read its `READ`.
    assert_eq!(
        results_with(&lines, &format!("real_bytes={BLOCK}")),
        reads - 1,
        "coarse reads report the copied real bytes: {lines:?}"
    );
    assert_eq!(results_with(&lines, &format!("real_bytes={READ}")), 1);
    for l in progress(&lines) {
        assert!(field_eq(l, "null_bytes", "0"), "no NULL filler: {l}");
    }
}

#[test]
fn cold_null_filler_is_byte_and_state_identical_with_observation() {
    let mut pair = Pair::start();
    let cold = pair.read(0, READ);
    assert!(all_null(&cold), "a cold ring yields NULL packets");
    assert_eq!(pair.read_count(), 1);
    let lines = pair.lines();

    assert_one_result_per_read_lines(1, &lines);
    assert!(has(&lines, "event=live_read_progress"), "{lines:?}");
    assert!(has(&lines, "reason=cold"), "{lines:?}");
    assert!(has(&lines, "component=engine"), "{lines:?}");
    // The result is the NULL filler it actually wrote, not the request size.
    assert!(has(&lines, &format!("len={READ}")), "{lines:?}");
    assert!(has(&lines, &format!("null_bytes={READ}")), "{lines:?}");
    assert!(has(&lines, "real_bytes=0"), "{lines:?}");
    assert!(has(&lines, "wait_ms="), "the cold wait is reported");
}

#[test]
fn starved_and_rejected_reads_are_byte_and_state_identical_with_observation() {
    let mut pair = Pair::start();
    let (base, _e0) = pair.warm();
    // Feeder restart: the old window is dropped and the new head is refilled.
    pair.restart(40_000 * TS); // ~7.5 MiB, above MIN_STREAM_START
    let head = s_write(&pair.on) - base;
    assert!(head > 0 && head < PROBE_ISOLATION_FOFF, "head below floor");

    let starved = pair.read(head, READ);
    assert!(all_null(&starved), "no data past the restarted head");
    let probe = pair.read(PROBE_ISOLATION_FOFF + MI, READ);
    assert!(all_null(&probe), "future probe is rejected");
    let lines = pair.lines();

    assert!(
        has(&lines, "event=ring_underrun"),
        "underrun hook: {lines:?}"
    );
    assert!(has(&lines, "reason=starve"), "{lines:?}");
    assert!(has(&lines, "reason=probe_reject"), "{lines:?}");
    // Both results are the explicit NULL filler they wrote.
    assert_eq!(
        results_with(&lines, &format!("null_bytes={READ}")),
        2,
        "explicit NULL accounting: {lines:?}"
    );
}

#[test]
fn confirmed_low_offset_overwritten_recovery_is_identical_with_observation() {
    let mut pair = Pair::start();
    let (base, e0) = pair.warm();
    pair.wrap(base);
    for i in 0..2u64 {
        let buf = pair.read(BM + i * READ as u64, READ);
        assert!(all_null(&buf), "speculative read {i} is NULL-only");
    }
    assert_eq!(confirms(&pair.on), 0, "two blocks must not confirm");
    let recovered = pair.read(BM + 2 * READ as u64, READ);
    assert!(!all_null(&recovered), "recovery delivers live data");
    assert_eq!(confirms(&pair.on), 1, "third eligible request confirms");
    assert_eq!(epoch(&pair.on), e0 + 1, "exactly one epoch on recovery");
    let lines = pair.lines();

    assert!(
        has(&lines, "event=reanchor_commit") && has(&lines, "reason=overwritten_recovery"),
        "commit hook carries the recovery reason: {lines:?}"
    );
    assert!(
        has(&lines, &format!("anchor_epoch={}", e0 + 1)),
        "{lines:?}"
    );
    // The speculative reads reported their NULL filler; the recovery read
    // reported the bytes it actually delivered.
    assert_eq!(
        results_with(&lines, &format!("null_bytes={READ}")),
        2,
        "speculative reads are accounted as NULL: {lines:?}"
    );
    let last = progress(&lines)
        .pop()
        .unwrap_or_else(|| panic!("result emitted: {lines:?}"));
    assert!(last.contains(&format!("real_bytes={READ}")), "{last}");
    assert!(last.contains("null_bytes=0"), "{last}");
}

#[test]
fn invalid_reference_noncommit_is_byte_and_state_identical_with_observation() {
    let mut pair = Pair::start();
    let (base, e0) = pair.warm();
    pair.wrap(base);
    pair.expire(7_200); // every surviving checkpoint ages out
    pair.reset_epoch(e0); // no usable delivered reference remains
    for i in 0..2u64 {
        let buf = pair.read(BM + i * READ as u64, READ);
        assert!(all_null(&buf), "speculative read {i} is NULL-only");
    }
    let noncommit = pair.read(BM + 2 * READ as u64, READ);
    assert!(all_null(&noncommit), "noncommit returns explicit NULLs");
    assert_eq!(confirms(&pair.on), 1, "the tracker did confirm");
    assert_eq!(epoch(&pair.on), e0, "noncommit must not advance the epoch");
    assert_eq!(
        last_fend(&pair.on),
        PLAY_END,
        "noncommit must not move the cursor"
    );
    let lines = pair.lines();

    assert!(
        has(&lines, "event=overwritten_noncommit") && has(&lines, "reason=reference_invalid"),
        "noncommit hook carries the invalid reference: {lines:?}"
    );
    let last = progress(&lines)
        .pop()
        .unwrap_or_else(|| panic!("result emitted: {lines:?}"));
    assert!(last.contains("reason=reference_invalid"), "{last}");
    assert!(last.contains(&format!("null_bytes={READ}")), "{last}");
    assert!(last.contains("real_bytes=0"), "{last}");
}
