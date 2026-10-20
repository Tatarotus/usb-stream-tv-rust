# RC-Live — ITERATION 2 host-only evidence

Response to the independent review verdict `STATE: PLAN` (TASK_ID: RC-001,
iteration 1). This iteration executes **only** the two host-only items; the
operational-rollback item (3) is intentionally not started because it needs
device access and explicit approval.

**No deploy, no physical test, no `/system`, no USB gadget, no `/data`, no
live provider, and no change to the frozen release package.**

Work was done in an isolated git worktree on branch `rc-001-iter2` based on
the tag commit, leaving the original checkout untouched.

## 1. Annotated tag confirmed

```
$ git cat-file -p rc-001-live-16d5991
object 16d5991c6868fca4b109fd0ee38b11106b10af58
type commit
tag rc-001-live-16d5991
tagger Alabatrus <shamuelbaruch@gmail.com> 1792511699 -0300

RC-001 RC-Live host-validated candidate
Commit: 16d5991c6868fca4b109fd0ee38b11106b10af58
Package: /home/sam/Code/rc001-release/rc-001-live-16d5991
Manifest: a079098d3465253e987412c2456f167936d8c8509cf8317824bb068876868a18
...
$ git rev-parse 'rc-001-live-16d5991^{}'
16d5991c6868fca4b109fd0ee38b11106b10af58
```

The tag is annotated and peels to commit `16d5991`.

## 2. ARM binary reproducibility

Built from the tagged revision in a clean worktree:

```
cargo build --workspace --release --target armv7-unknown-linux-gnueabihf --offline
```

All five binaries reproduce the frozen SHA-256 values
(`docs/release/RC_LIVE_ARM_SHA256.txt`) exactly, and are byte-identical to the
binaries in the assembled package:

| binary | sha256 |
| --- | --- |
| `fuse_ntfs` | `909e2f066efb4ef2d64034546f11131c52216c920858e6bfec12b71ec3bae898` |
| `fuse_direct` | `a6eb746872834a119a91537889b4b2d8d68ada58a206712d759df80d9a020f46` |
| `stream_fetcher` | `e2c203e0d809d12cdb07155c800fc80c76e9680c8485b55c4ffe59057644806d` |
| `patch_trp` | `3afd258f51ac45fccc3f35d262fbf5141b6090a710a23c9819619704ecf8015d` |
| `sparse_unpack` | `e52a389f8281e330895fa3c372dc0ecc8d99ad0d51d122f147f4e4679cfe8f19` |

Reproduce/verify with:

```
scripts/verify_rc_live_release.sh rc-001-live-16d5991 [package_dir]
```

Current result: `==> RESULT: PASS`.

## 3. Manifests

**Assembled package** (`/home/sam/Code/rc001-release/rc-001-live-16d5991`):

```
$ sha256sum -c MANIFEST.sha256   # exit 0
112 entries, 112 OK, 0 bad
$ sha256sum MANIFEST.sha256
a079098d3465253e987412c2456f167936d8c8509cf8317824bb068876868a18
```

**Repository runtime manifest** (generated, MCP-visible):

```
$ python3 scripts/gen_rc_live_runtime_manifest.py \
      --out docs/release/RC_LIVE_RUNTIME_MANIFEST.sha256
files: 91
manifest_sha256: 2eaf01cccc76011c6885a2bdf982b92d0194be430daaad6f7c5caac3d2610b62
```

- `sha256sum -c docs/release/RC_LIVE_RUNTIME_MANIFEST.sha256` → OK
- Deterministic: a second run is byte-identical.

## 4. Python 3.11 validation (image interpreter)

The Dockerfile runs `python:3.11-slim`. The reported host tests used 3.14.7.
The full Python suite was re-run on an actual 3.11 interpreter:

| interpreter | result |
| --- | --- |
| CPython 3.14.7 (host) | 239 passed, 41 subtests passed |
| CPython 3.11.17 (`uv`, image ABI) | 239 passed, 41 subtests passed |

**Limitation (not hidden):** this host has no container runtime
(`docker`/`podman` absent), so the `python:3.11-slim` **image itself was not
built**. The mitigation is running the suite on a real 3.11 interpreter. The
image build remains unproven and must not be claimed as verified.

## 5. New / updated tests (item 2)

- `tests/test_rc001_ts_smoke.py` — real ffmpeg-generated, decodable MPEG-TS
  pushed through the real `StreamHub` reader/broadcast path; asserts byte
  identity, 188-byte packet alignment and sync bytes, and decodes the exact
  published bytes back with ffmpeg. Skips cleanly if ffmpeg is missing.
- `tests/test_server_import_isolation.py` — deterministic **empty-catalog**
  import regression: importing `server` with an empty catalog cache starts no
  thread and no network, `_needs_initial_sync` is scheduled, and the deferred
  sync runs exactly once on explicit start.

Python test count moved from 237 → **239** (2 new).

## 6. Release gates run in this iteration

| gate | result |
| --- | --- |
| Python suite (3.14) | 239 passed / 41 subtests |
| Python suite (3.11) | 239 passed / 41 subtests |
| Rust `cargo test -p fuse_ntfs` | 215 passed / 0 failed / 3 ignored |
| `tests/test_watchdog_observability.sh` | 56 passed / 0 failed |
| `tests/test_vod_lock_and_elf.sh` | ALL UNIT TESTS PASSED |
| ARM rebuild vs frozen hashes | PASS |
| Package manifest `sha256sum -c` | 112/112 OK |
| Runtime manifest (generated) | 91 files, verifies, deterministic |

## 7. Explicitly not done

- No deploy, physical TV test, `/system` change, USB gadget reconfiguration,
  `/data` access or live-provider call.
- Item 3 (operational rollback capture from server/tablet) not started —
  requires device access and explicit approval.
- The inert TASK-006/B parser remains excluded and unreferenced.
- The frozen release package was not modified.