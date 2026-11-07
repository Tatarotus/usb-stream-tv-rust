import threading

from scripts.mixed_watch_signal import MixedWatchRecorder, MixedWatchSignal
from vod_history import VodHistory

M = 1024 * 1024


def task(number, ident):
    return {'id': ident, 'title': f'A Noiva de Istambul {number} - Star 528p',
            'status': 'ready', 'storage': 'gdrive', 'file_size': 100 * M,
            'remote_md5': 'a' * 32}


def test_probes_and_fast_read_ahead_do_not_indicate_playback():
    now = [100.0]
    signal = MixedWatchSignal(clock=lambda: now[0])
    item = task(230, '22222222')
    assert signal.observe(item, 0, 64) is None
    assert signal.observe(item, 99 * M, M) is None
    for n in range(8, 20):
        assert signal.observe(item, n * M, M) is None


def test_sustained_high_bitrate_blocks_still_form_one_cluster():
    now = [100.0]
    signal = MixedWatchSignal(clock=lambda: now[0])
    item = dict(task(230, '22222222'), file_size=300 * M)
    observed = None
    for n in range(60):
        now[0] = 100 + n
        observed = signal.observe(item, (10 + n) * M, M)
        if observed:
            break
    assert observed and now[0] >= 145


def test_sustained_reads_with_seek_and_reordering_can_confirm():
    now = [100.0]
    signal = MixedWatchSignal(clock=lambda: now[0])
    item = task(230, '22222222')
    assert signal.observe(item, 8 * M, M) is None
    observed = None
    for n, seconds in zip((30, 32, 31, 33, 35, 34), (110, 120, 130, 140, 150, 160)):
        now[0] = seconds
        observed = signal.observe(item, n * M, M)
    assert observed and observed['task_id'] == item['id']
    signal.acknowledge(observed)
    assert signal.observe(item, 36 * M, M) is None


def test_reporter_revalidates_revision_and_records_off_thread(tmp_path):
    item = task(230, '22222222')
    current = dict(item)
    history = VodHistory(str(tmp_path / 'history.json'))
    now = [100.0]
    signal = MixedWatchSignal(clock=lambda: now[0])
    recorder = MixedWatchRecorder(lambda ident: dict(current) if ident == current['id'] else None,
                                  history, signal)
    worker_thread = []
    original = history.record_inferred

    def record(value):
        worker_thread.append(threading.current_thread().name)
        return original(value)

    history.record_inferred = record
    for n in range(6):
        now[0] = 100 + n * 10
        recorder.observe(item, (10 + n) * M, M)
    recorder.pending.join()
    result = history.summary({item['id']: item})
    assert result[0]['episode_number'] == 230
    assert result[0]['position_source'] == 'inferred_stream'
    assert result[0]['position_seconds'] is None
    assert worker_thread == ['mixed-watch-history']


def test_reporter_drops_deleted_or_replaced_task(tmp_path):
    item = task(230, '22222222')
    replacement = dict(item, remote_md5='b' * 32)
    history = VodHistory(str(tmp_path / 'history.json'))
    now = [100.0]
    recorder = MixedWatchRecorder(lambda _: replacement, history, MixedWatchSignal(clock=lambda: now[0]))
    for n in range(6):
        now[0] = 100 + n * 10
        recorder.observe(item, (10 + n) * M, M)
    recorder.pending.join()
    assert history.summary({item['id']: replacement}) == []


def test_inferred_episode_supersedes_old_selection_without_completion(tmp_path):
    now = [100.0]
    history = VodHistory(str(tmp_path / 'history.json'), clock=lambda: now[0])
    old, new = task(164, '11111111'), task(230, '22222222')
    history.record_selected(old)
    now[0] = 130
    assert history.record_inferred(new) is None  # recent explicit choice wins
    now[0] = 200
    history.record_inferred(new)
    result = history.summary({old['id']: old, new['id']: new})[0]
    assert result['episode_number'] == 230
    assert result['position_source'] == 'inferred_stream'
    assert history._data['series']['import:istanbullu-star']['completed'] == {}
    reloaded = VodHistory(str(tmp_path / 'history.json'))
    assert reloaded.summary({new['id']: new})[0]['episode_number'] == 230


def test_inference_does_not_undo_explicit_completion(tmp_path):
    history = VodHistory(str(tmp_path / 'history.json'))
    item = task(230, '22222222')
    history.complete(item)
    assert history.record_inferred(item) is None
    assert item['id'] in history._data['series']['import:istanbullu-star']['completed']


def test_newer_explicit_selection_wins_over_old_checkpoint_and_inference(tmp_path):
    now = [100.0]
    history = VodHistory(str(tmp_path / 'history.json'), clock=lambda: now[0])
    old, middle, newest = task(164, '11111111'), task(230, '22222222'), task(231, '33333333')
    history.checkpoint(old)
    now[0] = 200
    history.record_inferred(middle)
    now[0] = 300
    history.record_selected(newest)
    result = history.summary({t['id']: t for t in (old, middle, newest)})[0]
    assert result['episode_number'] == 231
    assert result['position_source'] == 'selected'


def test_explicit_selection_wins_when_wall_clock_moves_backwards(tmp_path):
    now = [100.0]
    history = VodHistory(str(tmp_path / 'history.json'), clock=lambda: now[0])
    inferred, explicit = task(230, '22222222'), task(231, '33333333')
    history.record_inferred(inferred)
    now[0] = 50
    history.record_selected(explicit)
    assert history.summary({t['id']: t for t in (inferred, explicit)})[0]['episode_number'] == 231


def test_rejected_inference_can_retry_after_manual_grace(tmp_path):
    wall, mono = [100.0], [100.0]
    history = VodHistory(str(tmp_path / 'history.json'), clock=lambda: wall[0])
    old, new = task(164, '11111111'), task(230, '22222222')
    history.record_selected(old)
    signal = MixedWatchSignal(clock=lambda: mono[0])
    recorder = MixedWatchRecorder(lambda _: new, history, signal)
    for n in range(6):
        mono[0] = 100 + n * 9
        wall[0] = mono[0]
        recorder.observe(new, (10 + n) * M, M)
    recorder.pending.join()
    assert history.summary({old['id']: old, new['id']: new})[0]['episode_number'] == 164
    mono[0] = wall[0] = 161
    recorder.observe(new, 16 * M, M)
    recorder.pending.join()
    assert history.summary({old['id']: old, new['id']: new})[0]['episode_number'] == 230


def test_stale_queued_observation_is_not_recorded(tmp_path):
    item = task(230, '22222222')
    history = VodHistory(str(tmp_path / 'history.json'))
    class Signal:
        def observe(self, *args):
            return {'task_id': item['id'], 'revision': 'md5:' + 'a' * 32, 'observed_at': 100}
        def clock(self):
            return 131
    recorder = MixedWatchRecorder(lambda _: item, history, Signal())
    recorder.observe(item, 10 * M, M)
    recorder.pending.join()
    assert history.summary({item['id']: item}) == []
