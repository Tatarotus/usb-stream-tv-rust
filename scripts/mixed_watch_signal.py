"""Conservative evidence of sustained mixed-VOD reads, never proof of viewing."""
from collections import deque
import queue
import threading
import time

from vod_history import resolve_episode_identity
from vod_revision import content_revision


class MixedWatchSignal:
    """Detect paced interior reads while bounding memory and ignoring probes.

    A client, indexer, or cache could mimic this pattern. Callers must label it
    inferred, not watched/completed, and cannot infer a playback timestamp.
    """

    def __init__(self, clock=time.monotonic, block_size=1024 * 1024):
        self.clock = clock
        self.block_size = block_size
        self.lock = threading.Lock()
        self.events = {}
        self.last_report = {}

    def observe(self, task, start, length):
        block = self.block_size
        size = task.get('file_size') or 0
        if (not isinstance(size, int) or size < 32 * block or length != block or
            start < 8 * block or start % block or start + length > size - 8 * block or
            not resolve_episode_identity(task)):
            return None
        revision = content_revision(task)
        key = (task.get('id'), revision)
        if not key[0] or not revision:
            return None
        now = self.clock()
        index = start // block
        with self.lock:
            self.events = {k: v for k, v in self.events.items() if v and now - v[-1][0] <= 180}
            self.last_report = {k: t for k, t in self.last_report.items() if now - t <= 1800}
            if key not in self.events and len(self.events) >= 256:
                oldest = min(self.events, key=lambda k: self.events[k][-1][0])
                del self.events[oldest]
            events = self.events.setdefault(key, deque(maxlen=128))
            while events and now - events[0][0] > 180:
                events.popleft()
            events.append((now, index))
            # Join nearby blocks into a cluster. This tolerates cache hits and
            # reordered requests while keeping distant probes/seeks separate.
            blocks = sorted({i for _, i in events})
            pos = blocks.index(index)
            left = right = pos
            while left and blocks[left] - blocks[left - 1] <= 2:
                left -= 1
            while right + 1 < len(blocks) and blocks[right + 1] - blocks[right] <= 2:
                right += 1
            distinct = set(blocks[left:right + 1])
            cluster = [(t, i) for t, i in events if i in distinct]
            buckets = {int(t // 15) for t, _ in cluster}
            if (len(distinct) < 6 or len(buckets) < 3 or
                now - min(t for t, _ in cluster) < 45 or
                now - self.last_report.get(key, float('-inf')) < 120):
                return None
            return {'task_id': key[0], 'revision': revision, 'observed_at': now}

    def acknowledge(self, event):
        with self.lock:
            self.last_report[(event['task_id'], event['revision'])] = event['observed_at']


class MixedWatchRecorder:
    """Record qualifying observations off the HTTP streaming thread."""

    def __init__(self, lookup, history, signal=None):
        self.lookup = lookup
        self.history = history
        self.signal = signal or MixedWatchSignal()
        self.pending = queue.Queue(maxsize=32)
        self.lock = threading.Lock()
        self.worker = None
        self.latest = {}
        self.inflight = set()

    def observe(self, task, start, length):
        event = self.signal.observe(task, start, length)
        if not event:
            return
        with self.lock:
            key = (event['task_id'], event['revision'])
            if key in self.inflight:
                return
            try:
                self.pending.put_nowait(event)
            except queue.Full:
                return
            self.inflight.add(key)
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._run, name='mixed-watch-history', daemon=True)
                self.worker.start()

    def _run(self):
        while True:
            event = self.pending.get()
            try:
                age = self.signal.clock() - event['observed_at']
                if age < 0 or age > 30:
                    continue
                task = self.lookup(event['task_id'])
                if not task or task.get('status') != 'ready' or content_revision(task) != event['revision']:
                    continue
                identity = resolve_episode_identity(task)
                if not identity:
                    continue
                series = identity['series_key']
                if event['observed_at'] < self.latest.get(series, float('-inf')):
                    continue
                if self.history.record_inferred(task) is not None:
                    self.latest[series] = event['observed_at']
                    if len(self.latest) > 512:
                        del self.latest[min(self.latest, key=self.latest.get)]
                    self.signal.acknowledge(event)
            except Exception as exc:
                print(f'[VOD] Watch inference ignored: {type(exc).__name__}')
            finally:
                with self.lock:
                    self.inflight.discard((event['task_id'], event['revision']))
                self.pending.task_done()
