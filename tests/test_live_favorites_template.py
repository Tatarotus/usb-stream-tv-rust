import gzip
import hashlib
import json
from pathlib import Path
import shutil
import struct
import unittest

from scripts.build_live_favorites_template import (
    SCRATCH, build, validate_bitmap, validate_map, validate_output_dir,
)


class LiveFavoritesTemplateTests(unittest.TestCase):
    def test_output_cannot_overwrite_existing_or_external_path(self):
        for path in (Path('/tmp/live-favorites-template-unowned'), SCRATCH, SCRATCH / '..' / 'live-favorites-template-x'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_output_dir(path)

    def test_bitmap_must_back_every_cluster(self):
        data = {'files': [{'runs': [{'lcn': 7, 'clusters': 12}]}]}
        validate_bitmap(bytes([0x80, 0xff, 0x07]), data)
        with self.assertRaises(ValueError):
            validate_bitmap(bytes([0x80, 0x7f, 0x07]), data)
        with self.assertRaises(ValueError):
            validate_bitmap(bytes([0x80, 0xff]), data)

    def test_map_rejects_sparse_and_overlapping_runs(self):
        base = {'cluster_size': 4096, 'files': [
            {'logical_size': 4096, 'initialized_size': 4096, 'runs': [{'vcn': 0, 'lcn': 10, 'clusters': 1}]},
            {'logical_size': 4096, 'initialized_size': 4096, 'runs': [{'vcn': 0, 'lcn': 11, 'clusters': 1}]},
        ]}
        validate_map(base, 1024**2, 4096)
        base['files'][1]['runs'][0]['lcn'] = 10
        with self.assertRaises(ValueError):
            validate_map(base, 1024**2, 4096)
        base['files'][1]['runs'][0]['lcn'] = -1
        with self.assertRaises(ValueError):
            validate_map(base, 1024**2, 4096)

    @unittest.skipUnless(all(shutil.which(p) for p in ['cc','mkntfs','ntfsinfo','ntfsls','ntfscat']), 'NTFS development tools required')
    def test_real_ntfs_and_sparse_package(self):
        directory, result = build(volume_bytes=512 * 1024**2, file_bytes=64 * 1024**2)
        try:
            self.assertLess(result['host_physical_bytes'], 32 * 1024**2)
            image = directory / 'favorites_template.bin'
            self.assertEqual(image.stat().st_size, 512 * 1024**2)
            self.assertEqual({item['path'] for item in result['files']}, {
                '/Favoritos/Abertos/SBT.trp', '/Favoritos/Abertos/Record.trp',
            })
            for item in result['files']:
                self.assertEqual(item['initialized_size'], item['logical_size'])
                info = (directory / (Path(item['path']).name + '.ntfsinfo.txt')).read_text()
                self.assertNotIn('<HOLE>', info)
                self.assertNotIn('SPARSE_FILE', info)
            package = json.loads((directory / 'favorites_package.json').read_text())
            packed = directory / 'favorites_template.sparse.gz'
            self.assertEqual(package['template_sparse_sha256'], hashlib.sha256(packed.read_bytes()).hexdigest())
            self.assertEqual(package['layout_sha256'], hashlib.sha256((directory / 'favorites_layout.json').read_bytes()).hexdigest())
            with gzip.open(packed, 'rb') as stream:
                self.assertEqual(stream.read(8), b'SPARSE01')
                self.assertEqual(struct.unpack('<Q', stream.read(8))[0], image.stat().st_size)
                chunks = []
                while True:
                    offset = struct.unpack('<Q', stream.read(8))[0]
                    if offset == 0xffffffffffffffff:
                        break
                    length = struct.unpack('<I', stream.read(4))[0]
                    data = stream.read(length)
                    self.assertEqual(len(data), length)
                    self.assertLessEqual(offset + length, image.stat().st_size)
                    chunks.append((offset, data))
            with image.open('rb') as original:
                for offset, data in chunks:
                    original.seek(offset)
                    self.assertEqual(original.read(len(data)), data)
        finally:
            # build() creates a fresh directory within this exact scratch prefix.
            self.assertEqual(directory.parent, SCRATCH.resolve())
            self.assertTrue(directory.name.startswith('live-favorites-template-'))
            shutil.rmtree(directory)


if __name__ == '__main__':
    unittest.main()
