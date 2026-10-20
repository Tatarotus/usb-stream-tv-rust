"""RC-001 host-only integration tests for the live switching path.

These tests drive the real `server.StreamHub` state machine with fake FFmpeg
processes backed by OS pipes. They do not launch ffmpeg, bind a port, touch a
provider, access `/data`, or invoke any USB/gadget command.
"""

import os
import queue
import threading
import time
import types
from pathlib import Path
from unittest import mock

import server


class _PassThroughRestamper:
    """Keeps the hub tests focused on publication and state, not TS parsing."""

    def __init__(self):
        self.new_channel_calls = 0

    def process_chunk(self, chunk):
        return chunk

    def start_new_channel(self):
        self.new_channel_calls += 1

    def reset_epoch(self):
        pass


class _FakePipeProc:
    """A Popen-like object whose stdout reads from a real OS pipe."""

    def __init__(self, chunks=(), exit_rc=None):
        self._r, self._w = os.pipe()
        self.stdout = types.SimpleNamespace(fileno=lambda fd=self._r: fd)
        self._poll_rc = None
        self.kill_calls = 0
        for chunk in chunks:
            os.write(self._w, chunk)
        if exit_rc is not None:
            os.close(self._w)
            self._w = None
            self._poll_rc = exit_rc

    def write(self, data):
        os.write(self._w, data)

    def eof(self):
        if self._w is not None:
            os.close(self._w)
            self._w = None
        if self._poll_rc is None:
            self._poll_rc = 0

    def poll(self):
        return self._poll_rc

    def kill(self):
        self.kill_calls += 1
        if self._w is not None:
            os.close(self._w)
            self._w = None
        self._poll_rc = -9

    def close(self):
        for fd in (self._w, self._r):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._w = None
        self._r = None


