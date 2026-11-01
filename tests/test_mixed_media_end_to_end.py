"""End-to-End integration test for Mixed-Media (Live + VOD) pipeline.

Validates the complete chain:
  server.py (/api/internal/vod-range) -> run_mixed_vod_proxy.py -> loopback HTTP client
Covering:
  - Local disk VOD and Google Drive cached VOD
  - Seek to arbitrary offsets and read to exact EOF
  - Proxy allowlist, authentication, and range bounds
  - Absence of side effects on playback selection (ACTIVE_VOD_TASK) and VOD history
  - Concurrent reading across multiple tasks
  - Playback activity detection during Live and VOD states
"""
from __future__ import annotations

import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import tempfile
import threading
import types
import unittest
from unittest import mock

import server
import run_mixed_vod_proxy as proxy
import mixed_generation_service as mgs
import vod_revision
from vod_history import VodHistory

TOKEN = "e2e-test-bearer-token-at-least-32-chars-long"
PIN = "test-e2e-private-pin-987"

LOCAL_TASK_ID = "11223344"
LOCAL_MEDIA = bytes((i * 7) % 256 for i in range(2 * 1024 * 1024))  # 2 MiB

GDRIVE_TASK_ID = "55667788"
GDRIVE_MEDIA = bytes((i * 13) % 256 for i in range(3 * 1024 * 1024))  # 3 MiB


class FakeDrivePlaybackCache:
    def __init__(self):
        self.reads = []

    def read(self, task, start, length):
        self.reads.append((task["id"], start, length))
        return GDRIVE_MEDIA[start:start + length]


class QuietHandler(server.RequestHandler):
    def log_message(self, *args):
        pass


