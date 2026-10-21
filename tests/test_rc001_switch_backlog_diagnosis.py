"""RC-001 ITERATION 8 - host-only diagnosis: channel-switch backlog.

Question (auditor): after a successful commit (SWITCH_OK), do old-channel bytes
remain queued for a continuously connected subscriber (the TV)?

Method: drive the REAL StreamHub.switch_channel() commit path with a fake ffmpeg
pipe. No ffmpeg process, no port, no provider, no production change.

Measured BEFORE the fix (iteration 8 diagnosis): queue depth 2500 chunks at
commit, 2499 old chunks delivered AFTER SWITCH_OK, 155.9 MiB = 284 s of stale
content at 4599 kbps (bound 2500 x 65424 B = 156 MiB).

The correction under test drains the subscriber queues inside
`_mark_live_commit_locked` (same critical section that publishes the first
new-channel chunk), so a lagging subscriber starts at the live edge.

Measurements produced:
  * subscriber queue depth at commit
  * age (seconds) of the oldest queued chunk at commit
  * number of old-channel chunks the subscriber must consume BEFORE the first
    new-channel chunk, and the corresponding stale window in seconds
"""

import os
import queue
import struct
import time
from unittest import mock

import pytest

import server

CHUNK = 348 * 188          # 65424 bytes - producer chunk size (server.py:1978)
QUEUE_MAX = 2500           # queue.Queue(maxsize=2500) - StreamHub.subscribe()
NOMINAL_KBPS = 4599.0      # measured live bitrate at the time of the observation
MARKER_LEN = CHUNK - 8


def _hub(tmp_event_log):
    hub = server.StreamHub(start_workers=False)
    hub.running = False
    hub.in_standby = False
    hub.switching = False
    hub.slate_mode = False
    hub.slate_start_time = 0
    hub.stream_health = "ok"
    hub.last_chunk_time = time.time()
    hub.slate_chunks = []
    hub.restamper = mock.Mock()
    hub.restamper.process_chunk.side_effect = lambda chunk: chunk
    hub.subscribers.clear()
    hub.prebuffer_chunks.clear()
    hub.prebuffer_bytes = 0
    hub.current_channel_id = "OLD"
    hub.current_channel_name = "OLD"
    hub.proc = None
    return hub


def _old_chunk(t):
    """Old-channel chunk carrying its production time in the last 8 bytes."""
    return b"A" * MARKER_LEN + struct.pack("<d", t)


def _new_chunk():
    return b"B" * CHUNK


def _fake_proc_with_first_chunk(first_chunk):
    rfd, wfd = os.pipe()
    os.write(wfd, first_chunk)
    os.close(wfd)

    class _FakeStdout:
        def fileno(self):
            return rfd

    class _FakeProc:
        stdout = _FakeStdout()

        def poll(self):
            return None

        def kill(self):
            pass

    return _FakeProc()


def _drive_real_switch(hub, tmp_event_log):
    """Run the real switch_channel() commit path."""
    with mock.patch.object(server, "EVENT_LOG", str(tmp_event_log)), mock.patch.object(
        server, "resolve_channel_url", side_effect=lambda u: u
    ), mock.patch.object(
        server, "build_ffmpeg_cmd", return_value=["ffmpeg"]
    ), mock.patch.object(
        server.subprocess,
        "Popen",
        side_effect=lambda *a, **k: _fake_proc_with_first_chunk(_new_chunk()),
    ):
        started = hub.switch_channel(
            custom_url="http://fixture.local/NEW.ts",
            custom_name="NEW",
            force=True,
            is_eco=False,
        )
        assert started is True
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if not hub.switching and not hub.switch_lock.locked():
                break
            time.sleep(0.01)
        else:
            raise AssertionError("switch did not complete")

    events = ""
    try:
        with open(tmp_event_log) as fh:
            events = fh.read()
    except OSError:
        pass
    assert "SWITCH_OK" in events, "commit must have completed: %r" % events
    return events


