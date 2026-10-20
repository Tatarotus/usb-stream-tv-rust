# RC-001 — Release Candidate & TV Validation Plan

Status: **PLANNING / iteration 0.** This document inventories the current tree
and proposes a release/validation sequence. It does not authorize commits,
tags, code changes, new tests, deployment, USB/gadget operations, `/data`
access or any physical TV test.

## 1. Scope and first physical candidate

The first physical candidate is **RC-Live**:

- TASK-001–005 Rust/live-engine fixes and observability;
- TASK-010 Python import/startup isolation;
- TASK-006 Phase A slate/switching corrections;
- existing shell/watchdog behavior;
- **no Phase B parser integration**;
- no new refactor.

The parser (`ts_candidate_probe.py` and its tests) remains approved and frozen
as a repository artifact, but it must not be imported, called, packaged as an
active runtime component, or used as a readiness gate for RC-Live. A later
RC-Parser can add it after a separate GO.

The objective is to observe the already-approved behavior on the Samsung while
keeping the set of changes attributable and reversible.

This work was originally requested as TASK-011. It is recorded as **RC-001**
because the roadmap already reserves TASK-011 for Quality Gates & CI and
TASK-012 for hardware validation. The roadmap and frozen contracts are not
changed by this rename.

## 2. Repository inventory

### 2.1 Git baseline

```text
repository: /home/sam/Code/usb-stream-tv-rust
branch:     main
HEAD:       8581259bed94df3062e2c7bdef2d4707442ca106
last commit: fix(fuse_ntfs): fix pacing token bucket, add FOFF0 restart
             detection and telemetry
tags:       none
remotes:    none configured
```

HEAD is the last committed baseline. It does **not** contain the approved
working-tree changes, so HEAD alone is not a complete rollback to the currently
tested host behavior.

### 2.2 Working tree

```text
modified tracked files: 17
untracked files:        67
tracked diff:           17 files, +1634 / -523
diff fingerprint:       93f6dd38fddd7f34125baf5d7cc99f262546aaa07640a4800609249a7215f5c2
```

The fingerprint was computed from `git diff`, the sorted untracked-file paths
and their SHA-256 hashes, and `git diff --stat`. It is an inventory aid, not a
signed release identity.

Modified tracked groups:

```text
crates/fuse_ntfs/src:       13 files
crates/fuse_ntfs/tests:      1 file  (integration_seek_reanchor.rs)
scripts:                    1 file  (tv_watchdog.sh)
server.py:                  1 file
tests:                      1 file  (test_torrent_vod_stress.py)
```

Untracked groups:

```text
crates/fuse_ntfs/src:       17 files (feeder_phase, live_engine/, state_machine/,
                                      observability/, dispatcher/observation*)
crates/fuse_ntfs/tests:     11 files (flush, epoch, no-data, observability,
                                      recovery, return/switch, strict contract)
docs/agent_tasks:            9 files
docs/architecture:          24 files
tests:                       4 files (import isolation, slate switch, parser,
                                      watchdog shell)
root:                        2 files (ts_candidate_probe.py,
                                      fuse_ntfs_consolidado.rs)
```

The complete path list is reproducible with:

```sh
git ls-files --others --exclude-standard | sort
git status --short
```

`fuse_ntfs_consolidado.rs` (252 KiB) is an untracked, non-compiled consolidated
reference, not a workspace source. It is excluded from RC packaging unless the
auditor explicitly decides otherwise.

### 2.3 Approved artifact anchors

These values are tied to existing handoffs and/or current local files:

```text
b1c6b14a8ffc4584ec8d584c27456b73bd785b1b314c33d41c3775f2998832cb  server.py
c2bc0fb0ab03d30ae79518fc23e76f8612572308d65e174db1782f360763a705  tests/test_server_slate_switch.py
046e98c71a9a87aea6f2edad379a55c0593a1959074077816231de19a2ea66b2  docs/agent_tasks/TASK-006.md
add39d88cace9aab51e3b4cc09452f21b4785840fc5a820a88d49f0406fb0f6b  ts_candidate_probe.py
3ab467dc86f6b623473901c69b10a6c673b0b4acdfd116156df8e0025be17b73  tests/test_ts_candidate_probe.py
5cf0fe2e8af9820c5f1c1d7b723072b95f85c885e33aee394ecd275ad19e116a  docs/architecture/TASK_006_PHASE_B_HANDOFF.md
f00e9c1fed3ef56ac4d4c041a67204631b8a032ad231d53bfcd02a0589837b7a  crates/fuse_ntfs/src/engine/state_machine.rs
8ca1f2e84d36a3b99c3cbee582960b49f29e9d1e38ba742bdc73a9f227ecefd3  crates/fuse_ntfs/src/engine/live_engine.rs
88cab6545642796848f308ae83b9a841044c38f561af8b32cb7df9e114eec2bd  crates/fuse_ntfs/src/engine/live_engine/observation.rs
bf4e82d63660894e8355ee7458f4755432a28d290debbe77065230e5f1b50411  crates/fuse_ntfs/src/main.rs
483081b9ffe7df2a7925b8611145a6f09f617389a88cd2dd336bb03fee06ab0e  scripts/tv_watchdog.sh
909e2f066efb4ef2d64034546f11131c52216c920858e6bfec12b71ec3bae898  target/armv7-unknown-linux-gnueabihf/release/fuse_ntfs
```

The ARM executable is a static ARM32 EABI5 binary and is currently ignored by
Git (`target/`), so it is an external release artifact and must be included in
the manifest by hash. TASK-004/B's earlier handoff records a different ARM hash
(`2e8c6e730b04…`); the current `909e2f066efb…` artifact is the later TASK-005
build. They must not be treated as the same release binary.

### 2.4 Task/status inventory

| Task | Recorded status | Release impact |
|---|---|---|
| TASK-001 | Test artifact APPROVED; production defect remains separately scoped | Include as regression baseline only with TASK-002 |
| TASK-002 | APPROVED host patch | Include |
| TASK-003 | APPROVED / GO, host-only, in the later C1–C4 audit with 19 hashes verified. The local `TASK_003_R_AUDIT.md` still ends in CHANGES_REQUESTED and predates that correction round | Include, but preserve the later approval evidence in an auditable repository record before RC |
| TASK-004 | Phase B APPROVED / GO, host-only, after the F1b/F1c re-audit with ten hashes verified. The local handoff still says “awaiting re-audit” | Include, but preserve the later approval evidence before RC |
| TASK-005 | APPROVED / GO, host-only | Include |
| TASK-006/A | APPROVED / GO, host-only | Include |
| TASK-006/B parser | APPROVED / GO as isolated parser; integration NO-GO | Keep frozen and excluded from RC-Live runtime |
| TASK-010 | APPROVED / GO, host-only | Include |
| RC-001 | This planning record; replaces the originally requested TASK-011 label | Roadmap TASK-011/012 remain unchanged |

TASK-006/A and TASK-010 changed the same `server.py`; the final server hash is
the Phase A hash above. The older TASK-010 server hash is historical, not the
current file.

### 2.5 Dependencies and runtime configuration

Host tools observed:

```text
Python 3.14.7
pytest 9.1.1
cargo/rustc 1.98.1
FFmpeg/ffprobe n9.0.1
git 2.55.0
```

Rust workspace: five crates (`fuse_ntfs`, `fuse_direct`, `stream_fetcher`,
`patch_trp`, `sparse_unpack`). `Cargo.lock` exists (lockfile version 4, 17
packages) but is **ignored** by Git. `.cargo/config.toml` contains a
host-specific absolute ARM linker path:
`/home/sam/.local/opt/arm-gnu-toolchain/bin/...`.

Server packaging:

- `Dockerfile` uses `python:3.11-slim`, while host tests run Python 3.14.7.
- `apt-get` installs unpinned `ffmpeg`, `curl`, `quickjs`, `aria2`.
- `pip install yt-dlp` has no pinned version.
- `compose.yaml` uses `.env` and bind-mounts live source files and media
  directories.