def _make_hub():
    hub = server.StreamHub(start_workers=False)
    hub.running = False
    hub.in_standby = False
    hub.switching = False
    hub.proc = None
    hub.slate_mode = False
    hub.slate_start_time = 0.0
    hub.slate_chunks = []
    hub.stream_health = "ok"
    hub.last_chunk_time = time.time()
    hub.restamper = _PassThroughRestamper()
    hub.subscribers.clear()
    hub.prebuffer_chunks.clear()
    hub.prebuffer_bytes = 0
    hub.current_channel_id = "chan-a"
    hub.current_channel_name = "Channel A"
    hub.current_url = "fake://a"
    hub.current_is_live = False
    hub.current_is_eco = False
    hub.current_is_temporary = False
    hub.fallback_channel = None
    hub.last_live_channel = "chan-a"
    return hub


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _drain_until(q, expected, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            item = q.get_nowait()
        except queue.Empty:
            time.sleep(0.01)
            continue
        if item == expected:
            return True
    return False


def _start_thread(target, **kwargs):
    thread = threading.Thread(target=target, kwargs=kwargs, daemon=True)
    thread.start()
    return thread


def _stop_hub_threads(hub, *threads):
    hub.running = False
    for thread in threads:
        if thread is not None:
            thread.join(timeout=1.0)


def test_healthy_reader_publishes_live_bytes():
    hub = _make_hub()
    proc = _FakePipeProc()
    hub.proc = proc
    subscriber = queue.Queue()
    hub.subscribers.add(subscriber)
    hub.running = True
    thread = _start_thread(hub._reader_loop)
    try:
        proc.write(b"L" * 188)
        assert _wait_for(lambda: not subscriber.empty()), "healthy live bytes were not published"
        assert subscriber.get_nowait() == b"L" * 188
        assert hub.slate_mode is False
        assert hub.stream_health == "ok"
    finally:
        _stop_hub_threads(hub, thread)
        proc.close()


def test_source_failure_enters_slate_and_recovers():
    hub = _make_hub()
    dead = _FakePipeProc(exit_rc=1)
    hub.proc = dead
    hub.slate_chunks = [(b"S" * 188, 0.01)]
    hub.last_chunk_time = time.time() - 10.0
    hub._start_initial = mock.Mock()
    subscriber = queue.Queue()
    hub.subscribers.add(subscriber)

    reader = None
    keepalive = None
    healthy = _FakePipeProc()
    hub.running = True
    try:
        reader = _start_thread(hub._reader_loop)
        assert _wait_for(lambda: hub.slate_mode is True), "source failure did not enter slate"
        assert hub.stream_health == "slate"

        keepalive = _start_thread(hub._keepalive_loop)
        assert _wait_for(lambda: not subscriber.empty()), "slate bytes were not published"
        assert subscriber.get_nowait() == b"S" * 188

        # Simulate a recovered upstream process. The reader loop must exit
        # slate atomically and publish real bytes without an epoch reset race.
        hub.proc = healthy
        healthy.write(b"R" * 188)
        assert _wait_for(lambda: hub.slate_mode is False), "reader did not exit slate after recovery"
        assert hub.stream_health == "ok"
        assert _drain_until(subscriber, b"R" * 188), "recovered live bytes were not published"
        assert hub.restamper.new_channel_calls >= 1
    finally:
        _stop_hub_threads(hub, reader, keepalive)
        dead.close()
        healthy.close()


def test_successful_switch_commits_atomically_and_kills_old_process():
    hub = _make_hub()
    old = _FakePipeProc()
    hub.proc = old
    new = _FakePipeProc(chunks=[b"N" * 188])
    subscriber = queue.Queue()
    hub.subscribers.add(subscriber)

    def channel_for(cid):
        return {"id": cid, "name": "Channel B", "url": "fake://b", "is_eco": False}

    with mock.patch.object(server.CH_MGR, "get_channel", side_effect=channel_for), \
         mock.patch.object(server, "resolve_channel_url", side_effect=lambda url: url), \
         mock.patch.object(server, "probe_is_h264", return_value=True), \
         mock.patch.object(server, "build_ffmpeg_cmd", side_effect=lambda *a, **k: ["ffmpeg"]), \
         mock.patch.object(server.subprocess, "Popen", return_value=new), \
         mock.patch.object(server, "log_event"):
        assert hub.switch_channel("chan-b") is True
        assert _wait_for(lambda: hub.switching is False and hub.current_channel_id == "chan-b"), \
            "successful switch did not commit"
        assert hub.proc is new
        assert old.kill_calls == 1
        assert hub.slate_mode is False
        assert hub.stream_health == "ok"
        assert not subscriber.empty()
        assert subscriber.get_nowait() == b"N" * 188

    old.close()
    new.close()


def test_failed_switch_keeps_old_channel_and_releases_lock():
    hub = _make_hub()
    old = _FakePipeProc()
    hub.proc = old
    failed = _FakePipeProc(exit_rc=1)

    def channel_for(cid):
        return {"id": cid, "name": "Channel B", "url": "fake://b", "is_eco": False}

    with mock.patch.object(server.CH_MGR, "get_channel", side_effect=channel_for), \
         mock.patch.object(server, "resolve_channel_url", side_effect=lambda url: url), \
         mock.patch.object(server, "probe_is_h264", return_value=True), \
         mock.patch.object(server, "build_ffmpeg_cmd", side_effect=lambda *a, **k: ["ffmpeg"]), \
         mock.patch.object(server.subprocess, "Popen", return_value=failed), \
         mock.patch.object(server, "log_event"):
        assert hub.switch_channel("chan-b") is True
        assert _wait_for(lambda: hub.switching is False, timeout=3.0), "failed switch left switching set"
        assert hub.proc is old
        assert hub.current_channel_id == "chan-a"
        assert failed.kill_calls == 1
        assert not hub.switch_lock.locked()

    old.close()
    failed.close()


def test_source_failure_does_not_dispatch_gadget_command():
    hub = _make_hub()
    dead = _FakePipeProc(exit_rc=1)
    hub.proc = dead
    hub.slate_chunks = [(b"S" * 188, 0.01)]
    hub._start_initial = mock.Mock()
    hub.running = True
    thread = None
    try:
        with mock.patch.object(server, "dispatch_device_cmd") as dispatch:
            thread = _start_thread(hub._reader_loop)
            assert _wait_for(lambda: hub.slate_mode is True), "failure path did not run"
            time.sleep(0.3)
            assert dispatch.call_count == 0
    finally:
        _stop_hub_threads(hub, thread)
        dead.close()


def test_prebuffer_is_bounded_and_drops_oldest():
    hub = _make_hub()
    hub.PREBUFFER_MAX_BYTES = 3 * 188
    for i in range(6):
        hub._broadcast(bytes([i]) * 188)
    assert hub.prebuffer_bytes <= hub.PREBUFFER_MAX_BYTES
    assert len(hub.prebuffer_chunks) == 3
    assert list(hub.prebuffer_chunks) == [bytes([3]) * 188, bytes([4]) * 188, bytes([5]) * 188]


def test_parser_is_not_referenced_by_server_or_rc_live_paths():
    source = Path(server.__file__).read_text()
    assert "TsCandidateProbe" not in source
    assert "ts_candidate_probe" not in source
    assert "wait_for_ready_candidate" not in source