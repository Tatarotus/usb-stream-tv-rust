"""Exercise inferred episode tracking through the real internal range handler."""
import http.client
import threading
import types
from unittest import mock

import pytest
import server
from scripts.mixed_watch_signal import MixedWatchRecorder, MixedWatchSignal
from vod_history import VodHistory
from vod_revision import content_revision

M = 1024 * 1024


@pytest.mark.parametrize('enabled', [True, False])
def test_real_range_handler_tracks_sustained_reads_without_switching_mode(tmp_path, enabled):
    task = {'id': '1234abcd', 'title': 'A Noiva de Istambul 230 - Star 528p',
            'status': 'ready', 'storage': 'gdrive', 'remote_path': 'episode.mp4',
            'file_size': 100 * M, 'remote_md5': 'a' * 32}
    now = [100.0]
    history = VodHistory(str(tmp_path / 'history.json'))
    recorder = MixedWatchRecorder(lambda key: dict(task) if key == task['id'] else None,
                                  history, MixedWatchSignal(clock=lambda: now[0]))
    done = threading.Event()
    observe = recorder.observe

    def observed(*args):
        observe(*args)
        done.set()

    recorder.observe = observed
    cache = types.SimpleNamespace(read=lambda item, offset, length: b'x' * length)
    patches = [mock.patch.object(server, 'AUTH_PIN', 'test-pin'),
               mock.patch.dict('os.environ', {'MIXED_WATCH_INFERENCE': '1' if enabled else '0'}),
               mock.patch.object(server, 'VOD_TASKS', {task['id']: task}),
               mock.patch.object(server, 'GDRIVE_STORAGE', types.SimpleNamespace(enabled=True)),
               mock.patch.object(server, 'VOD_PLAYBACK_CACHE', cache),
               mock.patch.object(server, 'MIXED_WATCH_RECORDER', recorder),
               mock.patch.object(server, '_mixed_cache_hint'),
               mock.patch.object(server, 'ACTIVE_VOD_TASK', None)]
    for patch in patches:
        patch.start()
    httpd = server.ThreadedHTTPServer(('127.0.0.1', 0), server.RequestHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        for n in range(6):
            now[0] = 100 + 10 * n
            start = (10 + n) * M
            done.clear()
            conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=5)
            conn.request('GET', f'/api/internal/vod-range/{task["id"]}', headers={
                'X-Auth-PIN': 'test-pin', 'X-Vod-Revision': content_revision(task),
                'Range': f'bytes={start}-{start + M - 1}'})
            response = conn.getresponse()
            assert response.status == 206
            assert len(response.read()) == M
            if enabled:
                assert done.wait(2)
            conn.close()
        recorder.pending.join()
        results = history.summary({task['id']: task})
        if enabled:
            assert results[0]['episode_number'] == 230
            assert results[0]['position_source'] == 'inferred_stream'
            assert results[0]['position_seconds'] is None
        else:
            assert results == []
        assert server.ACTIVE_VOD_TASK is None
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        for patch in reversed(patches):
            patch.stop()
