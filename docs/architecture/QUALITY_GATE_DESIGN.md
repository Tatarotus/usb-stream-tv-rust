# Quality-gate design — no tooling installed in audit phase

Baseline: [FORENSIC_AUDIT.md](FORENSIC_AUDIT.md). Current format/lint fail,2 ignored bugs reproduce,13 tracked source files exceed500 lines; coverage,mutation and CCN are UNKNOWN. No .github pipeline found. These thresholds are target gates, not present achievements.

## Complexity and source policy

Select Mozilla rust-code-analysis-cli, with a reviewed pinned release/commit and checksum. It exposes separate cyclomatic and cognitive metrics; use per-function cyclomatic, not unit aggregate or cognitive complexity. Its current implementation starts at1 and increments for Rust branch/loop/match-arm/try/short-circuit AST nodes. Validate exact pinned grammar and closure attribution against manually counted fixtures and representative `StateMachine::evaluate`/`LiveEngine::serve_live` before enforcing. This is an AST decision-count cyclomatic variant, not a claimed reconstructed machine-code CFG. [Metric documentation](https://mozilla.github.io/rust-code-analysis/metrics.html), [implementation and Rust fixture](https://github.com/mozilla/rust-code-analysis/blob/master/src/metrics/cyclomatic.rs).

Proposed `scripts/check_complexity.sh`: parse machine-readable per-function metrics; warn15,fail>20 for new/changed functions. Full CI scans all functions, reports maximum and a hash-bound explicit legacy debt list; no new/increased debt. Parser/tool failure is BLOCKED/failed check, never zero complexity. Baseline maximum NOT_RUN until installation/calibration. Existing oversized code is not excuse to hide CCN.

Proposed `scripts/check_file_size.sh`: count physical lines (including final unterminated line) of maintained.rs/.py/.sh and tests; new files<=500,warn300. Legacy touched files cannot grow without an individually reviewed reason/expiry, and full CI always reports all oversize counts. Do not split cohesive algorithms merely for a number. Determine whether consolidated root Rust is generated/reference before excluding; publish raw-inclusive and maintained counts. Documentation is not source for this limit.

## Coverage and mutation

Proposed `scripts/check_coverage.sh`: clean stale instrumentation, run pinned cargo-llvm-cov on host workspace including integration tests, export LCOV/JSON and require>=80% line coverage. Executable production logic remains in denominator; do not exclude FUSE/unsafe/network code to inflate score. Report hardware-only unexecuted paths separately, with reasons. Check that binary main.rs and library targets appear. [Official configuration/options](https://github.com/taiki-e/cargo-llvm-cov).

Proposed `scripts/run_mutation.sh`: pinned cargo-mutants, first engine/ring/snapper/state-machine/TS transform/reanchor, later remaining workspace. Capture exact files/features/test selection and outcomes JSON. Score = caught/(caught+missed+timeout)*100; unviable excluded but separately counted with compile errors. Timeout does not count as a kill. Require>=60% and no unresolved critical-state survivor. Start per-mutant60s, baseline suite timeout120s, overall CI job budget45min, calibrate with measured baseline and fail on incomplete run. Report timeouts, skipped/sharded mutants and survivors; only complete aggregate scope may pass. [Official project](https://github.com/sourcefrog/cargo-mutants), [outcome reporting history](https://mutants.rs/changelog.html).

## Fast local versus CI

Proposed `scripts/check_quality.sh` orchestrates documented fast/full modes and propagates each exit status. Local pre-commit reads STAGED blobs, not unstaged worktree: format check changed Rust/Python/shell as applicable, staged file size, changed-function CCN with cached tool. Warm normal budget<10s must be benchmarked,p95 recorded; if exceeded move expensive part to pre-push/CI. No full coverage/mutation pre-commit. Hooks optional locally, required identical CI policy cannot be bypassed by missing hooks.

Required CI jobs: pinned dependencies/cache keys (Cargo.lock+toolchain+tool versions+target), fmt; host Clippy all targets; CCN/line policies; host build/tests with known-failure inventory visible; Python isolation+tests/Ruff; shell syntax/ShellCheck for actual dialect; ARM static build and readelf/file validation; coverage>=80; separate mutation job>=60; security/license policy. Cache is not evidence a check ran. Publish artifacts and explicit skipped/BLOCKED results. No workflows/scripts created during this audit.

Known ignored critical regressions must be listed as FAIL in the quality summary, then promoted to required normal tests by their corrective tasks. Red regression patches may be reviewed but not represented as production-green. Avoid indiscriminate lint suppression and avoid host-driven ABI cast edits without ARM check. Enable targeted correctness lints; review unwrap/panic/conversion/unsafe with documented invariants rather than banning all unsafe FUSE code.

## Dependencies, security and reproducibility

CONFIRMED workspace has libc and socket2 dependencies, no internal crate edges/cycles. Keep low-level TS free of network/process ownership; contract.rs defines transport/backend seam. Python module import currently starts producers/threads; isolate startup before broad CI discovery (server.py:1893-1902,2549). Shell harness copied functions and Python `or True` assertion need targeted strengthening.

Proposed audit: cargo-audit/RustSec advisory database with recorded revision; cargo-deny license/source policy once project license and transitive inventory reviewed. Tool absence, stale DB or policy unknown is BLOCKED, not PASS. Document subprocess argument/shell boundaries, token/URL redaction, untrusted channel metadata/SSRF policy, TS lengths/PIDs, FUSE header/alignment/offset bounds, unsafe code inventory and Linux3.4 ABI. No vulnerability-free claim from this audit.

Installation procedure for later tooling task: choose and record exact versions after compatibility check; install Rust toolchain+rustfmt+clippy+llvm-tools-preview, ARM target/linker, then cargo install --locked --version <reviewed-pin> cargo-llvm-cov/cargo-mutants/rust-code-analysis-cli/cargo-audit/cargo-deny; install pinned Ruff/ShellCheck by checksummed package/lock. Resolve placeholders into committed version manifest before CI can pass; no unpinned latest downloads. Numeric pins are deliberately unresolved here rather than fabricated. This setup task is BLOCKED until version calibration is completed; existing offline baseline commands remain reproducible.
