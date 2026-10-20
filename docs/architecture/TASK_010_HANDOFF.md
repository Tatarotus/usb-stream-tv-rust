# TASK-010 — host-only handoff

Status: **host-only implementation complete; awaiting independent audit.**
No deployment, production server launch, live provider access, USB/gadget
operation, hardware action, `/data` access or C-reference edit was performed.

## 0. Objective and disposition

| Item | Disposition |
|---|---|
| `import server` starts `StreamHub`, ffmpeg and two threads | **Fixed.** Module-level `HUB` is constructed with `start_workers=False`; `run()` calls `HUB.start()` before serving. |
| Direct `StreamHub()` behavior | **Preserved.** The default remains `start_workers=True`; external callers still get a started hub. |
| Safe test discovery | **Confirmed.** A clean-subprocess test patches process/thread/network entry points and imports `server` without a call. |
| Vacuous `or True` stress assertion | **Fixed.** The unreadable-file test now uses deterministic walk order and a real selected-path oracle. |
| TASK-001..005 / TASK-004-B | **Preserved.** No Rust, shell harness, gadget, watchdog or frozen-file edit. |

## 1. Task-start and final hashes

Task-start (before TASK-010 edits):

```
e8744346c1383e3799ff089cc916bd164fed2d8ca55c36ab4b824a8792b0a272  server.py
fc7da89f5907a2f553a88d95bb602e6b39c5bd9a20c33f1ae9c641627e175989  tests/test_server_control_plane.py
cf2fd3c47b0cbffa4f698f26a8df04a8557cb990216fc8d7c1f3170d8df53d50  tests/test_probe_sar.py
54a3d6a4504da3ab9acc6c9af05daee981cd063bcbe1af71a8e0692a1d03060e  tests/test_torrent_vod_stress.py
```

Final:

```
937bf965db296ad57a389d54c8235488f6ed7ddc172235265f02cb83b6f2ce3c  server.py
b9b5f5e0ee175f16dfb7c801ddc4d58cd3cfd0272d85801dd5e647d32bffb40f  tests/test_server_import_isolation.py
c57d55e802e7be71bb5a3ed05e7c951938b5d8fd7dc18706521f6a9fc6cb4a6c  tests/test_torrent_vod_stress.py
cf2fd3c47b0cbffa4f698f26a8df04a8557cb990216fc8d7c1f3170d8df53d50  tests/test_probe_sar.py
fc7da89f5907a2f553a88d95bb602e6b39c5bd9a20c33f1ae9c641627e175989  tests/test_server_control_plane.py
fe6c5386c34411f08c1763c07c3d76aa169547c8205d77104a0ea3ff8c9c3192  docs/agent_tasks/TASK-010.md
909e2f066efb4ef2d64034546f11131c52216c920858e6bfec12b71ec3bae898  target/armv7-unknown-linux-gnueabihf/release/fuse_ntfs
```

Line counts:

```
10755  server.py
  115  tests/test_server_import_isolation.py
  693  tests/test_torrent_vod_stress.py
  148  docs/agent_tasks/TASK-010.md
```

`test_probe_sar.py` and `test_server_control_plane.py` are byte-identical to
task start; the unstarted HUB did not require changes there.

## 2. Code changes

`server.py`:

- `StreamHub.__init__` now takes `start_workers=True`.
- The initial ffmpeg producer and both worker threads moved into the new
  idempotent `StreamHub.start()`.
- `reader_thread` and `keepalive_thread` start as `None`; `_started` guards
  double start.
- Module level now uses `HUB = StreamHub(start_workers=False)`.
- `run()` calls `HUB.start()` before constructing `ThreadedHTTPServer`.

`tests/test_server_import_isolation.py` (new):

- Runs `import server` in a clean subprocess with `subprocess.Popen`,
  `threading.Thread.start`, `socket.socket.connect` and
  `urllib.request.urlopen` patched to record/raise.
- Asserts no side-effect call, `HUB._started is False`, no worker threads and
  no producer.
- Tests `run()` calls `HUB.start()` exactly once before serving.
- Tests default `StreamHub()` still calls `start()`.
- Tests explicit `StreamHub(start_workers=False)` plus idempotent `start()`
  with mocked `_start_initial` and mocked threads.

