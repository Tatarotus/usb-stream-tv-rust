"""Security and range integrity checks for the mixed-VOD loopback bridge."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import run_mixed_vod_proxy as proxy  # noqa: E402

TOKEN = 'x' * 64
PIN = 'private-pin'
TASK = '0123abcd'
REVISION = 'md5:' + 'a' * 32
OTHER_REVISION = 'md5:' + 'b' * 32
MEDIA = bytes(range(256)) * 1024


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append({
            'path': self.path,
            'pin': self.headers.get('X-Auth-PIN'),
            'revision': self.headers.get('X-Vod-Revision'),
            'auth': self.headers.get('Authorization'),
        })
        mode = self.server.mode
        if mode == 'redirect':
            self.send_response(302)
            self.send_header('Location', '/leak')
            self.send_header('Content-Length', '0')
            self.end_headers()
            self.close_connection = True
            return
        if mode == 'reject-revision':
            self.send_response(412)
            self.send_header('Content-Length', '0')
            self.end_headers()
            self.close_connection = True
            return
        if mode == 'unavailable':
            self.send_response(503)
            self.send_header('Content-Length', '0')
            self.end_headers()
            self.close_connection = True
            return
        first, last = map(int, self.headers['Range'][6:].split('-'))
        body = MEDIA[first:last + 1]
        self.send_response(200 if mode == 'wrong-status' else 206)
        self.send_header('Content-Range', f'bytes {first}-{last}/{len(MEDIA) + (1 if mode == "wrong-size" else 0)}')
        self.send_header('Content-Length', str(len(body) + (1 if mode == 'wrong-length' else 0)))
        if mode == 'encoded':
            self.send_header('Content-Encoding', 'gzip')
        self.end_headers()
        self.wfile.write(body[:-1] if mode == 'truncated' else body)
        self.close_connection = True


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.source = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.source.mode = ''
        self.source.requests = []
        self.proxy = proxy.MixedVodServer(('127.0.0.1', 0), token=TOKEN, pin=PIN,
            upstream=f'http://127.0.0.1:{self.source.server_port}',
            tasks={TASK: (len(MEDIA), REVISION)})
        self.threads = []
        for server in (self.source, self.proxy):
            self._serve(server)

    def _serve(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.threads.append(thread)
        return server

    def tearDown(self):
        for server in (self.proxy, self.source):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)

    def start_proxy(self, *, upstream=None, tasks=None):
        server = proxy.MixedVodServer(
            ('127.0.0.1', 0), token=TOKEN, pin=PIN,
            upstream=upstream or f'http://127.0.0.1:{self.source.server_port}',
            tasks={TASK: (len(MEDIA), REVISION)} if tasks is None else tasks)
        self._serve(server)
        return server

    @staticmethod
    def closed_port():
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def request(self, path=None, *, method='GET', auth=TOKEN, revision=REVISION,
                byte_range='bytes=10-99', port=None):
        headers = {}
        if auth is not None:
            headers['Authorization'] = 'Bearer ' + auth
        if revision is not None:
            headers['X-Vod-Revision'] = revision
        if byte_range is not None:
            headers['Range'] = byte_range
        client = http.client.HTTPConnection(
            '127.0.0.1', port or self.proxy.server_port, timeout=5)
        try:
            client.request(method, path or f'/vod/{TASK}', headers=headers)
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    def test_authorized_range_requires_matching_revision_and_forwards_headers(self):
        # Missing / wrong / malformed revision all fail closed with 409.
        for revision in (None, OTHER_REVISION, 'deadbeef', 'MD5:' + 'a' * 32,
                         'md5:NOTHEX'):
            with self.subTest(revision=revision):
                status, headers, body = self.request(revision=revision)
                self.assertEqual((status, body), (409, b''))
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertFalse(self.source.requests)

        # The exact revision succeeds and credentials never leak cross-hop.
        status, headers, body = self.request()
        self.assertEqual(status, 206)
        self.assertEqual(body, MEDIA[10:100])
        self.assertEqual(headers['Content-Range'], f'bytes 10-99/{len(MEDIA)}')
        self.assertEqual(headers['Content-Length'], '90')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.source.requests, [{
            'path': f'/api/internal/vod-range/{TASK}',
            'pin': PIN,
            'revision': REVISION,
            'auth': None,
        }])

    def test_auth_route_and_head(self):
        self.assertEqual(self.request(auth=None)[0], 403)
        self.assertEqual(self.request(auth='wrong')[0], 403)
        self.assertEqual(self.request('/vod/ffffffff')[0], 404)
        self.assertEqual(self.request('/vod/0123abcd?token=secret')[0], 404)
        self.assertEqual(self.request(method='HEAD', revision=None, byte_range=None)[0], 409)
        status, headers, body = self.request(method='HEAD', byte_range=None)
        self.assertEqual((status, body), (200, b''))
        self.assertEqual(headers['Content-Length'], str(len(MEDIA)))
        self.assertFalse(self.source.requests)

    def test_probe_performs_real_upstream_read(self):
        self.assertEqual(self.request('/probe', auth=None, revision=None)[0], 403)
        self.assertEqual(self.request('/probe', auth='bad-token', revision=None)[0], 403)

        status, headers, body = self.request('/probe', revision=None, byte_range=None)
        self.assertEqual(status, 200)
        self.assertEqual(body, f'OK {TASK} {len(MEDIA)}\n'.encode())
        self.assertEqual(headers['Content-Type'], 'text/plain')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual([r['path'] for r in self.source.requests],
                         [f'/api/internal/vod-range/{TASK}'])
        self.assertEqual(self.source.requests[0]['revision'], REVISION)
        self.assertEqual(self.source.requests[0]['pin'], PIN)

        status, headers, body = self.request('/probe', method='HEAD', revision=None,
                                             byte_range=None)
        self.assertEqual(status, 200)
        self.assertEqual(body, b'')
        self.assertEqual(headers['Content-Length'], '0')
        self.assertEqual(len(self.source.requests), 2)

    def test_probe_uses_first_task_and_probes_only_64_bytes(self):
        status, _, body = self.request('/probe', revision=None, byte_range=None)
        self.assertEqual(status, 200)
        self.assertEqual(body, f'OK {TASK} {len(MEDIA)}\n'.encode())

    def test_probe_upstream_down_returns_503(self):
        server = self.start_proxy(
            upstream=f'http://127.0.0.1:{self.closed_port()}')
        for method in ('GET', 'HEAD'):
            with self.subTest(method=method):
                status, _, body = self.request('/probe', method=method, revision=None,
                                               byte_range=None, port=server.server_port)
                self.assertEqual((status, body), (503, b''))

    def test_probe_rejects_short_and_rejected_upstream(self):
        for mode in ('truncated', 'reject-revision', 'unavailable',
                     'wrong-status', 'wrong-length', 'encoded', 'redirect'):
            with self.subTest(mode=mode):
                self.source.mode = mode
                self.source.requests.clear()
                status, _, body = self.request('/probe', revision=None, byte_range=None)
                self.assertEqual((status, body), (503, b''))
        self.assertFalse(any(r['path'] == '/leak' for r in self.source.requests))

    def test_probe_with_empty_allowlist_returns_503(self):
        server = self.start_proxy(tasks={})
        status, _, body = self.request('/probe', revision=None, byte_range=None,
                                       port=server.server_port)
        self.assertEqual((status, body), (503, b''))
        self.assertFalse(self.source.requests)

    def test_invalid_and_oversized_ranges_never_reach_upstream(self):
        self.assertEqual(self.request(byte_range=None)[0], 400)
        self.assertEqual(self.request(byte_range='bytes=0-1,4-5')[0], 400)
        self.assertEqual(self.request(byte_range='bytes=2-1')[0], 416)
        self.assertEqual(self.request(byte_range=f'bytes=0-{len(MEDIA)}')[0], 416)
        self.proxy.tasks[TASK] = (proxy.MAX_RANGE + 1, REVISION)
        self.assertEqual(self.request(byte_range=f'bytes=0-{proxy.MAX_RANGE}')[0], 416)
        self.assertFalse(self.source.requests)

    def test_bad_upstream_responses_fail_closed(self):
        for mode in ('wrong-status', 'wrong-size', 'wrong-length', 'encoded', 'redirect',
                     'truncated', 'reject-revision', 'unavailable'):
            with self.subTest(mode=mode):
                self.source.mode = mode
                status, _, body = self.request()
                self.assertEqual((status, body), (503, b''))
        self.assertFalse(any(r['path'] == '/leak' for r in self.source.requests))

    def test_configuration_rejects_public_http_and_insecure_allowlists(self):
        for value in ('http://example.com', 'http://user:pw@127.0.0.1',
                      'http://127.0.0.1/path', 'file:///tmp/a'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                proxy.validate_upstream(value)

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'allow.json'

            def load(tasks):
                path.write_text(json.dumps({'tasks': tasks}))
                return proxy.load_allowlist(path)

            self.assertEqual(load([{'task_id': TASK, 'size': 5, 'revision': REVISION}]),
                             {TASK: (5, REVISION)})
            for tasks in (
                [{'task_id': TASK, 'size': 5}],                                   # missing revision
                [{'task_id': TASK, 'size': 5, 'revision': 'deadbeef'}],           # bad revision
                [{'task_id': TASK, 'size': 5, 'revision': 'MD5:' + 'A' * 32}],    # non-canonical
                [{'task_id': TASK, 'size': 5, 'revision': REVISION},
                 {'task_id': TASK, 'size': 6, 'revision': REVISION}],             # duplicate id
                [{'task_id': TASK, 'size': 0, 'revision': REVISION}],             # bad size
                [{'task_id': TASK, 'size': -1, 'revision': REVISION}],
                [{'task_id': TASK, 'size': True, 'revision': REVISION}],
                [{'task_id': TASK, 'size': '5', 'revision': REVISION}],
                [{'task_id': 'XYZ', 'size': 5, 'revision': REVISION}],            # bad id
                [{'task_id': TASK}],                                              # bad shape
            ):
                with self.subTest(tasks=tasks), self.assertRaises(ValueError):
                    load(tasks)


if __name__ == '__main__':
    unittest.main()
