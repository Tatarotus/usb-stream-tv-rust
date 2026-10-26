"""Verified Google Drive VOD objects and bounded, read-through RAM caching.

Construction/import is inert. rclone HTTP starts only on the first media read;
quota refresh runs in one background thread when quota() is requested.
"""

import collections
import concurrent.futures
import hashlib
import http.client
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
import urllib.parse


class StorageError(RuntimeError):
    """A storage failure whose message contains no remote/config secrets."""


class GDriveStorage:
    BLOCK_SIZE = 4 * 1024 * 1024
    QUOTA_TTL = 60
    HTTP_TIMEOUT = 8
    SLOT_TIMEOUT = 8
    FUTURE_TIMEOUT = 10

    def __init__(self, remote="", config_path=None, cache_bytes=128 * 1024**2):
        self.remote = (remote or "").strip()
        if self.remote:
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*:[^\x00-\x1f\\]*", self.remote):
                raise ValueError("Invalid Google Drive remote")
            prefix = self.remote.split(":", 1)[1].rstrip("/")
            if any(p in (".", "..") for p in prefix.split("/")):
                raise ValueError("Invalid Google Drive remote prefix")
            self.remote = self.remote.split(":", 1)[0] + ":" + prefix
        self.config_path = config_path or None
        self.cache_bytes = max(0, int(cache_bytes))
        self._lock = threading.Lock()
        self._server_lock = threading.Lock()
        self._fetch_slots = threading.BoundedSemaphore(2)
        self._cache = collections.OrderedDict()
        self._cache_size = 0
        self._epoch = 0
        self._inflight = {}
        self._proc = None
        self._base_url = None
        self._closed = False
        self._quota_at = 0.0
        self._quota_running = False
        self._quota = self._empty_quota()

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("VOD_GDRIVE_REMOTE", ""),
                   os.environ.get("RCLONE_CONFIG"),
                   int(os.environ.get("VOD_GDRIVE_CACHE_MB", "128")) * 1024**2)

    @property
    def enabled(self):
        return bool(self.remote)

    def _empty_quota(self):
        return {"enabled": self.enabled, "available": False,
                "total_gb": None, "used_gb": None, "free_gb": None,
                "updating": False, "error": None}

    def _command(self, *args):
        cmd = ["rclone"]
        if self.config_path:
            cmd.extend(["--config", self.config_path])
        return cmd + list(args)

    def _require_enabled(self):
        if not self.enabled:
            raise StorageError("Google Drive storage is disabled")
        if self._closed:
            raise StorageError("Google Drive storage is closed")

    def _run(self, operation, *args, timeout=120):
        self._require_enabled()
        try:
            result = subprocess.run(self._command(*args), capture_output=True,
                                    text=True, check=True, timeout=timeout)
            return result.stdout
        except (OSError, subprocess.SubprocessError):
            raise StorageError("Google Drive %s failed" % operation) from None

    def _object(self, filename):
        if not isinstance(filename, str) or not re.fullmatch(r"[0-9a-f]{8}\.mp4", filename):
            raise StorageError("Invalid Google Drive VOD object")
        return self.remote + ("" if self.remote.endswith(":") else "/") + filename

    def _task(self, task):
        if not isinstance(task, dict) or task.get("storage") != "gdrive":
            raise StorageError("Invalid Google Drive VOD metadata")
        filename = task.get("remote_path")
        self._object(filename)
        if task.get("id") is not None and task["id"] != filename[:-4]:
            raise StorageError("Google Drive VOD ownership mismatch")
        size = task.get("file_size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise StorageError("Invalid Google Drive VOD size")
        return filename, size

    def upload(self, local_path, task_id):
        """Upload, verify size/hash, and return metadata; never unlink source."""
        self._require_enabled()
        if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{8}", task_id):
            raise StorageError("Invalid Google Drive VOD id")
        filename = task_id + ".mp4"
        target = self._object(filename)
        digest = hashlib.md5()
        try:
            with open(local_path, "rb") as source:
                size = os.fstat(source.fileno()).st_size
                for data in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(data)
        except OSError:
            raise StorageError("Cannot read local VOD source") from None
        if size <= 0:
            raise StorageError("Cannot upload an empty VOD")
        self._run("upload", "copyto", os.path.abspath(local_path), target,
                  "--retries", "2", "--low-level-retries", "3", timeout=7200)
        try:
            stat = json.loads(self._run("verification", "lsjson", target, "--stat", "--hash"))
            remote_md5 = next((str(value).lower() for name, value in stat.get("Hashes", {}).items()
                               if name.lower() == "md5"), "")
            if stat.get("Size") != size or remote_md5 != digest.hexdigest():
                raise StorageError("Google Drive upload verification failed")
        except (ValueError, AttributeError, TypeError):
            raise StorageError("Google Drive upload verification failed") from None
        self._invalidate(filename)
        return {"storage": "gdrive", "remote_path": filename,
                "file_size": size, "remote_md5": remote_md5}

    def delete(self, task):
        filename, _ = self._task(task)
        self._run("delete", "deletefile", self._object(filename))
        self._invalidate(filename)

    def _invalidate(self, filename):
        with self._lock:
            self._epoch += 1
            for key in list(self._cache):
                if key[0] == filename:
                    self._cache_size -= len(self._cache.pop(key))
            self._quota_at = 0.0
        # copyto/deletefile run outside this VFS. Flush its directory metadata
        # after publication, preserving the process and active HTTP downloads.
        with self._server_lock:
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.send_signal(signal.SIGHUP)
                except ProcessLookupError:
                    pass

    def quota(self):
        """Return immediately with cached quota and request one async refresh."""
        with self._lock:
            if (self.enabled and not self._closed and not self._quota_running
                    and (not self._quota_at or time.monotonic() - self._quota_at >= self.QUOTA_TTL)):
                self._quota_running = True
                threading.Thread(target=self._refresh_quota, daemon=True,
                                 name="gdrive-quota").start()
            result = dict(self._quota)
            result["updating"] = self._quota_running
            return result

    def _refresh_quota(self):
        with self._lock:
            epoch = self._epoch
        result = self._empty_quota()
        try:
            account = self.remote.split(":", 1)[0] + ":"
            data = json.loads(self._run("quota", "about", account, "--json", timeout=30))
            for source, dest in (("total", "total_gb"), ("used", "used_gb"), ("free", "free_gb")):
                value = data.get(source)
                result[dest] = round(value / 1024**3, 3) if isinstance(value, (int, float)) and value >= 0 else None
            result["available"] = any(result[k] is not None for k in ("total_gb", "used_gb", "free_gb"))
            if not result["available"]:
                result["error"] = "Google Drive quota unavailable"
        except (StorageError, ValueError, AttributeError, TypeError):
            result["error"] = "Google Drive quota unavailable"
        finally:
            with self._lock:
                self._quota = result
                self._quota_running = False
                self._quota_at = time.monotonic() if epoch == self._epoch else 0.0

    def _ensure_server(self, deadline=None):
        self._require_enabled()
        deadline = min(time.monotonic() + self.HTTP_TIMEOUT, deadline or float("inf"))
        if not self._server_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise StorageError("Google Drive HTTP reader start timed out")
        try:
            self._require_enabled()
            if self._proc is not None and self._proc.poll() is None:
                return self._base_url
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            try:
                self._proc = subprocess.Popen(self._command(
                    "serve", "http", self.remote, "--addr", "127.0.0.1:%d" % port,
                    "--read-only", "--vfs-cache-mode", "off", "--dir-cache-time", "5m",
                    "--buffer-size", "4M",
                    "--vfs-read-chunk-size", "4M", "--vfs-read-chunk-size-limit", "4M"),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                while time.monotonic() < deadline:
                    if self._proc.poll() is not None:
                        break
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            self._base_url = "http://127.0.0.1:%d/" % port
                            return self._base_url
                    except OSError:
                        time.sleep(0.05)
            except OSError:
                pass
            self._stop_server()
            raise StorageError("Google Drive HTTP reader failed to start")
        finally:
            self._server_lock.release()

    def _stop_server(self):
        proc, self._proc = self._proc, None
        self._base_url = None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            except OSError:
                pass

    def _fetch(self, filename, total, start, length):
        deadline = time.monotonic() + self.HTTP_TIMEOUT
        base = urllib.parse.urlsplit(self._ensure_server(deadline))
        end = start + length - 1
        connection = None
        response = None
        watchdog = None

        def remaining():
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise StorageError("Google Drive media read timed out")
            return budget

        try:
            connection = http.client.HTTPConnection(base.hostname, base.port, timeout=remaining())
            connection.connect()
            transport = connection.sock

            def expire():
                # Also bound HTTP header parsing, which internally reads lines.
                # At most two fetches means at most two short-lived watchdogs.
                try:
                    transport.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            watchdog = threading.Timer(remaining(), expire)
            watchdog.daemon = True
            watchdog.start()
            transport.settimeout(remaining())
            connection.request("GET", base.path + urllib.parse.quote(filename, safe=""),
                               headers={"Range": "bytes=%d-%d" % (start, end), "Connection": "close"})
            transport.settimeout(remaining())
            response = connection.getresponse()
            remaining()
            expected = "bytes %d-%d/%d" % (start, end, total)
            if (response.status != 206 or response.headers.get("Content-Range") != expected
                    or response.headers.get("Content-Length") != str(length)):
                raise StorageError("Google Drive returned an invalid byte range")
            data = bytearray()
            while len(data) < length:
                transport.settimeout(remaining())
                chunk = response.read1(min(131072, length - len(data)))
                remaining()
                if not chunk:
                    raise StorageError("Google Drive returned incomplete media")
                data.extend(chunk)
            return bytes(data)
        except StorageError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise StorageError("Google Drive media read failed") from None
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()

    def _block(self, task, index):
        filename, total = self._task(task)
        key = (filename, total, task.get("remote_md5", ""), index)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
            future = self._inflight.get(key)
            owner = future is None
            if owner:
                future = concurrent.futures.Future()
                self._inflight[key] = future
            epoch = self._epoch
        if not owner:
            try:
                return future.result(timeout=self.FUTURE_TIMEOUT)
            except concurrent.futures.TimeoutError:
                raise StorageError("Google Drive media read timed out") from None
        try:
            start = index * self.BLOCK_SIZE
            length = min(self.BLOCK_SIZE, total - start)
            if not self._fetch_slots.acquire(timeout=self.SLOT_TIMEOUT):
                raise StorageError("Google Drive media reader is busy")
            try:
                data = self._fetch(filename, total, start, length)
            finally:
                self._fetch_slots.release()
            with self._lock:
                if not self._closed and epoch == self._epoch and len(data) <= self.cache_bytes:
                    while self._cache and self._cache_size + len(data) > self.cache_bytes:
                        self._cache_size -= len(self._cache.popitem(last=False)[1])
                    self._cache[key] = data
                    self._cache_size += len(data)
            future.set_result(data)
            return data
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._inflight.pop(key, None)

    def read(self, task, start, length):
        self._require_enabled()
        _, total = self._task(task)
        if (not isinstance(start, int) or not isinstance(length, int)
                or start < 0 or length < 0 or start + length > total):
            raise StorageError("Invalid VOD byte range")
        # Bound one returned allocation; the HTTP handler streams larger ranges.
        if length > 8 * 1024**2:
            raise StorageError("VOD byte range exceeds 8 MiB")
        result = bytearray()
        while length:
            index, offset = divmod(start, self.BLOCK_SIZE)
            block = self._block(task, index)
            count = min(length, len(block) - offset)
            result.extend(block[offset:offset + count])
            start += count
            length -= count
        return bytes(result)

    def warm(self, task):
        """Fetch startup head/window and final index block before USB play."""
        self._require_enabled()
        _, total = self._task(task)
        for index in range(min(3, (total + self.BLOCK_SIZE - 1) // self.BLOCK_SIZE)):
            self._block(task, index)
        tail = (total - 1) // self.BLOCK_SIZE
        if tail >= 3:
            self._block(task, tail)

    def close(self):
        with self._server_lock:
            self._closed = True
            self._stop_server()
        with self._lock:
            self._cache.clear()
            self._cache_size = 0