- `.env` is ignored and is not to be read or packaged.
- `.env.example` contains placeholders only.
- `channels.json` is tracked; it must be checked for provider data before
  packaging. `catalog_cache/`, `favorites.json`, `series_favorites.json`,
  `server_events.log`, `.pytest_cache/`, `target/` and `__pycache__/` are
  ignored operational/cache artifacts and must be excluded.

No CI configuration, release tag, remote or signed artifact manifest was found.

## 3. Existing test evidence

Last local host-only run before this plan:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests
=> 230 passed, 41 subtests passed

cargo test -p fuse_ntfs --offline -- --test-threads=1
=> 215 passed, 0 failed, 3 ignored

bash -n tests/test_watchdog_observability.sh
sh -n scripts/tv_watchdog.sh
bash tests/test_watchdog_observability.sh
=> passed: 56, failed: 0

bash -n tests/test_vod_lock_and_elf.sh
bash tests/test_vod_lock_and_elf.sh
=> ALL UNIT TESTS PASSED SUCCESSFULLY
```

These are builder-declared host results, not hardware evidence. The parser
suite contributes 28 tests inside the Python total. `test_vod_lock_and_elf.sh`
and the FAT32 test should be explicitly accounted for in the RC manifest; they
must not be assumed covered by the Python count or the watchdog shell count.

## 4. Release-candidate strategy

### 4.1 Preconditions before any commit or package

1. Preserve the later TASK-003 (C1–C4, 19 hashes) and TASK-004/B (F1b/F1c,
   ten hashes) approval evidence in an auditable repository record. Do not
   rerun those audits; the stale local audit files remain historical evidence
   of the earlier rounds.
2. Confirm the RC-001 naming in the planning record; do not edit roadmap
   TASK-011 (quality gates) or TASK-012 (hardware validation).
3. Decide whether frozen parser files are:
   - kept in the repository but excluded from RC-Live runtime, or
   - omitted from the RC-Live branch entirely.
4. Run a secret scan and redact/sanitize any provider data before packaging.
5. Freeze tool versions and build inputs (see §4.4).
6. Capture the current working tree and current deployed artifacts as rollback
   evidence **before** any future deployment.

### 4.2 Proposed RC-Live package

Runtime contents:

- approved `server.py` (Phase A final) and its Python support modules;
- dashboard/PWA/static assets and slate generator/assets;
- approved Rust ARM binaries and shell/watchdog/switch/deploy scripts;
- sanitized `channels.json` and template assets;
- configuration examples, **not** `.env`;
- an artifact manifest with SHA-256 values and build commands.

Excluded:

- parser integration (the parser may remain in the repo as frozen evidence);
- `.env`, credentials, logs, caches, favorites, catalog caches;
- `target/`, `__pycache__/`, `.pytest_cache/`;
- `fuse_ntfs_consolidado.rs` unless separately approved;
- unrelated dirty files and temporary test outputs.

### 4.3 Future commit/tag sequence (NOT executed)

Do not commit now. When authorized, prefer one reviewable commit per task so a
failed behavior can be attributed/reverted independently:

```text
docs/contracts and audits
TASK-001/002 regression + delivery fixes
TASK-003 observability (preserve the later C1–C4 approval evidence first)
TASK-004/B flush/epoch atomicity
TASK-005 source-failure/gadget isolation
TASK-010 import/startup isolation
TASK-006/A slate/switching
frozen parser files (separate, non-runtime commit)
```

Suggested future refs:

```text
pre-rc-001-working-tree        backup of current uncommitted tree
rc-001-live                    candidate commit/branch
rc-001-live-artifacts          tag after host validation
rc-001-parser                  later, only after integration GO
```

Do not squash unrelated task changes into one opaque commit, and do not commit
ignored operational files.

### 4.4 Reproducibility controls

- Force-add and track `Cargo.lock` or document a locked build alternative.
- Pin a Rust toolchain and replace the absolute linker path with a documented
  environment/toolchain setting.
- Pin the Docker base image by digest and pin OS/Python/FFmpeg/yt-dlp versions.
- Record Python version, FFmpeg version, toolchain path and target triple.
- Build the ARM binary from the same commit as the server and record its hash.
- Produce a manifest listing every packaged file, hash, source revision,
  configuration template and test result.
- Prefer building in a clean temporary directory rather than the dirty
  workspace.

## 5. Host-only integrated validation plan (future; not implemented now)

These are new tests/scripts and require a separate authorization after this
plan is approved.

### H1 — Live/switch/slate simulation

Use local FFmpeg producers or deterministic file producers; no external
provider. Start the server in an isolated temporary configuration and inject:

- healthy live source;
- source freeze/stop;
- channel switch during healthy playback;
- slate/NULL entry and recovery;
- slow or late candidate;
- failed candidate acquisition.

Assert:

- byte continuity and 188-byte alignment at the existing server publication
  seam;
- old source/process remains alive until a valid commit;
- no duplicate or missing TS bytes around the existing first-chunk commit;
- slate/NULL behavior matches Phase A;
- failed switch preserves the old channel and retry state;
- no automatic USB/gadget action from a source failure.

RC-Live must not assert parser-owned fields such as `selected`, `trailing`,
parser deadlines or readiness reasons. Those belong to the later parser
integration test track below.

### H2 — Rollback simulation

Force a candidate failure and verify the error path keeps the previous
published stream, with bounded logs and no orphan workers. Verify the
operator-only recovery path is not automatically invoked.

### H3 — Parser isolation

Keep parser unit tests separate. RC-Live should prove that `server.py` never
imports or calls `ts_candidate_probe.py`.

### H4 — Packaging smoke

In a clean temp directory, start from the RC package (not the dirty tree),
verify imports, dashboard/control-plane tests, no provider/network access and
no `/data`/gadget writes. Compare packaged hashes with the manifest.

### H5 — Existing regression commands

Run the existing Python, Rust and shell suites offline in an isolated
`CARGO_TARGET_DIR`; include `test_vod_lock_and_elf.sh`, FAT32 integrity and the
watchdog harness in the final matrix. Do not use `--ignored` to hide known
failures.

### H6 — Phase B parser integration (future RC-Parser only)

After a separate integration GO, test the `selected`/`trailing` publication
contract, parser deadlines, fragmented PSI/PES/NAL handling and readiness
reasons at the real switch seam. These are parser-integration assertions and
must not appear in the RC-Live acceptance matrix.

## 6. Rollback design

### 6.1 Backups before any future deployment

- Exact current working-tree archive/fingerprint and Git HEAD.
- Current server image/compose/config hashes and the last known-good container.
- Current tablet binaries, scripts, templates, ownership/modes and active
  configuration hashes.
- Current USB/gadget state, mount state and TV-visible file identity.
- Current logs sufficient to identify the last known-good run, with secrets
  redacted.

Do not reset the gadget merely to create a backup. Preserve the existing USB
state and avoid unnecessary re-enumeration.

### 6.2 Rollback levels

1. **Server rollback:** restore the previous image/compose or previous
   `server.py`/static assets; restart only the server.
2. **Tablet binary/script rollback:** restore the previous ELF/scripts from the
   backup manifest; do not rewrite the 128 GiB template unless required.
3. **USB/gadget rollback:** only through the existing operator-controlled
   `switch_tv_mode.sh`/watchdog path after an explicit decision. No automatic
   source-failure reset, no marker creation as a test shortcut.
4. **Full stop:** leave the TV on the last known-good source/slate and stop
   experimentation; do not keep restarting a failing stack.

### 6.3 Rollback triggers

- persistent decoder freeze or no video after the agreed short window;
- repeated SCSI/USB errors or unsolicited disconnect;
- source failure causes a gadget reset;
- runaway RSS/FD/thread growth or unbounded queues;
- slate/NULL is not recoverable by a normal source return;
- any evidence of provider/credential exposure.

Rollback is not a failure of the test; it is the required safety outcome.

## 7. First controlled Samsung test plan

Only after RC-Live and the rollback package are separately approved.

### Preflight

- Record TV model/input, tablet state, deployed artifact hashes and config.
- Confirm no parser integration.
- Confirm host tests and package manifest.
- Confirm operator is present and knows how to stop/restore.
- Capture baseline: TV playback works for the current known-good setup.

### Short sequence

1. Boot/attach RC-Live; play live for a bounded short interval.
2. Switch channels several times; verify no freeze and old fallback behavior.
3. Freeze/stop the local producer; observe slate/NULL and recovery.
4. Force one failed switch; verify old channel remains and no USB reset occurs.
5. Return to normal live; capture logs/screen/resource samples.
6. Stop and restore the backup; verify the TV still works.

### Observability

- TV screen/time code and observed freeze onset/recovery.
- Server structured events and candidate/commit boundaries.
- Tablet process/RSS/FD/thread counts, USB/gadget and SCSI events.
- Watchdog decisions, explicit resync markers and reset count.
- Exact deployed hashes and configuration.

### Abort criteria

Abort immediately on persistent freeze, unsolicited USB reset, unrecoverable
slate, credential exposure, runaway resources or any need to improvise a
gadget reset. Restore the previous artifact; do not continue the matrix.

A longer 2 h/24 h soak is a later authorization after the short sequence and
rollback rehearsal pass.

## 8. Risks and open decisions

| Risk / decision | Impact | Required resolution |
|---|---|---|
| TASK-003 approved in the later C1–C4 audit, but local audit file is stale | Written approval evidence is not preserved in the repository | Add an auditable approval record before RC; do not rerun the audit |
| TASK-004/B approved after F1b/F1c, but local handoff is stale | Written approval evidence is not preserved in the repository | Add an auditable approval record before RC; do not rerun the audit |
| Original TASK-011 label collided with roadmap quality gates | Confusing contracts/commits | RC-001 naming resolves it; roadmap remains unchanged |
| 67 untracked files and 17 modified tracked files | Accidental inclusion/omission | Commit by task and use explicit package allowlist |
| No tags/remotes; uncommitted approved work | No reproducible rollback point | Create backup refs only after authorization |
| `.env` and operational caches present | Credential/log leakage | Secret scan; exclude ignored files |
| `Cargo.lock` ignored; absolute linker path | Non-reproducible Rust build | Track lock and parameterize toolchain |
| Unpinned Docker/FFmpeg/yt-dlp/Python | Host/container drift | Pin versions/digests; test target runtime |
| Host Python 3.14 vs Docker 3.11 | Test/runtime mismatch | Validate candidate on target runtime |
| ARM artifact is ignored and built outside Git | Missing/mismatched binary | Manifest hash and rebuild from RC commit |
| `fuse_ntfs_consolidado.rs` reference file | Accidental packaging | Exclude unless explicitly approved |
| Parser approved but not integrated | Do not attribute TV behavior to parser | RC-Live excludes integration |
| No device baseline/rollback hashes | Unsafe physical change | Capture before test; operator present |

## 9. Audit request

Please audit this plan only and confirm:

1. the inventory and approval-status matrix, including the stale local audit
   records for the later-approved TASK-003 and TASK-004/B;
2. the RC-Live scope and the exclusion of parser integration;
3. the proposed task-scoped commit/tag sequence and packaging allowlist;
4. the host-only test matrix (no new tests implemented yet), with parser-owned
   assertions kept out of RC-Live;
5. the rollback levels, operator-only USB recovery and abort criteria;
6. the first physical test sequence and observability requirements;
7. the RC-001 naming and dependency-pinning decisions.

No commit, tag, branch, source edit, new test, deployment, `/data` access, USB
reset or TV operation was performed while preparing this plan.