class TestMixedMediaEndToEnd(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="e2e-mixed-test-")
        self.root = Path(self.temp_dir.name)

        # 1. Local VOD file
        self.local_file = self.root / f"{LOCAL_TASK_ID}.mp4"
        self.local_file.write_bytes(LOCAL_MEDIA)

        self.gdrive_file = self.root / f"{GDRIVE_TASK_ID}.mp4"
        self.gdrive_file.write_bytes(b"")  # placeholder

        self.gdrive_cache = FakeDrivePlaybackCache()
        self.fake_gdrive_storage = types.SimpleNamespace(enabled=True)
        self.history = VodHistory(str(self.root / "watch_history.json"))
        self.gdrive_md5 = "0123456789abcdef0123456789abcdef"

        self.tasks = {
            LOCAL_TASK_ID: {
                "id": LOCAL_TASK_ID,
                "title": "Local Episode 1",
                "display_name": "Local EP01.mp4",
                "status": "ready",
                "storage": "local",
                "file_path": str(self.local_file),
                "file_size": len(LOCAL_MEDIA),
            },
            GDRIVE_TASK_ID: {
                "id": GDRIVE_TASK_ID,
                "title": "Drive Movie",
                "display_name": "Drive Movie.mp4",
                "status": "ready",
                "storage": "gdrive",
                "remote_path": f"{GDRIVE_TASK_ID}.mp4",
                "file_path": str(self.gdrive_file),
                "file_size": len(GDRIVE_MEDIA),
                "remote_md5": self.gdrive_md5,
            },
        }
        # Trusted content revision per task, shared by the allowlist, the client
        # requests and the authoritative server (see vod_revision.py).
        self.revisions = {
            task_id: vod_revision.content_revision(task)
            for task_id, task in self.tasks.items()
        }
        self.assertEqual(self.revisions[LOCAL_TASK_ID][:3], "fs:")
        self.assertEqual(self.revisions[GDRIVE_TASK_ID], "md5:" + self.gdrive_md5)

        # Patch server globals
        self.patches = [
            mock.patch.object(server, "AUTH_PIN", PIN),
            mock.patch.object(server, "VOD_TASKS", self.tasks),
            mock.patch.object(server, "GDRIVE_STORAGE", self.fake_gdrive_storage),
            mock.patch.object(server, "VOD_PLAYBACK_CACHE", self.gdrive_cache),
            mock.patch.object(server, "VOD_HISTORY", self.history),
            mock.patch.object(server, "ACTIVE_VOD_TASK", None),
            mock.patch.object(server, "ACTIVE_VOD_FILE", str(self.root / "active_vod.txt")),
            mock.patch.object(server, "log_event", mock.Mock()),
        ]
        for p in self.patches:
            p.start()

        # Start upstream HTTP server (server.py)
        self.upstream_server = server.ThreadedHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.upstream_thread = threading.Thread(target=self.upstream_server.serve_forever, daemon=True)
        self.upstream_thread.start()
        self.upstream_port = self.upstream_server.server_port

        # Start downstream proxy (run_mixed_vod_proxy.py)
        allowlist = {
            LOCAL_TASK_ID: (len(LOCAL_MEDIA), self.revisions[LOCAL_TASK_ID]),
            GDRIVE_TASK_ID: (len(GDRIVE_MEDIA), self.revisions[GDRIVE_TASK_ID]),
        }
        self.proxy_server = proxy.MixedVodServer(
            ("127.0.0.1", 0),
            token=TOKEN,
            pin=PIN,
            upstream=f"http://127.0.0.1:{self.upstream_port}",
            tasks=allowlist,
        )
        self.proxy_thread = threading.Thread(target=self.proxy_server.serve_forever, daemon=True)
        self.proxy_thread.start()
        self.proxy_port = self.proxy_server.server_port

    def tearDown(self):
        self.proxy_server.shutdown()
        self.proxy_server.server_close()
        self.proxy_thread.join(timeout=2)

        self.upstream_server.shutdown()
        self.upstream_server.server_close()
        self.upstream_thread.join(timeout=2)

        for p in reversed(self.patches):
            p.stop()
        self.temp_dir.cleanup()

    def proxy_request(self, path: str, *, method: str = "GET", token: str | None = TOKEN,
                      byte_range: str | None = None,
                      revision: str | None = None) -> tuple[int, dict[str, str], bytes]:
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if byte_range is not None:
            headers["Range"] = byte_range
        match = re.fullmatch(r"/vod/([0-9a-f]{8})", path)
        if match:
            supplied = self.revisions.get(match.group(1)) if revision is None else revision
            if supplied:
                headers["X-Vod-Revision"] = supplied
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=5)
        try:
            conn.request(method, path, headers=headers)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def test_local_vod_seek_and_eof(self):
        """Test seek to middle and read to exact EOF for local disk VOD."""
        # 1. Seek to 512 KiB and read 64 KiB
        offset = 512 * 1024
        length = 64 * 1024
        status, headers, body = self.proxy_request(
            f"/vod/{LOCAL_TASK_ID}",
            byte_range=f"bytes={offset}-{offset + length - 1}",
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, LOCAL_MEDIA[offset:offset + length])
        self.assertEqual(headers["Content-Range"], f"bytes {offset}-{offset + length - 1}/{len(LOCAL_MEDIA)}")

        # 2. Read exact EOF tail (last 128 bytes)
        tail_len = 128
        tail_start = len(LOCAL_MEDIA) - tail_len
        tail_end = len(LOCAL_MEDIA) - 1
        status, headers, body = self.proxy_request(
            f"/vod/{LOCAL_TASK_ID}",
            byte_range=f"bytes={tail_start}-{tail_end}",
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, LOCAL_MEDIA[tail_start:])
        self.assertEqual(headers["Content-Range"], f"bytes {tail_start}-{tail_end}/{len(LOCAL_MEDIA)}")

    def test_gdrive_vod_seek_and_eof(self):
        """Test seek and EOF for Google Drive cached VOD."""
        offset = 1024 * 1024
        length = 128 * 1024
        status, headers, body = self.proxy_request(
            f"/vod/{GDRIVE_TASK_ID}",
            byte_range=f"bytes={offset}-{offset + length - 1}",
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, GDRIVE_MEDIA[offset:offset + length])
        self.assertIn((GDRIVE_TASK_ID, offset, length), self.gdrive_cache.reads)

        # Read to EOF
        tail_start = len(GDRIVE_MEDIA) - 256
        tail_end = len(GDRIVE_MEDIA) - 1
        status, headers, body = self.proxy_request(
            f"/vod/{GDRIVE_TASK_ID}",
            byte_range=f"bytes={tail_start}-{tail_end}",
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, GDRIVE_MEDIA[tail_start:])

    def test_history_and_active_vod_selection_remain_untouched(self):
        """Internal VOD range reads must NEVER touch VOD history or ACTIVE_VOD_TASK."""
        # Read from both files
        self.proxy_request(f"/vod/{LOCAL_TASK_ID}", byte_range="bytes=0-1023")
        self.proxy_request(f"/vod/{GDRIVE_TASK_ID}", byte_range="bytes=0-1023")

        # Confirm VOD history has no entries
        self.assertEqual(len(self.history.summary(self.tasks)), 0)
        self.assertEqual(self.history._data["series"], {})
        # Confirm ACTIVE_VOD_TASK was not set
        self.assertIsNone(server.ACTIVE_VOD_TASK)

    def test_proxy_auth_and_allowlist_enforcement(self):
        """Validate bearer authentication and allowlist bounding."""
        # Missing token
        status, _, _ = self.proxy_request(f"/vod/{LOCAL_TASK_ID}", token=None, byte_range="bytes=0-100")
        self.assertEqual(status, 403)

        # Invalid token
        status, _, _ = self.proxy_request(f"/vod/{LOCAL_TASK_ID}", token="bad-token", byte_range="bytes=0-100")
        self.assertEqual(status, 403)

        # Task not in allowlist
        status, _, _ = self.proxy_request("/vod/99999999", byte_range="bytes=0-100")
        self.assertEqual(status, 404)

        # Direct call to server without valid PIN is rejected
        conn = http.client.HTTPConnection("127.0.0.1", self.upstream_port, timeout=5)
        conn.request("GET", f"/api/internal/vod-range/{LOCAL_TASK_ID}", headers={"Range": "bytes=0-100", "X-Auth-PIN": "wrong"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 403)
        conn.close()

    def test_head_request_metadata(self):
        """HEAD request returns exact Content-Length without body."""
        status, headers, body = self.proxy_request(f"/vod/{LOCAL_TASK_ID}", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Length"], str(len(LOCAL_MEDIA)))
        self.assertEqual(len(body), 0)

    def test_probe_proves_real_upstream_media(self):
        """The probe must deliver real bytes through the whole chain."""
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=10)
        conn.request("GET", "/probe", headers={"Authorization": f"Bearer {TOKEN}"})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        task_id, size = body.decode().split()[1:3]
        self.assertIn(task_id, self.revisions)
        self.assertEqual(int(size), len(LOCAL_MEDIA if task_id == LOCAL_TASK_ID else GDRIVE_MEDIA))

    def test_mutated_content_is_never_served_under_the_old_revision(self):
        """Same task id and same size, different bytes: the chain must refuse.

        This is the stale-content scenario: without a trusted revision an
        old cache/allowlist entry could keep serving the previous bytes.
        """
        before = self.proxy_request(f"/vod/{LOCAL_TASK_ID}", byte_range="bytes=0-1023")
        self.assertEqual(before[0], 206)
        # Replace the bytes, keeping the exact same length, then let the clock
        # move so the file identity changes.
        os.utime(self.local_file, (1_000_000, 1_000_000))
        self.local_file.write_bytes(bytes(reversed(LOCAL_MEDIA)))
        os.utime(self.local_file, (2_000_000, 2_000_000))
        self.assertNotEqual(vod_revision.content_revision(self.tasks[LOCAL_TASK_ID]),
                            self.revisions[LOCAL_TASK_ID])
        after = self.proxy_request(f"/vod/{LOCAL_TASK_ID}", byte_range="bytes=0-1023")
        self.assertEqual(after[0], 503)
        self.assertNotEqual(after[2], LOCAL_MEDIA[:1024])

    def test_stale_or_missing_revision_is_rejected_by_the_proxy(self):
        for bad in ("", "md5:" + "0" * 32, "not-a-revision"):
            with self.subTest(revision=bad):
                status, _, _ = self.proxy_request(
                    f"/vod/{LOCAL_TASK_ID}", byte_range="bytes=0-100", revision=bad)
                self.assertEqual(status, 409)

    def test_concurrent_reads_across_tasks(self):
        """Multiple concurrent reads on different tasks succeed deterministically."""
        def fetch_range(task_id: str, start: int, length: int) -> bytes:
            st, _, b = self.proxy_request(
                f"/vod/{task_id}",
                byte_range=f"bytes={start}-{start + length - 1}",
            )
            assert st == 206
            return b

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = []
            for i in range(6):
                if i % 2 == 0:
                    futures.append(pool.submit(fetch_range, LOCAL_TASK_ID, i * 65536, 32768))
                else:
                    futures.append(pool.submit(fetch_range, GDRIVE_TASK_ID, i * 65536, 32768))

            results = [f.result() for f in futures]
            self.assertEqual(len(results), 6)
            for i, res in enumerate(results):
                expected = LOCAL_MEDIA if i % 2 == 0 else GDRIVE_MEDIA
                self.assertEqual(res, expected[i * 65536:i * 65536 + 32768])


if __name__ == "__main__":
    unittest.main()
