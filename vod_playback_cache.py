"""Bounded, disposable disk cache for playback and nearby episodes.

The cloud remains the library. Only complete, verified reads are exposed from
the sparse local files; holes are never returned as video data. Construction is
inert so importing the server does not create files, threads, or connections.
"""

import collections
import hashlib
import fcntl
import os
import re
import shutil
import stat
import tempfile
import threading
import time

import vod_revision


class CurrentVideoCache:
    def __init__(self, directory, fetch, max_bytes=6 * 1024**3,
                 min_free_bytes=8 * 1024**3, block_size=4 * 1024**2):
        if max_bytes < 0 or min_free_bytes < 0 or block_size <= 0:
            raise ValueError("Invalid playback cache limits")
        self.directory = os.fspath(directory)
        self.fetch = fetch
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.block_size = block_size
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._wake = threading.Event()
        self._worker = None
        self._state = None
        self._closed = False
        self._directory_lock_fd = None
        self._secondary_cache = collections.OrderedDict()
        self._secondary_max = 16
        self._secondary_inflight = {}
        self._episode_states = collections.OrderedDict()
        self._episode_next_key = None
        self._episode_mode = False
        # Retenção temporal do cache de episódios: 72 h. É independente da
        # reserva de espaço (_max_bytes / min_free) e do número de entradas:
        # um episódio ainda pode ser evictado por CAPACIDADE antes das 72 h,
        # então 72 h é o teto de retenção, não uma promessa de retenção.
        self._episode_retention_seconds = 3 * 24 * 3600
        self._episode_max_entries = 4
        self._episode_last_activity = 0.0
        self._background_interval = 0.5

    @staticmethod
    def _identity(task):
        task_id = task.get("id", "")
        size = task.get("file_size")
        if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{8}", task_id):
            raise ValueError("Invalid VOD task identifier")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ValueError("Invalid VOD file size")
        if task.get("remote_path", task_id + ".mp4") != task_id + ".mp4":
            raise ValueError("Invalid VOD object path")
        md5 = task.get("remote_md5") or ""
        if md5 and (not isinstance(md5, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", md5)):
            raise ValueError("Invalid VOD content identity")
        # Trusted content revision is part of the identity: a mutated object with
        # the same task id and size must never be served from an old generation,
        # neither from the sparse disk file (keyed below) nor from the in-memory
        # secondary cache (keyed by (key, block)). When no revision can be
        # established the identity stays usable but is not content-trusting and
        # degrades to the historical (task_id, md5, size) semantics.
        revision = vod_revision.content_revision(task)
        if revision is not None and not vod_revision.is_valid_revision(revision):
            revision = None
        return task_id, md5.lower(), size, revision

    def _has_space(self, count):
        try:
            return shutil.disk_usage(self.directory).free >= self.min_free_bytes + count
        except OSError:
            return False

    def _discard_locked(self):
        for event in self._secondary_inflight.values():
            event.set()
        self._secondary_inflight.clear()
        self._secondary_cache.clear()
        states = list(self._episode_states.values())
        self._episode_states.clear()
        self._episode_next_key = None
        self._episode_mode = False
        state, self._state = self._state, None
        if state is not None and not any(s is state for s in states):
            states.append(state)
        for state in states:
            self._dispose_state_locked(state)

    def _dispose_state_locked(self, state):
        for event in state["inflight"].values():
            event.set()
        os.close(state["fd"])
        try:
            os.unlink(state["path"])
        except FileNotFoundError:
            pass

    def _new_state(self, task, key):
        if key[2] > self.max_bytes or not self.max_bytes:
            return None
        try:
            os.makedirs(self.directory, mode=0o700, exist_ok=True)
            if not self._own_directory():
                return None
            if not self._has_space(min(self.block_size, key[2])):
                return None
            fd, path = tempfile.mkstemp(prefix="current-vod-", suffix=".cache", dir=self.directory)
            try:
                os.ftruncate(fd, key[2])
            except OSError:
                os.close(fd)
                os.unlink(path)
                return None
            return {"key": key, "task": dict(task), "fd": fd, "path": path,
                    "blocks": set(), "inflight": {}, "ready": False,
                    "paused": False}
        except OSError:
            return None

    def _own_directory(self):
        """One cache owner per directory, including across a process restart."""
        if self._directory_lock_fd is not None:
            return True
        fd = os.open(os.path.join(self.directory, ".current-vod.lock"),
                     os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._directory_lock_fd = fd
        # A prior process may have died before unlinking its disposable movie.
        # The lock proves that these owned filenames have no live cache owner.
        with os.scandir(self.directory) as entries:
            for entry in entries:
                if re.fullmatch(r"current-vod-[a-zA-Z0-9_]+\.cache(?:\.seed)?", entry.name):
                    try:
                        os.unlink(entry.path)
                    except FileNotFoundError:
                        pass
        return True

    def _fetch(self, task, start, length):
        data = self.fetch(task, start, length)
        if not isinstance(data, bytes) or len(data) != length:
            raise OSError("Incomplete VOD range from origin")
        return data

    def _store_locked(self, state, block, data):
        if (self._state is not state and self._episode_states.get(state["key"]) is not state) or not self._has_space(len(data)):
            state["paused"] = True
            return
        try:
            offset = block * self.block_size
            view = memoryview(data)
            while view:
                written = os.pwrite(state["fd"], view, offset)
                if written <= 0:
                    raise OSError("Short local cache write")
                offset += written
                view = view[written:]
            state["blocks"].add(block)
        except OSError:
            # Existing complete blocks remain useful if disk space runs out.
            state["paused"] = True

    def _block(self, task, key, block):
        start = block * self.block_size
        count = min(self.block_size, key[2] - start)
        sec_key = (key, block)
        while True:
            with self._lock:
                state = self._episode_states.get(key) if self._episode_mode else self._state
                if state is None or state["key"] != key:
                    cached_sec = self._secondary_cache.get(sec_key)
                    if cached_sec is not None:
                        self._secondary_cache.move_to_end(sec_key)
                        return cached_sec
                    event = self._secondary_inflight.get(sec_key)
                    owner = event is None
                    if owner:
                        event = threading.Event()
                        self._secondary_inflight[sec_key] = event
                    state = None
                elif block in state["blocks"]:
                    try:
                        data = os.pread(state["fd"], count, start)
                        if len(data) == count:
                            return data
                    except OSError:
                        pass
                    state["blocks"].discard(block)
                    continue
                else:
                    event = state["inflight"].get(block)
                    owner = event is None
                    if owner:
                        event = threading.Event()
                        state["inflight"][block] = event
            if not owner:
                if not event.wait(timeout=60):
                    raise TimeoutError("VOD cache fetch timed out")
                continue
            try:
                data = self._fetch(task, start, count)
                with self._lock:
                    if state is not None:
                        self._store_locked(state, block, data)
                    else:
                        while len(self._secondary_cache) >= self._secondary_max:
                            self._secondary_cache.popitem(last=False)
                        self._secondary_cache[sec_key] = data
                return data
            finally:
                if state is not None:
                    with self._lock:
                        state["inflight"].pop(block, None)
                        event.set()
                else:
                    with self._lock:
                        self._secondary_inflight.pop(sec_key, None)
                        event.set()

    def read(self, task, start, length):
        """Read a byte range, clipping at EOF; another task never changes selection."""
        key = self._identity(task)
        if isinstance(start, bool) or isinstance(length, bool) or not isinstance(start, int) or not isinstance(length, int) or start < 0 or length < 0:
            raise ValueError("Invalid VOD byte range")
        end = min(key[2], start + length)
        if start >= end:
            return b""
        chunks = []
        while start < end:
            block = start // self.block_size
            relative = start % self.block_size
            take = min(self.block_size - relative, end - start)
            cached = None
            with self._lock:
                state = self._episode_states.get(key) if self._episode_mode else self._state
                if state is not None and state["key"] == key and block in state["blocks"]:
                    try:
                        piece = os.pread(state["fd"], take, start)
                        if len(piece) == take:
                            cached = piece
                    except OSError:
                        pass
                    if cached is None:
                        state["blocks"].discard(block)
            if cached is None:
                data = self._block(task, key, block)
                cached = data[relative:relative + take]
            chunks.append(cached)
            start += take
        return b"".join(chunks)

    def _seed(self, state, seed_path):
        """Copy an existing verified final file without moving or deleting it."""
        key = state["key"]
        try:
            with open(seed_path, "rb") as source:
                if os.fstat(source.fileno()).st_size != key[2]:
                    return
                if key[1]:
                    digest = hashlib.md5()
                    while True:
                        chunk = source.read(self.block_size)
                        if not chunk:
                            break
                        digest.update(chunk)
                    if digest.hexdigest() != key[1]:
                        return
                    source.seek(0)
                if self._link_seed(state, source):
                    return
                block = 0
                while True:
                    data = source.read(self.block_size)
                    if not data:
                        break
                    with self._lock:
                        if self._state is not state or state["paused"]:
                            return
                        expected = min(self.block_size, key[2] - block * self.block_size)
                        if len(data) != expected:
                            return
                        self._store_locked(state, block, data)
                    block += 1
        except OSError:
            # Missing/unreadable seed is optional; the cloud is still the source.
            return

    def _link_seed(self, state, source):
        """Reuse a verified immutable final inode on the same filesystem.

        The caller may unlink the staging filename afterward. The owned cache
        link keeps the bytes alive without a second disk allocation or copy.
        Unsupported links (for example across filesystems) use the copy path.
        """
        link_path = state["path"] + ".seed"
        link_fd = None
        try:
            original = os.fstat(source.fileno())
            named = os.stat(source.name, follow_symlinks=False)
            if not stat.S_ISREG(named.st_mode) or (named.st_dev, named.st_ino) != (original.st_dev, original.st_ino):
                return False
            with self._lock:
                if self._state is not state:
                    return False
                os.link(source.name, link_path, follow_symlinks=False)
                linked = os.stat(link_path, follow_symlinks=False)
                if (linked.st_dev, linked.st_ino, linked.st_size) != (original.st_dev, original.st_ino, state["key"][2]):
                    return False
                link_fd = os.open(link_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                os.replace(link_path, state["path"])
                old_fd, state["fd"] = state["fd"], link_fd
                link_fd = None
                os.close(old_fd)
                total = (state["key"][2] + self.block_size - 1) // self.block_size
                state["blocks"] = set(range(total))
                state["paused"] = False
                return True
        except OSError:
            return False
        finally:
            if link_fd is not None:
                os.close(link_fd)
            try:
                os.unlink(link_path)
            except FileNotFoundError:
                pass

    def start(self, task, seed_path=None):
        """Warm the selected video and fill it in the background. Return disk eligibility."""
        task = dict(task)
        key = self._identity(task)
        with self._start_lock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("Playback cache is closed")
                if self._episode_mode:
                    self._discard_locked()
                state = self._state
                if state is None or state["key"] != key:
                    self._discard_locked()
                    state = self._new_state(task, key)
                    self._state = state
                elif state is not None:
                    state["paused"] = False
                if state is not None:
                    state["ready"] = False
            if state is not None and seed_path:
                self._seed(state, seed_path)
            # Read one block at a time so startup does not allocate the movie in RAM.
            for offset in range(0, min(key[2], 8 * 1024**2), self.block_size):
                self.read(task, offset, min(self.block_size, key[2] - offset))
            tail = min(key[2], 1024**2)
            self.read(task, key[2] - tail, tail)
            if state is not None:
                with self._lock:
                    if self._state is state and not self._closed:
                        state["ready"] = True
                        if self._worker is None:
                            self._worker = threading.Thread(target=self._fill, name="vod-current-cache", daemon=True)
                            self._worker.start()
                        self._wake.set()
            return state is not None

    def select_episode(self, task, next_task=None):
        """Select an observed episode without blocking its read on cloud access.

        Keep a bounded number of recent episodes; the worker fills the current
        episode before the optional next one. Probe reads must not call this.
        """
        task = dict(task)
        key = self._identity(task)
        following = dict(next_task) if next_task is not None else None
        next_key = self._identity(following) if following is not None else None
        with self._lock:
            if self._closed:
                return False
            if not self._episode_mode:
                self._discard_locked()
                self._episode_mode = True
            self._episode_next_key = None
            now = time.monotonic()
            self._episode_last_activity = now
            for old_key, old in list(self._episode_states.items()):
                if old_key != key and now - old.get("last_used", now) > self._episode_retention_seconds:
                    self._episode_states.pop(old_key)
                    self._dispose_state_locked(old)

            def add(item, item_key, protected):
                existing = self._episode_states.get(item_key)
                if existing is not None:
                    existing["last_used"] = now
                    existing["paused"] = False
                    self._episode_states.move_to_end(item_key)
                    return existing
                if item_key[2] > self.max_bytes or not self.max_bytes:
                    return None
                while (len(self._episode_states) >= self._episode_max_entries or
                       sum(k[2] for k in self._episode_states) + item_key[2] > self.max_bytes):
                    victim = next((k for k in self._episode_states if k not in protected), None)
                    if victim is None:
                        return None
                    self._dispose_state_locked(self._episode_states.pop(victim))
                state = self._new_state(item, item_key)
                if state is not None:
                    state.update(ready=True, last_used=now)
                    self._episode_states[item_key] = state
                return state

            state = add(task, key, set())
            self._state = state
            if following is not None and next_key != key and state is not None:
                if add(following, next_key, {key}) is not None:
                    self._episode_next_key = next_key
            if state is not None:
                if self._worker is None:
                    self._worker = threading.Thread(target=self._fill, name="vod-current-cache", daemon=True)
                    self._worker.start()
                self._wake.set()
            return state is not None

    def _fill(self):
        while True:
            self._wake.wait(timeout=60)
            self._wake.clear()
            while True:
                with self._lock:
                    if self._closed:
                        return
                    if self._episode_mode:
                        now = time.monotonic()
                        if now - self._episode_last_activity > self._episode_retention_seconds:
                            self._discard_locked()
                            break
                        protected = {self._episode_next_key}
                        if self._state is not None:
                            protected.add(self._state["key"])
                        for old_key, old in list(self._episode_states.items()):
                            if old_key not in protected and now - old.get("last_used", now) > self._episode_retention_seconds:
                                self._episode_states.pop(old_key)
                                self._dispose_state_locked(old)
                    candidates = [self._state]
                    if self._episode_mode and self._episode_next_key is not None and self._state is not None and not self._state["paused"]:
                        candidates.append(self._episode_states.get(self._episode_next_key))
                    state = None
                    missing = None
                    for candidate in candidates:
                        if candidate is None or not candidate["ready"] or candidate["paused"]:
                            continue
                        count = (candidate["key"][2] + self.block_size - 1) // self.block_size
                        block = next((b for b in range(count) if b not in candidate["blocks"]), None)
                        if block is not None:
                            state, missing = candidate, block
                            break
                    if state is None:
                        break
                    task, key = state["task"], state["key"]
                try:
                    self._block(task, key, missing)
                    # Yield between background requests; a new selection wakes
                    # this delay immediately. Foreground reads never wait here.
                    if self._episode_mode and self._wake.wait(timeout=self._background_interval):
                        break
                except Exception:
                    with self._lock:
                        if self._state is state or self._episode_states.get(key) is state:
                            state["paused"] = True

    def stop(self):
        """Drop only the owned cache file; an ongoing origin read cannot republish it."""
        with self._lock:
            self._discard_locked()
            self._wake.set()

    def close(self):
        with self._lock:
            self._closed = True
            self._discard_locked()
            self._wake.set()
            worker = self._worker
            directory_lock, self._directory_lock_fd = self._directory_lock_fd, None
            if directory_lock is not None:
                os.close(directory_lock)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2)
