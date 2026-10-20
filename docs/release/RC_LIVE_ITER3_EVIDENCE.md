# RC-Live — iteration 3 evidence (host-only release consolidation)

Independent audit of iteration 2 returned **PLAN**: the five `fuse_ntfs` fixes
(#4/#6/#7/#8/#9) passed static inspection, but the new revision had to be
consolidated as a separately identified release candidate before any physical
work. This document records that consolidation. Nothing here touches a device,
`/data`, the USB gadget, a provider or `.env`.

## What changed

### 1. `scripts/verify_rc_live_release.sh` — build the requested revision

The script resolved the requested revision to a commit but then built the
**current working tree**, so naming a tag could hash an unrelated HEAD.

- RED (before): at HEAD `335c855` the old script compared the current-tree
  `fuse_ntfs` (`24a57dbb…`, ITER#2) against the frozen `909e2f06…` and failed.
- GREEN (after): when HEAD differs from the revision, the script creates a
  temporary detached `git worktree` of that revision, builds it into its own
  per-revision target directory (`target/verify/<short-sha>`), verifies, and
  removes the worktree on exit. When HEAD equals the revision it builds in place
  against `target/`.
- A first implementation shared one `CARGO_TARGET_DIR` between the worktree and
  the in-place build. Cargo then reported the in-place outputs as up to date
  while the worktree build had already overwritten those same binaries on disk,
  so the hash check compared against the wrong build and failed. Each build root
  now owns its target directory; this is why the packaging step verifies before
  it copies anything.

The expected hashes are now chosen by the `# Revision:` line of
`docs/release/RC_LIVE_ARM_SHA256*.txt` (matched against the commit, short
commit, the revision as typed and any tag pointing at the commit), so a tree may
hold several records instead of one overloaded file.

Demonstrated result — verifying the frozen tag while HEAD is the new revision:

```text
$ scripts/verify_rc_live_release.sh rc-001-live-16d5991
==> revision commit: 16d5991c6868fca4b109fd0ee38b11106b10af58
==> HEAD 5b685db... differs from the revision; building an isolated worktree
==> building armv7-unknown-linux-gnueabihf (offline) from /tmp/rc-live-verify.XXXXXX
==> checking ARM binary hashes against .../docs/release/RC_LIVE_ARM_SHA256.txt
OK      fuse_ntfs  909e2f066efb4ef2d64034546f11131c52216c920858e6bfec12b71ec3bae898
OK      fuse_direct  a6eb746872834a119a91537889b4b2d8d68ada58a206712d759df80d9a020f46
...
==> RESULT: PASS
```

### 2. `live_engine.rs` back under the 500-line budget

The ITER#2 fix #4 grew `serve_live` and took the file to 513 lines, above the
documented 500-line limit for maintained sources. The fragment-delivery loop is
extracted to `crates/fuse_ntfs/src/engine/live_engine/deliver.rs`
(`LiveEngine::deliver_fragments`, returning a small `Delivery` struct), matching
the existing `anchor.rs`/`commit.rs`/`observation.rs` pattern.

- `live_engine.rs`: 513 → **457** lines; `deliver.rs`: 108 lines.
- Behaviour unchanged: the delivery loop body is moved verbatim (overwritten
  ranges NULL-filled, edge wait within `EDGE_WAIT_BUDGET_MS`, and the resident
  copy remains residency-checked and copied under one critical section).
- Test parity proves it: the crate result is identical before and after
  (221 passed / 0 failed / 3 ignored).

### 3. Manifest regenerated

`docs/release/RC_LIVE_RUNTIME_MANIFEST.sha256` was stale — it still described
the six Rust files as they were before the ITER#2 fixes. Regenerated over the
allowlist:

```text
files: 94
manifest_sha256: 68a3e2a41f6e868133fa8d68fd378f0dbe34a8f16681ba34965fb356ee7d75b8
```

`sha256sum -c docs/release/RC_LIVE_RUNTIME_MANIFEST.sha256` → all OK.

### 4. ARM binaries and a new hash record

Rebuilt for `armv7-unknown-linux-gnueabihf` (static). Four binaries are
byte-identical to the frozen 16d5991 release; only `fuse_ntfs` changed. The
frozen record `docs/release/RC_LIVE_ARM_SHA256.txt` is left untouched;
`docs/release/RC_LIVE_ARM_SHA256_iter3.txt` records the new candidate.

| binary | sha256 | vs frozen 16d5991 |
|--------|--------|-------------------|
| `fuse_ntfs` | `d5a973d6c9194d33c8654767d00e7b6073ca65b30107a717a6a9baa143399f55` | **changed** |
| `fuse_direct` | `a6eb746872834a119a91537889b4b2d8d68ada58a206712d759df80d9a020f46` | identical |
| `stream_fetcher` | `e2c203e0d809d12cdb07155c800fc80c76e9680c8485b55c4ffe59057644806d` | identical |
| `patch_trp` | `3afd258f51ac45fccc3f35d262fbf5141b6090a710a23c9819619704ecf8015d` | identical |
| `sparse_unpack` | `e52a389f8281e330895fa3c372dc0ecc8d99ad0d51d122f147f4e4679cfe8f19` | identical |

### 5. Transport test complemented (audit recommendation)

`tests/test_rc001_ts_smoke.py`:

- `test_valid_ffmpeg_ts_survives_hub_and_decodes` is parametrised over
  `mpeg2` (MPEG-2/MP2) **and** `h264` (H.264/AC-3), so the hub transport path
  is exercised with the codec profile actually destined for the Samsung set.
- `test_real_restamper_keeps_video_pts_forward_across_channel_switch` drives the
  **real** `server.SeamlessRestamper` (not the pass-through stub) across a
  channel switch and asserts video PTS is forward within each channel and
  anchored strictly above the previous channel, and that both restamped streams
  still decode. B-frames are disabled so PTS is monotonic without heuristics.

## Test evidence (host-only)

```text
cargo test -p fuse_ntfs --offline -- --test-threads=1
=> 221 passed, 0 failed, 3 ignored
   (lib 163/0; integration_strict_contract 14/0/2 ignored)

PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests
=> 241 passed, 41 subtests passed

bash tests/test_watchdog_observability.sh
=> passed: 56  failed: 0

bash tests/test_vod_lock_and_elf.sh
=> ALL UNIT TESTS PASSED SUCCESSFULLY

scripts/verify_rc_live_release.sh rc-001-live-16d5991
=> RESULT: PASS (isolated worktree build of the frozen tag)
```

Serial execution (`--test-threads=1`) remains required: the Phase-B pause seam
is a single global and hangs under parallelism.

## Deferred (unchanged from iteration 2)

- #5 — per-PID PTS alignment still contradicts the pinned splice contract
  (`return_resume_seam_is_bit_exact`); a splice-semantics decision, not a fix.
- #1/#2 — need product intent (1500 ms `last_fend` window; contiguous seek
  re-anchoring to the live edge).
- #3 — remains TASK-008; its two bookmark tests stay `#[ignore]`d.

## Still pending before a physical test

1. Prove backup + restore of the components actually installed on the tablet
   and the server (requires device access — needs explicit operator approval).
2. A short, supervised Samsung test on `Canais TV` / `TV AO VIVO.trp`.