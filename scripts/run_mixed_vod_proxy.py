#!/usr/bin/env python3
"""Loopback-only bridge for bounded byte reads from ready VOD files.

Tablet protocol (over the existing authenticated private tunnel):

* GET /vod/<8-lowercase-hex-task-id> with Authorization: Bearer <token>,
  X-Vod-Revision: <md5|fs|rev>:<32 lowercase hex> and Range: bytes=<start>-<end>.
  Ranges are inclusive and at most 4 MiB.
* HEAD on the same path requires the same bearer token and revision and returns
  the approved file size.
* GET returns 206 with exact Content-Range, Content-Length and media bytes;
  any upstream mismatch (including a 412 revision rejection) returns an error
  without exposing response contents.
* GET/HEAD /probe performs one real upstream range read of the first allowlisted
  task to prove the whole read path is live.

The upstream endpoint is /api/internal/vod-range/<task-id> and is called with
the private X-Auth-PIN and X-Vod-Revision headers. The bridge never logs headers,
URLs or secrets.
"""
from __future__ import annotations

import argparse
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vod_revision import (  # noqa: E402
    REVISION_HEADER,
    is_valid_revision,
    parse_allowlist_entry,
)

MAX_RANGE = 4 * 1024 * 1024
PROBE_WINDOW = 64
RANGE_RE = re.compile(r"bytes=(\d{1,20})-(\d{1,20})")


def validate_upstream(url: str) -> str:
    parts = urlsplit(url)
    if (parts.username is not None or parts.password is not None or parts.query
            or parts.fragment or parts.path not in ('', '/') or not parts.hostname):
        raise ValueError('invalid upstream origin')
    if parts.scheme == 'http':
        if parts.hostname not in ('127.0.0.1', '::1'):
            raise ValueError('plain HTTP upstream must use loopback')
    elif parts.scheme != 'https':
        raise ValueError('upstream must use HTTPS or loopback HTTP')
    try:
        _ = parts.port
    except ValueError as exc:
        raise ValueError('invalid upstream port') from exc
    return url.rstrip('/')


def load_allowlist(path: Path) -> dict[str, tuple[int, str]]:
    """Return ``{task_id: (size, revision)}`` for a fully trusted allowlist.

    Every task must carry a canonical trusted revision; any malformed entry
    (missing/invalid revision, bad size, non-canonical task id, duplicate id or
    unusable shape) fails closed with ``ValueError``.
    """
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('allowlist too large')
    # Bounded read: never trust a stat() size that could change before the read.
    with path.open('rb') as handle:
        raw_bytes = handle.read(1024 * 1024 + 1)
    if len(raw_bytes) > 1024 * 1024:
        raise ValueError('allowlist too large')
    raw = json.loads(raw_bytes)
    tasks = raw.get('tasks') if isinstance(raw, dict) else None
    if not isinstance(tasks, list) or not tasks or len(tasks) > 4096:
        raise ValueError('invalid allowlist')
    result: dict[str, tuple[int, str]] = {}
    for item in tasks:
        parsed = parse_allowlist_entry(item)
        if parsed is None or parsed[0] in result:
            raise ValueError('invalid task identity, size or revision')
        task_id, size, revision = parsed
        result[task_id] = (size, revision)
    return result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MixedVodServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], *, token: str, pin: str,
                 upstream: str, tasks: dict[str, tuple[int, str]]):
        if address[0] != '127.0.0.1':
            raise ValueError('proxy must bind to IPv4 loopback')
        if not isinstance(token, str) or len(token.encode('utf-8')) < 32 or not pin:
            raise ValueError('token and PIN must be configured')
        self.token = token.encode('utf-8')
        self.pin = pin
        self.upstream = validate_upstream(upstream)
        self.tasks = dict(tasks)
        self.opener = build_opener(NoRedirect())
        super().__init__(address, MixedVodHandler)


class MixedVodHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def finish_error(self, code: int, *, size: int | None = None):
        self.send_response(code)
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        if size is not None:
            self.send_header('Content-Range', f'bytes */{size}')
        self.end_headers()

    def _check_auth(self) -> bool:
        supplied = self.headers.get('Authorization', '')
        prefix = 'Bearer '
        if not supplied.startswith(prefix) or not hmac.compare_digest(
                supplied[len(prefix):].encode('utf-8'), self.server.token):
            self.finish_error(403)
            return False
        return True

    def authorized_task(self):
        """Auth (403) then task membership (404) then revision match (409)."""
        if not self._check_auth():
            return None
        match = re.fullmatch(r'/vod/([0-9a-f]{8})', self.path)
        if not match or match[1] not in self.server.tasks:
            self.finish_error(404)
            return None
        task_id = match[1]
        size, revision = self.server.tasks[task_id]
        supplied = self.headers.get(REVISION_HEADER, '')
        if not is_valid_revision(supplied) or not hmac.compare_digest(supplied, revision):
            self.finish_error(409)
            return None
        return task_id, size, revision

    def _fetch_upstream(self, task_id: str, size: int, revision: str,
                        start: int, end: int) -> bytes | None:
        """Fetch one exact upstream range; ``None`` on any mismatch or failure.

        Shared by /vod/<id> and /probe so byte-level validation cannot drift:
        206 status, exact Content-Range, exact Content-Length, identity encoding
        and an exact-length body read.
        """
        length = end - start + 1
        expected_range = f'bytes {start}-{end}/{size}'
        request = Request(f'{self.server.upstream}/api/internal/vod-range/{task_id}',
                          headers={'X-Auth-PIN': self.server.pin,
                                   REVISION_HEADER: revision,
                                   'Range': f'bytes={start}-{end}',
                                   'Accept-Encoding': 'identity'}, method='GET')
        try:
            with self.server.opener.open(request, timeout=30) as response:
                if (response.status != 206
                        or response.headers.get('Content-Range') != expected_range
                        or response.headers.get('Content-Length') != str(length)
                        or response.headers.get('Content-Encoding', 'identity').lower() != 'identity'):
                    raise ValueError('upstream response mismatch')
                data = response.read(length + 1)
                if len(data) != length:
                    raise ValueError('upstream body mismatch')
        except (HTTPError, URLError, OSError, TimeoutError, ValueError, http.client.HTTPException):
            return None
        return data

    def _probe(self, *, head: bool):
        if not self._check_auth():
            return
        tasks = self.server.tasks
        if not tasks:
            self.finish_error(503)
            return
        task_id = min(tasks)
        size, revision = tasks[task_id]
        end = min(PROBE_WINDOW - 1, size - 1)
        data = self._fetch_upstream(task_id, size, revision, 0, end)
        if data is None or not data:
            self.finish_error(503)
            return
        if head:
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return
        body = f'OK {task_id} {size}\n'.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        if self.path == '/probe':
            self._probe(head=True)
            return
        task = self.authorized_task()
        if task is None:
            return
        task_id, size, revision = task
        self.send_response(200)
        self.send_header('Content-Length', str(size))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def do_GET(self):
        if self.path == '/probe':
            self._probe(head=False)
            return
        task = self.authorized_task()
        if task is None:
            return
        task_id, size, revision = task
        match = RANGE_RE.fullmatch(self.headers.get('Range', '').strip())
        if not match:
            self.finish_error(400)
            return
        start, end = int(match[1]), int(match[2])
        # The exact requested interval must be valid; do not silently clip EOF.
        if start > end or end >= size or end - start + 1 > MAX_RANGE:
            self.finish_error(416, size=size)
            return
        length = end - start + 1
        expected_range = f'bytes {start}-{end}/{size}'
        data = self._fetch_upstream(task_id, size, revision, start, end)
        if data is None:
            self.finish_error(503)
            return
        self.send_response(206)
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('Content-Length', str(length))
        self.send_header('Content-Range', expected_range)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            self.close_connection = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--allowlist', type=Path, required=True)
    parser.add_argument('--upstream', required=True, help='HTTPS origin or loopback HTTP origin')
    args = parser.parse_args()
    try:
        if not 1 <= args.port <= 65535 or args.token_file.stat().st_size > 4096:
            raise ValueError('invalid port or token')
        token = args.token_file.read_text().strip()
        pin = os.environ.get('TV_PIN', '')
        server = MixedVodServer(('127.0.0.1', args.port), token=token, pin=pin,
                                upstream=args.upstream, tasks=load_allowlist(args.allowlist))
    except (OSError, ValueError, json.JSONDecodeError):
        parser.exit(2, 'invalid proxy configuration\n')
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
