"""Conservative cache hints from sequential mixed-VOD reads, not watch history."""
import threading
import time

from vod_history import resolve_episode_identity
from vod_revision import content_revision


class MixedCacheSignal:
    def __init__(self, block_size=1024 * 1024, clock=time.monotonic):
        self.block_size = block_size
        self.clock = clock
        self.lock = threading.Lock()
        self.observations = {}
        self.selected = None
        self.selected_at = 0

    def observe(self, task, start, length):
        size = task.get('file_size') or 0
        block = self.block_size
        if (length != block or start % block or start < 8 * block
                or start + length > size - 8 * block):
            return False
        now = self.clock()
        key = (task.get('id'), content_revision(task))
        if not key[0] or not key[1]:
            return False
        with self.lock:
            self.observations = {k: v for k, v in self.observations.items() if now - v[2] < 30}
            previous = self.observations.get(key)
            count = previous[1] + 1 if previous and start == previous[0] + block else 1
            self.observations[key] = (start, count, now)
            if count >= 3 and (self.selected != key or now - self.selected_at >= 60):
                self.selected = key
                self.selected_at = now
                return True
        return False


def next_episode(task, tasks):
    current = resolve_episode_identity(task)
    if not current:
        return None
    matches = []
    for candidate in tasks:
        if candidate.get('status') != 'ready' or candidate.get('storage') != 'gdrive':
            continue
        identity = resolve_episode_identity(candidate)
        if (identity and identity['series_key'] == current['series_key']
                and identity['season_number'] == current['season_number']
                and identity['episode_number'] == current['episode_number'] + 1
                and content_revision(candidate)):
            matches.append(candidate)
    return dict(matches[0]) if len(matches) == 1 else None
