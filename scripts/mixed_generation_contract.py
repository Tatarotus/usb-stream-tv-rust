"""Offline consistency checks for a mixed generation's server-side inputs.

These checks do not start services and do not prove tablet handoff or playback.
"""
from __future__ import annotations

import json
from pathlib import Path

from run_live_favorites_manager import manifest_digest
from vod_revision import parse_allowlist_entry


def validate_generation(manifest_path: Path, allowlist_path: Path,
                        expected_generation: int, expected_manifest: str) -> dict:
    manifest = json.loads(Path(manifest_path).read_text())
    allowlist = json.loads(Path(allowlist_path).read_text())
    if manifest.get('generation') != expected_generation:
        raise ValueError('manifest generation mismatch')
    if manifest.get('manifest_id') != expected_manifest or manifest_digest(manifest) != expected_manifest:
        raise ValueError('manifest digest mismatch')
    entries = manifest.get('entries')
    tasks = allowlist.get('tasks') if isinstance(allowlist, dict) else None
    if not isinstance(entries, list) or not isinstance(tasks, list):
        raise ValueError('missing entries or tasks')
    expected = {}
    for entry in entries:
        if entry.get('kind') != 'vod':
            continue
        task = entry.get('channel_id')
        identity = (task, entry.get('logical_size'), entry.get('content_revision'))
        if parse_allowlist_entry({'task_id': identity[0], 'size': identity[1],
                                  'revision': identity[2]}) is None or task in expected:
            raise ValueError('invalid or duplicate VOD manifest entry')
        expected[task] = identity[1:]
    actual = {}
    for task in tasks:
        parsed = parse_allowlist_entry(task)
        if parsed is None or parsed[0] in actual:
            raise ValueError('invalid or duplicate allowlist entry')
        actual[parsed[0]] = parsed[1:]
    if expected != actual:
        raise ValueError('manifest and VOD allowlist disagree')
    return {'generation': expected_generation, 'manifest_id': expected_manifest,
            'live_count': sum(e.get('kind') == 'live' for e in entries),
            'vod_count': len(expected)}
