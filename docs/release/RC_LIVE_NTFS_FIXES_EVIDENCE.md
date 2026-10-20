# RC-Live `fuse_ntfs` defect fixes — iteration-2 evidence

Host-only work. No deploy, no device/`/data`/gadget/live-provider access.
Branch `rc-001-iter2` in worktree `/home/sam/Code/rc001-iter2`; the main
checkout (`rc-001-live` @ `16d5991`) is untouched.

Method: failing test first (RED), then the fix (GREEN), per defect.

## Scope

| # | Defect | Status |
|---|--------|--------|
| 4 | ring check/copy TOCTOU race (`live_engine.rs`, `ring.rs`) | **fixed** |
| 5 | single `served.pts` shared across PIDs (`ts_xform.rs`) | **deferred — contract conflict (see below)** |
| 6 | CC seam ignores payload-less packets (`ts_xform.rs`) | **fixed** |
| 7 | root listed as its own child / empty-name dirent (`catalog.rs`,`dispatcher.rs`) | **fixed** |
| 8 | extent coalescing ignores file-offset continuity (`ntfs_mft.rs`) | **fixed** |
| 9 | `append` > capacity mispositions the suffix (`ring.rs`) | **fixed** |
| 1, 2 | `commit.rs` `last_fend` gating; probe/reanchor gating | **deferred — product intent unknown** |
| 3 | probe rejection preempts cold re-anchor (bookmarks ≥ 8 MiB) | **deferred — tracked as TASK-008** |

## Fixes

### #4 — ring check/copy TOCTOU (`src/engine/ring.rs`, `src/engine/live_engine.rs`)
The delivery loop snapshotted `ring_old`/`s_write` under the ring lock, released
it, then re-locked to copy. If the feeder overwrote the range in that gap the
reader could serve stale bytes. Added `RingData::copy_resident(pos, dst) -> bool`
(residency check **and** copy in one critical section) and the loop now refuses
overwritten ranges, stops the contiguous run and NULL-fills the remainder — the
same semantics as the `pos < ring_old` branch. No stale byte is ever served.

### #6 — CC seam must ignore payload-less packets (`src/engine/ts_xform.rs`)
`collect_raw` sampled the first CC per PID regardless of payload. An
adaptation-only packet repeats the previous payload-bearing CC, so using it as
the seam reference added a spurious `+1` to the delivered continuity counter.
`collect_raw` now records CC only from payload-bearing packets
(`ts_payload_off(pkt).is_some()`), so `cc_add = delivered + 1 - raw_cc` is exact
and payload-less packets correctly repeat the delivered value.

### #7 — root is not its own child (`src/vfs/catalog.rs`)
`Catalog::children(ino)` filtered on `n.parent == ino`; the root carries
`parent == ROOT_INO`, so it was listed as its own child with an empty name — an
invalid FUSE dirent. The filter now also requires `n.ino != ino`, so
`readdir_entries(ROOT_INO)` never emits a zero-length name.

### #8 — extent coalescing must not span sparse holes (`src/vfs/ntfs_mft.rs`)
`extents_from_runs` merged runs that were physically adjacent even when a sparse
hole separated them in VCN order, mapping hole offsets onto post-hole data.
Coalescing now also requires `last.file_end() == file_start` (file-offset
continuity).

### #9 — `append` larger than the ring (`src/engine/ring.rs`)
When a feed exceeded capacity, the retained suffix was written at
`phys(s_write)` instead of its own stream position, rotating the resident window
whenever the overshoot was not a whole capacity. It is now written at
`phys(s_write + skip)`.

## Tests added (RED first)

