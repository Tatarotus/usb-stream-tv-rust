# TASK-002 — scope addendum for R6 (2026-10-19)

## Decision

Authorize cohesive extraction of tests into child modules. Do not increase the500line limit for new maintained files, raise the state-machine legacy cap, dispense with tests, duplicate fixtures or compress code to fit a number. This is an amendment to TASK-002, not a new feature or authorization for TASK-003. Production behavior remains frozen during this structural pass.

R1–R5 corrections have been inspected independently without a new blocking functional finding; final patch approval remains pending structural/formatting checks and revalidation. Previous audit outcome remains CHANGES_REQUESTED until those gates are met. Reported negative controls are not silently upgraded to independently executed evidence.

## Baseline to preserve

Task-start current hashes: live_engine1495ebcad7680af24894eeca6a1574654c0e4596a4206a65c6ca4d22c3b25d05; state_machine8e541639317beb425c70beaa603f4915e3212249bd980e3bf6b66d3e3d1deb89; integration_no_data_progress56fac6bb8352b32da892ec228d89bad991fcc237ba5300a893deed18e1b1306e. Frozen TASK-001 remainsd71e04fca90618e0abe4cc860bcd6742524b70cdd6d25e696322a3d6df69803f. Keep the strict test at its submitted hash4f27319ace326e48fe5be8f5b5bb9987b9c8ae9148cb8f2d0ede32dc0149a6ac during this structural pass.

Current actual sizes: live_engine500;state_machine818;no_data615. These are observations, not new approved ceilings.

## Exact additional allowed paths (NEW)

1. crates/fuse_ntfs/src/engine/state_machine/overwritten_tests.rs
2. crates/fuse_ntfs/tests/no_data_progress/delivery.rs
3. crates/fuse_ntfs/tests/no_data_progress/recovery.rs

Parent files that may be changed for this pass: crates/fuse_ntfs/src/engine/state_machine.rs and crates/fuse_ntfs/tests/integration_no_data_progress.rs. live_engine.rs may receive formatting of task-authored hunks only, with no semantic change; retain its500line ceiling. All other source/config/script paths remain read-only. In particular engine/mod.rs and Cargo.toml do not need modifications for these child modules.

## Required structure

### State-machine unit tests

Move the3TASK-002 overwritten-tracker tests and their exclusively associated test helpers/constants from the current nested tests module to state_machine/overwritten_tests.rs. Keep all pre-existing state-machine tests unchanged. Declare the NEW module at state_machine.rs module level with #[cfg(test)] mod overwritten_tests; each child can use super::* to access parent-private tracker state. Do not make ovw_count or tracker internals publicly accessible for this move.

The child module is compiled only for unit tests. No runtime code or tracker logic is moved or changed. Do not accidentally leave a second copy of a test in the old tests module. Last-segment test names and assertions remain identical; full test paths legitimately change.

### Integration scenarios

Keep common imports, constants, fixture builders and helper functions ONCE in integration_no_data_progress.rs. Declare child modules using explicit paths no_data_progress/delivery.rs and no_data_progress/recovery.rs; both use super::* to access the existing parent-private helpers. Do not duplicate ~150lines of fixture code or introduce include! of another integration target.

Move scenarios as follows, preserving all9test functions and assertions:

| Child module | Scenarios |
|---|---|
| delivery.rs | producer_restart_does_not_forge_progress; real_bytes_plus_null_suffix_reports_explicit_nulls; checkpoint_non_fabrication_at_writer_edge; fixture_is_parsed_by_production_ts_parser |
| recovery.rs | mixed_range_crossing_ring_old_breaks_the_overwritten_chain; invalid_reference_recovery_does_not_commit; empty_request_resets_pending_overwritten_chain; reference_below_base_foff_recovers_from_served; noncommit_records_observation_without_progress |

The Cargo integration target stays integration_no_data_progress. Nested files are child modules, not new auto-discovered root targets. Cargo test filters by last test-name substring remain usable; update negative-control command paths to match the moved functions. Parent/child private access is sufficient; no production testing API is authorized.

## Formatting and limits

- Every NEW file and the wholly task-authored integration_no_data_progress.rs must be fully rustfmt-clean and<=500physical lines after formatting.
- state_machine.rs must return to<=783physical lines, the existing approved legacy ceiling. Move only the cohesive new tracker tests; no unrelated module splitting. Format new production tracker hunks and module declarations, leaving unrelated historical formatting WIP alone.
- live_engine.rs remains<=500physical lines; format only task-authored hunks, preserving existing semantics and unrelated legacy text. Do not remove meaningful comments or combine unrelated statements to evade line counts.
- integration_strict_contract.rs remains unchanged in this pass; its already documented legacy formatting differences remain a baseline FAIL.
- Report rustfmt output for legacy parent files faithfully. It may remain nonzero solely because of unchanged baseline regions; the new files and task-authored regions must be clean. No full-repository formatting.

If any budget still cannot be met with this structure, stop and report exact formatted counts and affected hunks for a further bounded contract decision. Do not silently relax thresholds or delete tests.

## Validation and evidence

Inspect moved files for accidental IO. Run the existing safe suite serially:

cargo test -p fuse_ntfs --offline -- --test-threads=1 --skip probe_isolation_future_and_behind

Expect146passed,2known ignored,1explicit safety filter; total110library+4cont_splice+9no_data+3return/channel+7seek+13strict. Module-qualified names may change, test count and assertions must not. Never run the unfiltered resync-marker test or --ignored cases. No /data/sysfs/marker checks or cleanup.

Run rustfmt --check --edition2021 on the new child files and integration root; inspect legacy-file formatting diffs separately. Run git diff --check, SHA256 frozen tests, and physical-line counts on every changed/new file. Re-run release ARMv7 build using existing offline toolchain when code/module layout changes; no deploy or binary installation.

No need to invent extra tests for a pure move. Preserve existing failing-negative-control evidence, and rerun representative negative controls after moves to verify filters still execute the intended tests. Isolated source and explicit separate CARGO_TARGET_DIR are required; never share original target artifacts with mutants. Record each individual Cargo exit, not just wrapper-shell exit.

Handoff includes exact structural diff, path/name mapping for all9integration+3tracker tests, before/after formatted counts, preserved hashes, command outputs, remaining baseline formatting diffs, and all deviations. Architect independently reviews the final diff and safe tests. No automatic approval, deployment or next-task execution.

## Final disposition

Structural pass AUDIT_PASSED. The final audit accepts the unchanged pre-TASK-002 live_engine formatting regions as documented baseline FAIL, consistent with this addendum. No increase of the500line ceiling, test removal or further mechanical formatting is needed for TASK-002. Full repository formatting remains a separate open quality finding. See [TASK_002_AUDIT.md](../architecture/TASK_002_AUDIT.md).
