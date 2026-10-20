# RC-001 — Baseline preservation and approval reconciliation

Status: **host-only baseline record.** No deployment, TV, Android, USB/gadget
or real `/data` operation is authorized by this document.

Date: 2026-10-20.

## 1. Backup

Before any commit or build, the complete dirty working tree was archived
excluding only regenerable build/cache output (`target/`, `.pytest_cache/`,
`__pycache__/`). The archive includes `.git`, all tracked modifications, all
untracked source, docs, tests, scripts, configuration and runtime JSON, and
the pre-RC ARM binary copy.

```text
backup dir: /home/sam/Code/rc001-backups/rc001-working-tree-20261020T153254Z
working-tree.tgz sha256:
  9b4014219c43f72bc0304124f8880bd0769f39c1329bdf15ebb96c356fc36377
tracked.diff sha256:
  06646fe38a062895d254520285e332ad56ef3f68eed2956a8dd6c68654d21f94
pre-RC ARM binary sha256:
  909e2f066efb4ef2d64034546f11131c52216c920858e6bfec12b71ec3bae898
```

The archive was extracted to a temporary directory and compared with the live
tree; no differences were found outside the excluded regenerable directories.

## 2. Approval reconciliation

The local audit records are historical and predate the final correction
rounds. The later approvals below were confirmed during the RC-001 planning
audit and are the basis for including the code in RC-Live. The stale local
files are preserved unchanged.

| Task | Local record | Later approval evidence | RC-Live decision |
|---|---|---|---|
| TASK-003 | `TASK_003_AUDIT.md` and `TASK_003_R_AUDIT.md` end in CHANGES_REQUESTED, before the final C1–C4 corrections | RC-001 planning audit: APPROVED / GO host-only after C1–C4, 19 hashes verified | Include; local record remains historical |
| TASK-004/B | `TASK_004_B_HANDOFF.md` says awaiting re-audit, before the F1b/F1c re-audit | RC-001 planning audit: APPROVED / GO host-only after F1b/F1c, ten hashes verified; TASK-005 audit independently confirmed the TASK-004/B frozen hashes | Include; local record remains historical |
| TASK-005 | APPROVED / GO host-only | 9/9 TASK-005 hashes and 8/8 TASK-004/B frozen hashes confirmed | Include |
| TASK-006/A | APPROVED / GO host-only | Phase A final hashes frozen | Include |
| TASK-006/B parser | APPROVED / GO as isolated parser; integration NO-GO | Parser iteration 2 approved and frozen | Keep as inert repository artifact only; **not integrated** |
| TASK-010 | APPROVED / GO host-only | 6/6 TASK-010 hashes and 6/6 predecessor hashes confirmed | Include |

## 3. Current TASK-003 hashes

These match the TASK-003 corrected handoff and are the files included in the
TASK-003 commit:

```text
7fbafc59b152927cbdf7c2f3e30d949aa2402f82cb9323b44c0fb1e328b1fce4  observability.rs
fe1b607f2f62a4c3b54932d68aa11f0e2355158de2ab5dfc5146dc1685cfd470  observability/budget_tests.rs
155fd16efdbf8e6fef282b9aef7b4c5d39704009530e5974bf4015907ff598aa  observability/format.rs
8db892ca3bd115055bb931c1d2f82661820e65b966657379b8c92f20dc339880  observability/format_tests.rs
7ba898a7c7450dfa0c4b2283a172b15d492610279ebf3b90f524257521e0a30e  observability/gate.rs
0b31757376b19a04b4c5451a865615127dc049e55cafe2cf65e11d8e4c7e5f52  observability/sinks.rs
18cb532e1f22c933b26cba31198972c751371b74bd3b8dd97cf793a3e322555e  observability/tests.rs
f5c61167390108640201a950db2335b31f8a4d8f86c477d6040129944ebecfe6  observability/writer.rs
3df0db35f04bd11bb356eb790d7b78bc964df34734704e508c095522f744df75  vfs/dispatcher.rs
b25a6e8ce3e859059b8f1a6bf290497056b05f6c64426d882d89598c8bb4c587  vfs/dispatcher/observation.rs
aaa3a58b16b4ca2d8ac437a836a35579f354a2b17b1a2238f34053979fd39e05  vfs/dispatcher/observation_tests.rs
dd413a7872e4d4a8f4cc741ee8b3b5a646394205fae7ef76c14a181d48597553  tests/test_watchdog_observability.sh
```

`live_engine/observation.rs`, `live_engine.rs`, `state_machine.rs` and
`scripts/tv_watchdog.sh` were later modified by TASK-005; their current hashes
are recorded in section 4 and they are committed with TASK-005 instead.