`tests/test_torrent_vod_stress.py`:

- Replaced the always-true `... in (None, ..., mock.ANY) or True` assertion.
- The test now fixes walk order (`bad.mkv`, then `good.mp4`), makes `getsize`
  raise `OSError` for the first candidate and returns `100` for the second,
  and asserts `find_main_video_file()` returns the readable path.

## 3. Validation results

Import isolation:

```
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  tests/test_server_import_isolation.py
=> 4 passed
```

Control plane and SAR:

```
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  tests/test_server_control_plane.py tests/test_probe_sar.py
=> 22 passed
```

Unreadable-file oracle:

```
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  tests/test_torrent_vod_stress.py -k unreadable_file_skipped
=> 1 passed, 107 deselected
```

Full Python suite (after the import fix):

```
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests
=> 188 passed, 41 subtests passed
```

Rust regression (no Rust files changed):

```
cargo test -p fuse_ntfs --offline -- --test-threads=1
=> 215 passed, 0 failed, 3 ignored
```

Shell harness (no shell files changed):

```
bash -n tests/test_watchdog_observability.sh
sh -n scripts/tv_watchdog.sh
bash tests/test_watchdog_observability.sh
=> passed: 56  failed: 0
```

ARMv7 release build (no Rust source change; artifact unchanged):

```
cargo build --release --target armv7-unknown-linux-gnueabihf --offline
=> finished successfully
909e2f066efb4ef2d64034546f11131c52216c920858e6bfec12b71ec3bae898
ELF 32-bit LSB executable, ARM, EABI5, statically linked, stripped
```

The Python suite uses only synthetic local media generated by ffmpeg and a
loopback `127.0.0.1` fixture server in `test_probe_sar.py`; there is no live
provider, external network probe or production server launch. The linker
emits the known static `getaddrinfo` warning for `stream_fetcher`/`fuse_direct`;
it is unrelated to TASK-010 and the `fuse_ntfs` artifact hash is unchanged.

## 4. Constraints and limits

- No `HUB.start()` during import; production start remains in `run()`.
- No change to stream selection, switching, slate, VOD, HTTP endpoint
  semantics, feeder, pacing, ring, TS continuity or watchdog policy.
- No Rust source, shell script, C reference, deployment script, service or
  device touched.
- The unstarted module HUB still loads local configuration/slate bytes and
  creates locks/queues; the audit boundary is process/thread/network
  side-effect freedom, not zero local reads.
- TASK-010 remains uncommitted, like TASK-004/B and TASK-005.

## 5. Audit request

Please inspect the current workspace through MCP and verify:

1. `server.py` import path cannot call `Popen`, start a thread or open a socket.
2. `run()` starts HUB before serving; direct `StreamHub()` still starts.
3. The stress test has a real oracle and no `or True`/`mock.ANY` escape.
4. Final hashes and test counts.
5. No TASK-001..005 / TASK-004-B regression.

Deploy and physical validation remain **NO-GO**.

## 6. Post-audit addendum (2026-10-20)

Independent read-only MCP audit: **APPROVED / GO** for the host-only TASK-010
scope. Deploy, hardware and live-network validation remain **NO-GO**.

- Auditor verified 6/6 TASK-010 hashes, including the full `server.py` hash
  `937bf965db296ad57a389d54c8235488f6ed7ddc172235265f02cb83b6f2ce3c`.
- Auditor verified 6/6 frozen predecessor files and found 0 blockers.
- Auditor confirmed the import isolation, explicit `run()` start, direct
  constructor compatibility, idempotent sequential start and the replacement
  of the vacuous assertion.
- Non-blocking observations: `start()` sets `_started=True` before worker
  creation, so a partial start failure is not retried and simultaneous calls
  are not mutually excluded; import still performs local configuration/slate
  reads and directory creation. Both are within the TASK-010 contract; any
  hardening is future scope.
- Test/build counts remain builder-declared because MCP did not expose
  independent execution records.
- Audit conversation (ChatGPT Chat mode, connector
  `Codex with ChatGPT · Code`):
  `https://chatgpt.com/c/6ad6631b-aae8-83e8-a10c-61b675d55e3b`

No file, production service, process or device was modified by the audit.
