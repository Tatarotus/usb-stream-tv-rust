from scripts.mixed_cache_signal import MixedCacheSignal, next_episode

M = 1024 * 1024


def task(number, ident):
    return {'id': ident, 'title': f'A Noiva de Istambul {number} - Star 528p',
            'status': 'ready', 'storage': 'gdrive', 'file_size': 100 * M,
            'remote_md5': 'a' * 32}


def test_sequential_content_triggers_once_without_probe():
    signal = MixedCacheSignal()
    item = task(1, '11111111')
    assert not signal.observe(item, 0, 64)
    assert not signal.observe(item, 99 * M, M)
    assert not signal.observe(item, 8 * M, M)
    assert not signal.observe(item, 9 * M, M)
    assert signal.observe(item, 10 * M, M)
    assert not signal.observe(item, 11 * M, M)


def test_random_ranges_and_expired_sequence_do_not_trigger():
    now = [0]
    signal = MixedCacheSignal(clock=lambda: now[0])
    item = task(1, '11111111')
    assert not signal.observe(item, 8 * M, M)
    assert not signal.observe(item, 12 * M, M)
    now[0] = 31
    assert not signal.observe(item, 13 * M, M)
    assert not signal.observe(item, 14 * M, M)


def test_next_episode_requires_exact_unambiguous_ready_successor():
    first, second = task(1, '11111111'), task(2, '22222222')
    assert next_episode(first, [first, second]) == second
    assert next_episode(first, [first, task(3, '33333333')]) is None
    assert next_episode(first, [second, task(2, '44444444')]) is None
    second['status'] = 'downloading'
    assert next_episode(first, [second]) is None
