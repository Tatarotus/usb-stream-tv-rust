"""TASK-006 Phase A: slate fallback during candidate acquisition.

These tests use an unstarted StreamHub and fake producer/restamper objects.
They never launch ffmpeg, bind a port or touch a live provider.
"""

import queue
import threading
import time
from unittest import mock

import server


def _hub():
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
    return hub


class _SlowQueue(queue.Queue):
    """First enqueue is slow, later ones are fast; exposes prebuffer/queue reordering."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def put_nowait(self, item):
        self.calls += 1
        time.sleep(0.05 if self.calls == 1 else 0.005)
        super().put_nowait(item)


class _FakeProc:
    def __init__(self, poll_rc=None):
        self.stdout = mock.Mock()
        self.stdout.fileno.return_value = 0
        self._poll_rc = poll_rc
        self.kill_calls = 0

    def poll(self):
        return self._poll_rc

    def kill(self):
        self.kill_calls += 1
        self._poll_rc = -9


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _run_keepalive_until(hub, predicate, timeout=1.5):
    hub.running = True
    thread = threading.Thread(target=hub._keepalive_loop, daemon=True)
    thread.start()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False
    finally:
        hub.running = False
        thread.join(timeout=1.0)


def test_keepalive_publishes_slate_while_switching():
    hub = _hub()
    hub.switching = True
    hub.last_chunk_time = time.time() - 10.0
    hub.slate_chunks = [(b"S" * 188, 0.01)]
    subscriber = queue.Queue()
    hub.subscribers.add(subscriber)

    def has_data():
        return not subscriber.empty()

    assert _run_keepalive_until(hub, has_data), "slate was suppressed by switching"
    assert subscriber.get_nowait() == b"S" * 188
    assert hub.slate_mode is True
    assert hub.stream_health == "slate"


def test_keepalive_publishes_null_while_switching_without_slate():
    hub = _hub()
    hub.switching = True
    hub.last_chunk_time = time.time() - 2.0
    hub.slate_chunks = []
    subscriber = queue.Queue()
    hub.subscribers.add(subscriber)

    def has_data():
        return not subscriber.empty()

    assert _run_keepalive_until(hub, has_data), "NULL keepalive was suppressed"
    data = subscriber.get_nowait()
    assert data.startswith(b"\x47\x1f\xff\x10")
    assert hub.stream_health == "degraded"


def test_keepalive_does_not_publish_for_healthy_source_while_switching():
    hub = _hub()
    hub.switching = True
    hub.last_chunk_time = time.time()
    hub.slate_chunks = [(b"S" * 188, 0.01)]
    subscriber = queue.Queue()
    hub.subscribers.add(subscriber)

    def has_data():
        return not subscriber.empty()

    assert not _run_keepalive_until(hub, has_data, timeout=0.25), "healthy source got fallback output"
    assert subscriber.empty()


def test_pre_commit_tick_cannot_publish_stale_slate():
    hub = _hub()
    hub.switching = False
    hub.last_chunk_time = time.time() - 10.0
    hub.slate_chunks = [(b"S" * 188, 0.01)]
    subscriber = queue.Queue()
    hub.subscribers.add(subscriber)
    hub.running = True

    thread = threading.Thread(target=hub._keepalive_loop, daemon=True)
    with hub.lock:
        thread.start()
        # Give the keepalive tick time to compute a stale gap and wait for
        # self.lock. Then simulate the live commit updating state under it.
        time.sleep(0.15)
        hub.last_chunk_time = time.time()
        hub._mark_live_commit_locked()
    try:
        time.sleep(0.3)
        assert subscriber.empty(), "stale slate reached the subscriber after commit"
        assert hub.slate_mode is False
        assert hub.stream_health == "ok"
    finally:
        hub.running = False
        thread.join(timeout=1.0)


def test_mark_live_commit_clears_slate_state():
    hub = _hub()
    hub.slate_mode = True
    hub.slate_start_time = 123.0
    hub.stream_health = "slate"

    with hub.lock:
        hub._mark_live_commit_locked()

    assert hub.slate_mode is False
    assert hub.slate_start_time == 0
    assert hub.stream_health == "ok"


def test_broadcast_preserves_prebuffer_and_subscriber_order():
    hub = _hub()
    subscriber = queue.Queue()
    hub.subscribers.add(subscriber)
    barrier = threading.Barrier(3)

    def publish(payload):
        barrier.wait()
        for _ in range(20):
            hub._broadcast(payload)

    first = threading.Thread(target=publish, args=(b"A" * 188,))
    second = threading.Thread(target=publish, args=(b"B" * 188,))
    first.start()
    second.start()
    barrier.wait()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert not first.is_alive() and not second.is_alive()
    queued = list(subscriber.queue)
    prebuffered = list(hub.prebuffer_chunks)
    assert queued == prebuffered
    assert len(queued) == 40


def test_broadcast_atomic_ordering_with_slow_subscriber():
    hub = _hub()
    subscriber = _SlowQueue()
    hub.subscribers.add(subscriber)
    started = threading.Event()
    payload_a = b"A" * 188
    payload_b = b"B" * 188

    def publish_a():
        started.set()
        hub._broadcast(payload_a)

    def publish_b():
        started.wait()
        time.sleep(0.01)
        hub._broadcast(payload_b)

    first = threading.Thread(target=publish_a)
    second = threading.Thread(target=publish_b)
    first.start()
    second.start()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert not first.is_alive() and not second.is_alive()
    assert list(hub.prebuffer_chunks) == [payload_a, payload_b]
    assert list(subscriber.queue) == [payload_a, payload_b]


def test_broadcast_drops_oldest_without_blocking_full_queue():
    hub = _hub()
    subscriber = queue.Queue(maxsize=1)
    subscriber.put(b"old")
    hub.subscribers.add(subscriber)

    hub._broadcast(b"new")

    assert subscriber.get_nowait() == b"new"
    assert hub.prebuffer_chunks[-1] == b"new"


def test_watchdog_does_not_kill_old_proc_while_switching():
    hub = _hub()
    old_proc = _FakeProc()
    hub.proc = old_proc
    hub.switching = True
    hub.slate_mode = True
    hub.slate_start_time = time.time() - 20.0
    hub.last_chunk_time = time.time() - 20.0
    hub.slate_chunks = []
    subscriber = queue.Queue()
    hub.subscribers.add(subscriber)

    assert _run_keepalive_until(
        hub, lambda: old_proc.kill_calls > 0 or not subscriber.empty(), timeout=0.3
    ), "keepalive stopped producing while switching"
    assert old_proc.kill_calls == 0
    assert not subscriber.empty()

    hub.switching = False
    hub.slate_start_time = time.time() - 20.0
    assert _run_keepalive_until(hub, lambda: old_proc.kill_calls > 0, timeout=0.5)
    assert old_proc.kill_calls >= 1


def test_watchdog_kill_is_atomic_with_switch_start():
    hub = _hub()
    checked = threading.Event()
    switch_applied = threading.Event()
    kill_observations = []

    class RaceProc:
        def poll(self):
            checked.set()
            return None

        def kill(self):
            # Old code released self.lock before kill(), so this helper could
            # set switching=True in the gap. Fixed code kills while holding it.
            switch_applied.wait(timeout=0.05)
            kill_observations.append(hub.switching)

    hub.proc = RaceProc()
    hub.slate_mode = True
    hub.slate_start_time = time.time() - 20.0
    hub.last_chunk_time = time.time() - 20.0
    hub.slate_chunks = []
    subscriber = queue.Queue()
    hub.subscribers.add(subscriber)

    def begin_switch():
        # Wait until the watchdog has observed switching=False and entered the
        # check/kill window, then try to start a switch there.
        assert checked.wait(timeout=1.0)
        with hub.lock:
            hub.switching = True
            switch_applied.set()

    switch_thread = threading.Thread(target=begin_switch)
    switch_thread.start()
    assert _run_keepalive_until(hub, lambda: bool(kill_observations), timeout=1.0)
    switch_thread.join(timeout=1.0)

    assert kill_observations == [False], "watchdog killed while a switch was starting"


def test_switch_channel_commit_refreshes_timestamp_and_last_live():
    hub = _hub()
    hub.current_channel_id = "old"
    hub.current_channel_name = "Old"
    hub.current_url = "old"
    hub.last_live_channel = "old"
    old_proc = _FakeProc()
    new_proc = _FakeProc()
    hub.proc = old_proc
    hub.last_chunk_time = time.time() - 30.0
    hub.restamper.process_chunk.side_effect = None
    hub.restamper.process_chunk.return_value = b""

    with mock.patch("server.subprocess.Popen", return_value=new_proc), mock.patch(
        "server.os.read", return_value=b"partial"
    ):
        assert hub.switch_channel("cnn-brasil", force=True, is_eco=False)
        assert _wait_for(
            lambda: not hub.switching and hub.current_channel_id == "cnn-brasil"
        )

    assert hub.last_live_channel == "cnn-brasil"
    assert hub.current_channel_id == "cnn-brasil"
    assert hub.last_chunk_time > time.time() - 5.0
    assert hub.slate_mode is False
    assert hub.stream_health == "ok"
    assert old_proc.kill_calls >= 1


def test_switch_channel_failure_preserves_last_live_and_proc():
    hub = _hub()
    hub.current_channel_id = "old"
    hub.current_channel_name = "Old"
    hub.last_live_channel = "old"
    old_proc = _FakeProc()
    hub.proc = old_proc

    with mock.patch("server.subprocess.Popen", side_effect=RuntimeError("no candidate")):
        assert hub.switch_channel("cnn-brasil", force=True, is_eco=False)
        assert _wait_for(lambda: not hub.switching)

    assert hub.current_channel_id == "old"
    assert hub.last_live_channel == "old"
    assert hub.proc is old_proc
    assert old_proc.kill_calls == 0


def test_switch_channel_timeout_preserves_last_live_and_proc():
    hub = _hub()
    hub.current_channel_id = "old"
    hub.current_channel_name = "Old"
    hub.last_live_channel = "old"
    old_proc = _FakeProc()
    new_proc = _FakeProc()
    hub.proc = old_proc
    clock = [time.time()]

    def fast_clock():
        clock[0] += 30.0
        return clock[0]

    with mock.patch("server.subprocess.Popen", return_value=new_proc), mock.patch(
        "server.os.read", return_value=b""
    ), mock.patch("server.time.time", side_effect=fast_clock):
        assert hub.switch_channel("cnn-brasil", force=True, is_eco=False)
        for _ in range(200):
            if not hub.switching:
                break
            time.sleep(0.01)

    assert not hub.switching
    assert hub.current_channel_id == "old"
    assert hub.last_live_channel == "old"
    assert hub.proc is old_proc
    assert old_proc.kill_calls == 0
    assert new_proc.kill_calls >= 1


def test_switch_youtube_failure_preserves_retry_count():
    hub = _hub()
    hub.current_channel_id = "old"
    hub.last_live_channel = "old"
    hub.temporary_retries = 2
    meta = {
        "original_url": "https://youtube.example/watch?v=x",
        "title": "fixture",
        "video_url": "http://127.0.0.1/fixture",
        "is_live": False,
    }

    with mock.patch("server.subprocess.Popen", side_effect=RuntimeError("no candidate")):
        assert hub.switch_youtube(meta)
        assert _wait_for(lambda: not hub.switching)

    assert hub.temporary_retries == 2
    assert hub.current_channel_id == "old"
    assert hub.last_live_channel == "old"
