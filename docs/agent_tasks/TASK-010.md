# TASK-010

## TASK ID

TASK-010

## TITLE

Safe Python test startup and baseline test-strength defects

## PRIORITY

P1. Status: **AUTHORIZED — host-only**, 2026-10-20. No deployment, hardware,
device, network probe, production server or ffmpeg launch is authorized.

## DEPENDENCIES

None. TASK-006 and TASK-009 need this safe import boundary before any
`server.py` instrumentation or StreamHub test work.

## OBJECTIVE

Make importing `server` side-effect free with respect to processes, threads and
network activity, while preserving `run()` as the production start path.
Replace the known vacuous stress assertion with a deterministic behavioral
oracle.

## EVIDENCE

- `server.py:2549` constructs `HUB = StreamHub()` at module import.
- `server.py:1891-1902` loads the slate, starts the initial ffmpeg producer and
  starts `reader_thread`/`keepalive_thread` inside `StreamHub.__init__`.
- `tests/test_server_control_plane.py:5` imports `HUB`, so discovery starts the
  producer and threads before any test runs.
- `tests/test_probe_sar.py:26-35` registers an import-time-at-exit cleanup that
  touches `server.HUB`.
- `tests/test_torrent_vod_stress.py:205` contains
  `assert ... in (None, ..., mock.ANY) or True`, which cannot fail.
- `docs/architecture/FORENSIC_AUDIT.md:70` records full Python discovery as
  deliberately NOT_RUN for these reasons.

## REQUIRED BEHAVIOR

1. `import server` must not call `subprocess.Popen`, start a thread, or open a
   network connection.
2. Module-level `HUB` is an initialized, unstarted `StreamHub`. It may load
   local configuration/slate bytes and create locks/queues, but no background
   work.
3. `run()` explicitly starts HUB before serving requests, preserving the
   production startup order and stream behavior.
4. `StreamHub()` with no argument keeps its existing started behavior for
   external callers; `StreamHub(start_workers=False)` is the explicit safe
   constructor used by module-level `HUB` and tests.
5. `StreamHub.start()` is idempotent and starts the initial producer exactly
   once plus the two worker threads.
6. Existing HTTP control-plane, probe SAR and torrent VOD tests remain
   behaviorally valid.
7. The unreadable-file stress test asserts that an unreadable video is skipped
   and the readable candidate is selected, using deterministic file order and
   a raised `OSError`; no `mock.ANY` or `or True` escape hatch remains.

## DESIGN

- Add `start_workers=True` keyword to `StreamHub.__init__`.
- Move `_start_initial()` and both `Thread(...).start()` calls into
  `StreamHub.start()`; guard with `_started`.
- Initialize `self.reader_thread = None`, `self.keepalive_thread = None` and
  `self._started = False` before optional start.
- At module level use `HUB = StreamHub(start_workers=False)`.
- Add `HUB.start()` at the beginning of `run()`.
- Add a subprocess import-isolation test that patches `subprocess.Popen`,
  `threading.Thread.start` and `socket.create_connection` to record/raise
  before importing `server`; assert no calls and `HUB._started is False`.
- Add a unit test for explicit/idempotent `start()` with mocked
  `_start_initial` and mocked thread objects.
- Replace the vacuous stress assertion with deterministic `os.walk` plus
  `getsize` mocks: first candidate raises `OSError`, second is readable, and
  the readable path is returned.

## ALLOWED FILES

Production:

- `server.py` only: `StreamHub.__init__`, new `StreamHub.start()`, module-level
  `HUB` construction and `run()` startup call.

Tests:

- NEW `tests/test_server_import_isolation.py`
- `tests/test_torrent_vod_stress.py` (the single unreadable-file test)
- `tests/test_probe_sar.py` only if the unstarted HUB requires a cleanup
  adjustment; no probe policy changes.
- `tests/test_server_control_plane.py` only if import/use of HUB must be made
  explicit; no endpoint behavior changes.

Documentation:

- `docs/agent_tasks/TASK-010.md`
- `docs/architecture/TASK_010_HANDOFF.md`

## READ-ONLY REFERENCES

Channel manager, slate generation/loading, StreamHub stream/switch/VOD logic,
RequestHandler endpoints, ffmpeg command construction, `torrent_downloader.py`,
all Rust files, shell harnesses, C reference, deployment scripts.

## FORBIDDEN CHANGES

No deployment, production server launch, production/live ffmpeg or yt-dlp
execution, external network probe, hardware access, service restart,
credential/.env read for a live provider, channel policy, slate policy, VOD
policy, HTTP semantics, StreamHub stream behavior, Rust files or C reference.
Test-local synthetic fixtures, loopback-only HTTP test servers and mocked
subprocesses are permitted. Do not remove tests, add ignores, weaken
assertions, or reformat unrelated code.

## TESTS

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  tests/test_server_import_isolation.py
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  tests/test_server_control_plane.py tests/test_probe_sar.py
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  tests/test_torrent_vod_stress.py -k unreadable_file_skipped
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest --collect-only -q -p no:cacheprovider tests
```

The full discovery/run may be attempted only after the import-isolation test
passes. If unrelated tests require network, ffmpeg or external assets, record
`NOT_RUN`/`BLOCKED` with the exact reason; do not silently skip them.

## ACCEPTANCE CRITERIA

- Import is process/thread/network free under the isolation test.
- Production `run()` still starts HUB exactly once before serving.
- Direct `StreamHub()` behavior is unchanged.
- HTTP control-plane, probe SAR and stress tests pass; the vacuous assertion is
  replaced by a deterministic oracle.
- Full pytest discovery no longer starts the producer/threads at import.
- Handoff records exact hashes, commands, outputs, and any environmental
  NOT_RUN/BLOCKED scope.
- No deployment, ffmpeg launch, hardware or C-reference action.

## STOP POINT

End with a host-only handoff and independent audit. Do not start TASK-006 or
deploy automatically.
