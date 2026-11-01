"""Variable-sized virtual media must be independently allocated and sized."""
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import build_mixed_media_template as mixed  # noqa: E402


class MixedTemplateTests(unittest.TestCase):
    def test_rejects_duplicate_paths_and_excess_capacity(self):
        with self.assertRaises(ValueError):
            mixed.validate_entries([{'path': 'Series/A/E01.mp4', 'size': 5000},
                                    {'path': 'series/a/e01.mp4', 'size': 6000}], 128 * 1024**2)
        with self.assertRaises(ValueError):
            mixed.validate_entries([{'path': 'Filmes/X.mp4', 'size': 128 * 1024**2}], 128 * 1024**2)

    @unittest.skipUnless(all(shutil.which(x) for x in ('cc', 'mkntfs', 'ntfsinfo', 'ntfsls', 'ntfscat')),
                         'NTFS tools required')
    def test_live_and_nested_vod_have_exact_different_sizes(self):
        entries = [{'path': 'Abertos/SBT.trp', 'size': 8 * 1024**2},
                   {'path': 'Series/Noiva/EP01.mp4', 'size': 5 * 1024**2 + 173},
                   {'path': 'Filmes/Filme.mp4', 'size': 6 * 1024**2 + 31}]
        directory, layout = mixed.build(entries, volume_bytes=256 * 1024**2)
        try:
            actual = {x['path'].lstrip('/'): x['logical_size'] for x in layout['files']}
            self.assertEqual(actual, {x['path']: x['size'] for x in entries})
            listing = mixed.base.run(['ntfsls', '-p', '/Series/Noiva', str(directory / 'favorites_template.bin')])
            self.assertIn('EP01.mp4', listing)
            self.assertLess(layout['host_physical_bytes'], 256 * 1024**2)
        finally:
            shutil.rmtree(directory)

    @unittest.skipUnless(all(shutil.which(x) for x in ('cc', 'mkntfs', 'ntfsinfo', 'ntfsls', 'ntfscat')),
                         'NTFS tools required')
    def test_packed_template_reproduces_every_byte_of_the_image(self):
        """The shipped .sparse.gz must unpack to the exact template image.

        Regression: mixing ``os.lseek`` on the descriptor with a buffered file
        made ``pack_sparse`` emit stale read-ahead bytes, silently dropping the
        MFT records of files beyond the first 112 inodes. The package then
        failed the candidate validator ("MFT 112 has no FILE signature") and
        could never be activated.
        """
        import gzip
        import struct

        # More than 112 files (the MFT record count of a small mkntfs image).
        entries = [{'path': f'Series/S/E{index:03d}.mp4', 'size': 4096 * (index + 1)}
                   for index in range(150)]
        directory, layout = mixed.build(entries, volume_bytes=256 * 1024**2)
        try:
            image = directory / 'favorites_template.bin'
            packed = directory / 'favorites_template.sparse.gz'
            stream = gzip.open(packed, 'rb').read()
            self.assertEqual(stream[:8], b'SPARSE01')
            total = struct.unpack_from('<Q', stream, 8)[0]
            self.assertEqual(total, image.stat().st_size)
            position = 16
            chunks = []
            while True:
                offset = struct.unpack_from('<Q', stream, position)[0]
                if offset == 0xffffffffffffffff:
                    break
                length = struct.unpack_from('<I', stream, position + 8)[0]
                chunks.append((offset, stream[position + 12:position + 12 + length]))
                position += 12 + length
            self.assertTrue(chunks)
            with image.open('rb') as source:
                for offset, payload in chunks:
                    source.seek(offset)
                    self.assertEqual(source.read(len(payload)), payload,
                                     f'packed chunk at {offset} differs from the image')
            # The MFT must be complete: every allocated inode resolves to FILE.
            import struct as _struct
            with image.open('rb') as source:
                boot = source.read(512)
                bytes_per_sector = _struct.unpack_from('<H', boot, 0x0B)[0]
                sectors_per_cluster = boot[0x0D]
                mft_lcn = _struct.unpack_from('<Q', boot, 0x30)[0]
                record = 1024
                base = mft_lcn * bytes_per_sector * sectors_per_cluster
                for inode in sorted({item['mft_inode'] for item in layout['files']}):
                    source.seek(base + inode * record)
                    self.assertEqual(source.read(4), b'FILE',
                                     f'MFT record {inode} missing from the image')
        finally:
            shutil.rmtree(directory)


if __name__ == '__main__':
    unittest.main()
