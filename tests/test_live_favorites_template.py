import gzip
import hashlib
import json
from pathlib import Path
import shutil
import struct
import unittest

from scripts.build_live_favorites_template import (
    SCRATCH, build, validate_bitmap, validate_map, validate_output_dir, validate_paths,
)


class LiveFavoritesTemplateTests(unittest.TestCase):
    def test_output_cannot_overwrite_existing_or_external_path(self):
        for path in (Path('/tmp/live-favorites-template-unowned'), SCRATCH, SCRATCH / '..' / 'live-favorites-template-x'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_output_dir(path)

    def test_paths_must_be_root_categories_with_ascii_trp_names(self):
        validate_paths(['Abertos/SBT.trp', 'Noticias/Band News.trp'])
        for bad in (['../SBT.trp'], ['/Abertos/SBT.trp'],
                    ['Abertos/SBT.mp4'], ['Abertos/SBT.trp', 'abertos/sbt.trp'],
                    ['Noticias/CNN Brasil.trp', 'Noticias/CNN Brasil.trp'],
                    ['Abertos/..trp'], ['Abertos/SBT\u00e9.trp']):
            with self.subTest(paths=bad), self.assertRaises(ValueError):
                validate_paths(bad)

    def test_bitmap_must_back_every_cluster(self):
        data = {'files': [{'runs': [{'lcn': 7, 'clusters': 12}]}]}
        validate_bitmap(bytes([0x80, 0xff, 0x07]), data)
        with self.assertRaises(ValueError):
            validate_bitmap(bytes([0x80, 0x7f, 0x07]), data)
        with self.assertRaises(ValueError):
            validate_bitmap(bytes([0x80, 0xff]), data)

    def test_map_rejects_sparse_and_overlapping_runs(self):
        paths = ['Abertos/A.trp', 'Abertos/B.trp']
        base = {'cluster_size': 4096, 'files': [
            {'path': '/Abertos/A.trp', 'mft_inode': 66, 'logical_size': 4096,
             'initialized_size': 4096, 'runs': [{'vcn': 0, 'lcn': 10, 'clusters': 1}]},
            {'path': '/Abertos/B.trp', 'mft_inode': 67, 'logical_size': 4096,
             'initialized_size': 4096, 'runs': [{'vcn': 0, 'lcn': 11, 'clusters': 1}]},
        ]}
        validate_map(base, 1024**2, 4096, paths)
        base['files'][1]['runs'][0]['lcn'] = 10
        with self.assertRaises(ValueError):
            validate_map(base, 1024**2, 4096, paths)
        base['files'][1]['runs'][0]['lcn'] = -1
        with self.assertRaises(ValueError):
            validate_map(base, 1024**2, 4096, paths)
        base['files'][1]['path'] = '/Abertos/Other.trp'
        with self.assertRaises(ValueError):
            validate_map(base, 1024**2, 4096, paths)

    @unittest.skipUnless(all(shutil.which(p) for p in ['cc', 'mkntfs', 'ntfsinfo', 'ntfsls', 'ntfscat']), 'NTFS development tools required')
    def test_real_ntfs_and_sparse_package_at_the_volume_root(self):
        paths = ['Abertos/SBT.trp', 'Abertos/Record.trp', 'Noticias/CNN Brasil.trp']
        directory, result = build(paths, volume_bytes=512 * 1024**2, file_bytes=96 * 1024**2)
        try:
            self.assertLess(result['host_physical_bytes'], 32 * 1024**2)
            image = directory / 'favorites_template.bin'
            self.assertEqual(image.stat().st_size, 512 * 1024**2)
            self.assertEqual({item['path'] for item in result['files']},
                             {'/' + path for path in paths})
            self.assertFalse(any('Favoritos' in item['path'] for item in result['files']))
            for item in result['files']:
                self.assertEqual(item['initialized_size'], item['logical_size'])
                safe = item['path'].strip('/').replace('/', '__')
                info = (directory / (safe + '.ntfsinfo.txt')).read_text()
                self.assertNotIn('<HOLE>', info)
                self.assertNotIn('SPARSE_FILE', info)
            listing = (directory / 'ntfsls.txt').read_text()
            self.assertIn('/Abertos', listing)
            self.assertIn('/Noticias', listing)
            self.assertIn('CNN Brasil.trp', listing)
            package = json.loads((directory / 'favorites_package.json').read_text())
            packed = directory / 'favorites_template.sparse.gz'
            self.assertEqual(package['template_sparse_sha256'], hashlib.sha256(packed.read_bytes()).hexdigest())
            self.assertEqual(package['layout_sha256'], hashlib.sha256((directory / 'favorites_layout.json').read_bytes()).hexdigest())
            self.assertEqual(len(package['files']), 3)
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
            self.assertEqual(directory.parent, SCRATCH.resolve())
            self.assertTrue(directory.name.startswith('live-favorites-template-'))
            shutil.rmtree(directory)


if __name__ == '__main__':
    unittest.main()