- `engine::ring::tests::copy_resident_refuses_overwritten_region` (#4)
- `engine::ring::tests::feed_larger_than_ring_positions_suffix_correctly` (#9)
- `engine::ts_xform::tests::seam_cc_reference_ignores_adaptation_only_packets` (#6)
- `vfs::catalog::tests::root_is_not_listed_as_its_own_child` (#7)
- `vfs::dispatcher::tests::readdir_has_no_empty_name_entry` (#7)
- `vfs::ntfs_mft::tests::extents_do_not_coalesce_across_sparse_holes` (#8)

RED (before the fixes), `cargo test -p fuse_ntfs --lib`:
`157 passed; 5 failed` — the five behavioural guards above failed
with the expected wrong values (rotation, `Some(4)` vs `Some(5)`, `1` vs `2`,
empty dirent, root self-child). The #4 guard is API-shaped (it targets the new
atomic path) and was captured as a compile-level RED.

GREEN (after the fixes):

- `cargo test -p fuse_ntfs --lib -- --test-threads=1` → **163 passed; 0 failed**.
- `cargo test -p fuse_ntfs --offline -- --test-threads=1` (full crate) →
  **221 passed; 0 failed; 3 ignored** (the 3 ignored are the two TASK-008
  bookmark guards plus one other pre-existing `#[ignore]`; none were touched).
  Baseline before this work: 215 passed / 3 ignored → +6 new tests.
- `integration_strict_contract` → **14 passed; 0 failed; 2 ignored** (the
  `#5`-sensitive `return_resume_seam_is_bit_exact` and
  `return_resume_arbitrary_phase_is_position_preserving` both remain green on the
  unchanged splice contract).
- Serial execution (`--test-threads=1`) is required: the Phase-B pause seam is a
  single global and hangs if the lib tests run in parallel.

## ARM rebuild (host-only, `armv7-unknown-linux-gnueabihf`, `+crt-static`)

Rebuilt at commit `5491aa3`. Only `fuse_ntfs` changed; the other four binaries
are byte-identical to the frozen `16d5991` release (so the frozen hashes still
verify for them).

| binary | sha256 | vs frozen 16d5991 |
|--------|--------|-------------------|
| `fuse_ntfs` | `24a57dbbe161993b33a79628feaeb59f83e69bfd6bd36b373e3b29959431976d` | **changed** (expected) |
| `fuse_direct` | `a6eb746872834a119a91537889b4b2d8d68ada58a206712d759df80d9a020f46` | identical |
| `stream_fetcher` | `e2c203e0d809d12cdb07155c800fc80c76e9680c8485b55c4ffe59057644806d` | identical |
| `patch_trp` | `3afd258f51ac45fccc3f35d262fbf5141b6090a710a23c9819619704ecf8015d` | identical |
| `sparse_unpack` | `e52a389f8281e330895fa3c372dc0ecc8d99ad0d51d122f147f4e4679cfe8f19` | identical |

The only build messages are the pre-existing static-`getaddrinfo` linker notes
(`stream_fetcher`, `fuse_direct`); no Rust warnings. Because the ARM
`fuse_ntfs` hash changes, the frozen release package/tag would need a re-baseline
before any deploy.

## Deferred: #5 (single shared PTS across PIDs)

The epoch applies a single `delta_pts` to every PID, so #5 only materialises
when the reference's last delivered PES is a *different* PID than the anchor's
first PES. A per-PID alignment was implemented and made the mobile/return
fixtures pass, but it changes the pinned splice contract:

- `tests/integration_strict_contract.rs:407` (`return_resume_seam_is_bit_exact`)
  asserts `delta_pts` **bit-exact** against the legacy shared-`pts` math
  (`want = last_pes_pts_any + SPLICE_PTS_EPS`, `raw = first_pes_pts_any`).
- `tests/integration_strict_contract.rs:275` `first_pes_pts_any` is documented
  as "mirrors `collect_raw`".

Changing this is a deliberate splice-semantics decision (and could touch the
validated TV behaviour), so it is **not** changed unilaterally here. Evidence:
with the per-PID variant, exactly that one contract test failed
(`left: 7000, right: 8000`). Options for review: (a) keep the contract and
document #5 as intentional; (b) adopt same-PID alignment and update the contract
test with sign-off.

## Deferred: #1, #2, #3

- #1 (`commit.rs:61-63` / `state_machine.rs:264-294`) and #2
  (`live_engine.rs:179-182`): behaviour may be by design. Need product intent —
  is the 1500 ms `last_fend` window mandatory? must a contiguous seek re-anchor
  to the live edge?
- #3: known bug, tracked as **TASK-008**; the two bookmark tests remain
  `#[ignore]`d and were not touched.