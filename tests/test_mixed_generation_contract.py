import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import mixed_generation_contract as contract
from run_live_favorites_manager import manifest_digest


class GenerationContractTests(unittest.TestCase):
    def test_matches_and_rejects_stale_or_missing_allowlist(self):
        entry = {'file_id': 1, 'namespace': 'vod', 'channel_id': 'abcdef01',
                 'kind': 'vod', 'folder': ['Filmes'], 'filename': 'Video.mp4',
                 'logical_size': 4096, 'bootstrap_id': '',
                 'content_revision': 'md5:' + 'a' * 32}
        manifest = {'schema_version': 1, 'layout_version': 1, 'generation': 2,
                    'entries': [entry]}
        manifest['manifest_id'] = manifest_digest(manifest)
        allowed = {'tasks': [{'task_id': 'abcdef01', 'size': 4096,
                              'revision': 'md5:' + 'a' * 32}]}
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'manifest.json'
            a = Path(tmp) / 'allowlist.json'
            p.write_text(json.dumps(manifest))
            a.write_text(json.dumps(allowed))
            self.assertEqual(contract.validate_generation(p, a, 2, manifest['manifest_id'])['vod_count'], 1)
            allowed['tasks'][0]['revision'] = 'md5:' + 'b' * 32
            a.write_text(json.dumps(allowed))
            with self.assertRaisesRegex(ValueError, 'disagree'):
                contract.validate_generation(p, a, 2, manifest['manifest_id'])
            a.write_text(json.dumps({'tasks': []}))
            with self.assertRaisesRegex(ValueError, 'disagree'):
                contract.validate_generation(p, a, 2, manifest['manifest_id'])


if __name__ == '__main__':
    unittest.main()