def _drain_and_count(sub):
    """Count old chunks ahead of the first new chunk; return (stale, total, oldest_age)."""
    stale = 0
    total = 0
    seen_new = False
    oldest_age = None
    while not sub.empty():
        chunk = sub.get_nowait()
        total += 1
        if chunk[:1] == b"B":
            seen_new = True
            continue
        if not seen_new:
            stale += 1
            if oldest_age is None:
                t_produced = struct.unpack("<d", chunk[-8:])[0]
                oldest_age = time.time() - t_produced
    return stale, total, oldest_age, seen_new


def test_switch_commit_evicts_old_channel_backlog(tmp_path):
    hub = _hub(tmp_path / "events.log")
    sub = queue.Queue(maxsize=QUEUE_MAX)
    hub.subscribers.add(sub)

    # A connected TV that has fallen behind: its queue is full and the server is
    # in the drop-oldest regime (server.py:2065-2075).
    for _ in range(QUEUE_MAX + 300):
        hub._broadcast(_old_chunk(time.time()))
        time.sleep(0.0002)
    depth_at_commit = sub.qsize()

    _drive_real_switch(hub, tmp_path / "events.log")

    stale, total, oldest_age, seen_new = _drain_and_count(sub)
    stale_bytes = stale * CHUNK
    stale_secs = stale_bytes / (NOMINAL_KBPS * 1000 / 8.0)

    print(
        "\n[DIAG] queue depth at commit: %d chunks\n"
        "[DIAG] oldest queued chunk age at commit: %.1f s\n"
        "[DIAG] old chunks delivered AFTER SWITCH_OK, before the new channel: %d\n"
        "[DIAG] stale bytes ahead of the new channel: %.1f MiB\n"
        "[DIAG] stale window at %.0f kbps: %.0f s\n"
        "[DIAG] new channel reached the subscriber: %s"
        % (
            depth_at_commit,
            oldest_age or 0.0,
            stale,
            stale_bytes / 1048576.0,
            NOMINAL_KBPS,
            stale_secs,
            seen_new,
        )
    )

    # FIXED INVARIANT (RC-001 iter8): the commit evicts already-queued
    # old-channel bytes, so a subscriber that fell behind starts at the live
    # edge. Pre-fix this was 2499 stale chunks (155.9 MiB ~ 284 s).
    assert depth_at_commit == QUEUE_MAX
    assert seen_new is True, "the new channel chunk must reach the subscriber"
    assert stale == 0, (
        "old-channel bytes survived the commit: %d chunks (%.1f MiB) ahead of "
        "the new channel" % (stale, stale_bytes / 1048576.0)
    )
    # The new channel must also be the very first thing the subscriber sees.
    assert total >= 1 and stale == 0


def test_candidate_commit_flush_removes_the_backlog(tmp_path):
    """Validates the candidate correction (drain subscriber queues at commit).

    This is a host-only validation; nothing is deployed.
    """
    hub = _hub(tmp_path / "events.log")
    sub = queue.Queue(maxsize=QUEUE_MAX)
    hub.subscribers.add(sub)

    for _ in range(QUEUE_MAX + 300):
        hub._broadcast(_old_chunk(time.time()))

    real_commit = hub._mark_live_commit_locked

    def flushing_commit():
        real_commit()
        for q in list(hub.subscribers):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    with mock.patch.object(hub, "_mark_live_commit_locked", side_effect=flushing_commit):
        _drive_real_switch(hub, tmp_path / "events.log")

    stale, total, _age, seen_new = _drain_and_count(sub)
    print(
        "\n[DIAG-FIX] with commit-time queue flush: stale=%d total=%d new_channel_ok=%s"
        % (stale, total, seen_new)
    )
    assert seen_new is True
    assert stale == 0, "the flush must remove old-channel bytes queued at commit"


def test_subscriber_queue_bound_is_the_stale_window_bound():
    """Arithmetic bound used in the diagnosis: worst-case stale window."""
    worst_bytes = QUEUE_MAX * CHUNK
    worst_secs = worst_bytes / (NOMINAL_KBPS * 1000 / 8.0)
    print(
        "\n[DIAG] worst-case backlog: %d chunks x %d B = %.1f MiB = %.0f s at %.0f kbps"
        % (QUEUE_MAX, CHUNK, worst_bytes / 1048576.0, worst_secs, NOMINAL_KBPS)
    )
    assert worst_bytes == 163_560_000
    assert 200 < worst_secs < 320


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))