## 4. Current TASK-004/B and TASK-005 hashes

TASK-004/B files:

```text
bf888b020c9488f70ffb387faa9fcc2385cf61b9111764f5874110008a668573  engine/feeder_phase.rs
a012936a92daa305d06314bf9c41e8e398664cbd86797cdcbcbeafb9b40affae  engine/live_engine/anchor.rs
2aefc892bb8efde727d0769a9c5834d7980a1c5ca60faaec6e84412dc010622b  engine/live_engine/commit.rs
2272fa0637b32d97d9092481384a0a5ea1b065347227ce24cb4fae58f2e36681  engine/live_engine/test_seam.rs
2841a8f55bd50945d456551e71d09569369d79234c731ddd70d857eed808441d  engine/live_engine/phase_b_tests.rs
bf4e82d63660894e8355ee7458f4755432a28d290debbe77065230e5f1b50411  main.rs
```

TASK-005 final files:

```text
f00e9c1fed3ef56ac4d4c041a67204631b8a032ad231d53bfcd02a0589837b7a  engine/state_machine.rs
20902eea9ed9b28949e8fb52412f7adde098c224ba820ffff6c669f8cba15a99  engine/state_machine/overwritten_tests.rs
8ca1f2e84d36a3b99c3cbee582960b49f29e9d1e38ba742bdc73a9f227ecefd3  engine/live_engine.rs
88cab6545642796848f308ae83b9a841044c38f561af8b32cb7df9e114eec2bd  engine/live_engine/observation.rs
483081b9ffe7df2a7925b8611145a6f09f617389a88cd2dd336bb03fee06ab0e  scripts/tv_watchdog.sh
0047c390e34054d4edeaf92ad19264408598b0eda40100b2ed75a280f8d5a535  tests/integration_strict_contract.rs
76ba78d973eded78315a666a0c521b8f05afb41642d6153d6593e92037462c47  tests/integration_seek_reanchor.rs
8fd153ae27cc9226793987276d098e4f302103fdbce8637c80bab379ba850b65  tests/integration_source_recovery.rs
```

## 5. Build and toolchain inputs

```text
host:        x86_64-unknown-linux-gnu
target:      armv7-unknown-linux-gnueabihf (static)
cargo/rustc: 1.98.1
python:      3.14.7
pytest:      9.1.1
ffmpeg:      n9.0.1
```

`Cargo.lock` is present but ignored by `.gitignore` (`*.lock`). RC-001 adds an
explicit `!Cargo.lock` exception and tracks the lockfile. The ARM linker path
in `.cargo/config.toml` is host-specific; the release manifest records the
exact path and the RC build script will allow an explicit override.

Remaining reproducibility risks, deferred with no code impact:

- Docker base image and `apt`/`pip` packages are not pinned by digest.
- `Dockerfile` targets Python 3.11 while host tests run 3.14.7.
- The ARM toolchain path is absolute and not part of the repository.

## 6. Commit attribution used for RC-Live

The working tree spans several approved tasks. To keep commits reviewable
without splitting historical hunks blindly, files are attributed to the last
task that changed them:

```text
TASK-001/002 baseline : contract, engine/mod, ring, snapper, ts_xform, lib,
                        vfs/catalog, vfs/fuse_abi, vfs/ntfs_mft,
                        state_machine/overwritten_tests,
                        integration_no_data_progress, tests/no_data_progress,
                        integration_return_and_channel_switch
TASK-003 observability: observability*, vfs/dispatcher, observation*,
                        tests/observability*, integration_observability,
                        test_watchdog_observability.sh
TASK-004/B            : feeder_phase, live_engine/anchor, commit, test_seam,
                        phase_b_tests, main.rs, integration_flush_phase,
                        integration_epoch_flush
TASK-005              : state_machine, live_engine, live_engine/observation,
                        tv_watchdog.sh, integration_strict_contract,
                        integration_seek_reanchor, integration_source_recovery
TASK-006/A            : server.py, test_server_slate_switch.py
TASK-010              : test_server_import_isolation.py,
                        test_torrent_vod_stress.py
parser (inert)        : ts_candidate_probe.py, test_ts_candidate_probe.py
docs                  : docs/**
```

`fuse_ntfs_consolidado.rs` remains untracked and excluded. `.env`, logs,
caches, media directories, `favorites.json`, `series_favorites.json` and
`target/` are excluded from commits and from the runtime package.

## 7. Approval record

This record captures the reconciliation required before RC packaging. It does
not modify any frozen audit, handoff, contract, parser or Phase A artifact.