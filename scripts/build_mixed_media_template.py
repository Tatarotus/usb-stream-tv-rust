#!/usr/bin/env python3
"""Build an offline NTFS template with variable-size Live and VOD entries.

Only metadata is allocated; media bytes are served by the runtime. Input is a
JSON list of {path, size}; output is created under scratch, never production.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import build_live_favorites_template as base

MAX_FILES = 256
# O padrao continua 128 GiB (nenhuma geracao existente muda). O teto foi separado
# do padrao para permitir volumes maiores por escolha explicita: a TV Samsung
# PL51F4000 reconheceu um HD USB de 300 GB (relato do operador, 30/10), entao o
# limite anterior era do nosso codigo, nao do decodificador. Acima deste valor
# continua recusando, por prudencia.


def validate_entries(entries, volume_bytes):
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES:
        raise ValueError('invalid entry count')
    seen = set()
    normalized = []
    for item in entries:
        path, size = item.get('path'), item.get('size')
        if not isinstance(path, str) or path.startswith('/') or '\\' in path:
            raise ValueError('invalid path')
        parts = path.split('/')
        if not 2 <= len(parts) <= 4 or not parts[-1].lower().endswith(('.trp', '.mp4')):
            raise ValueError('invalid media path')
        for part in parts:
            if (not part or part in ('.', '..') or part.startswith('.') or not part.isascii()
                    or len(part) >= 64 or part.endswith((' ', '.'))
                    or any(ch in part for ch in '<>:"/\\|?*')
                    or any(ord(ch) < 32 for ch in part)):
                raise ValueError('invalid NTFS component')
        if len(path) >= 255 or path.casefold() in seen:
            raise ValueError('duplicate or too-long path')
        if not isinstance(size, int) or isinstance(size, bool) or size < 4096:
            raise ValueError('invalid file size')
        seen.add(path.casefold())
        normalized.append({'path': path, 'size': size})
    reserve = min(1024**3, volume_bytes // 8)
    # Account for the last partial NTFS cluster for each file.
    if sum((x['size'] + 4095) // 4096 * 4096 for x in normalized) > volume_bytes - reserve:
        raise ValueError('virtual volume capacity exceeded')
    return normalized


def allocator_source():
    source = base.ALLOCATOR_C
    replacements = [
        ('    char *end = NULL;\n    s64 length = strtoll(argv[2], &end, 10);\n    if (!end || *end || length < 4096) return 2;\n', ''),
        ('    for (int arg = 3; arg < argc; arg++) {\n        char *copy = strdup(argv[arg]);',
         '    for (int arg = 2; arg + 1 < argc; arg += 2) {\n        char *end = NULL;\n        s64 length = strtoll(argv[arg], &end, 10);\n        if (!end || *end || length < 4096) return 2;\n        char *copy = strdup(argv[arg + 1]);'),
        ('        if (strchr(copy, \'/\') || *copy == 0) { errno = EINVAL; die("single category level"); }',
         '        if (*copy == 0) { errno = EINVAL; die("empty directory"); }'),
        ('        make_file(dir, last + 1, length, argv[arg], arg == 3 ? 0 : 1);',
         '        make_file(dir, last + 1, length, argv[arg + 1], arg == 2 ? 0 : 1);'),
        ('static DirCache cache[64];', 'static DirCache cache[256];'),
        ('if (cache_len >= 64)', 'if (cache_len >= 256)'),
    ]
    for old, new in replacements:
        if source.count(old) != 1:
            raise RuntimeError('allocator source changed; refusing unsafe transformation')
        source = source.replace(old, new)
    return source


MAX_VOLUME_BYTES = 512 * 1024**3


def build(entries, volume_bytes=base.DEFAULT_VOLUME_BYTES):
    if volume_bytes < 128 * 1024**2 or volume_bytes > MAX_VOLUME_BYTES or volume_bytes % 4096:
        raise ValueError('invalid volume size')
    entries = validate_entries(entries, volume_bytes)
    for command in ('cc', 'mkntfs', 'ntfsinfo', 'ntfsls', 'ntfscat'):
        if not shutil.which(command):
            raise RuntimeError(f'missing {command}')
    base.SCRATCH.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='mixed-media-template-', dir=base.SCRATCH)).resolve()
    try:
        image = directory / 'favorites_template.bin'
        with image.open('xb') as handle:
            handle.truncate(volume_bytes)
        subprocess.run(['mkntfs', '-F', '-Q', '-T', '-c', '4096', '-s', '512',
                        '-L', 'MIDIA', str(image)], check=True, capture_output=True)
        base.assert_physical_budget(image)
        allocator = directory / 'allocate_mixed_files'
        base.run(['cc', '-Wall', '-Wextra', '-Werror', '-O2', '-x', 'c', '-',
                  '-lntfs-3g', '-o', str(allocator)], input=allocator_source())
        # Allocate largest-first. NTFS keeps a file's runlist inside its 1 KiB
        # MFT record, and the allocator's first-fit walk leaves narrow holes
        # behind: a large file created late is scattered across many runs and
        # its runlist no longer fits the record, which fails template startup
        # with "MFT <n> runlist mismatch". Measured: at 5 files the median was
        # 2 runs but one 2.68 GB video needed 144 (a 990-byte runlist, past the
        # ~832 bytes a 1 KiB record can hold). Placing the big files first keeps
        # their extents contiguous; the order below only affects fragmentation,
        # because the manifest and the extent map are both sorted later.
        args = [str(allocator), str(image)]
        for item in sorted(entries, key=lambda entry: entry['size'], reverse=True):
            args.extend((str(item['size']), item['path']))
        layout = json.loads(base.run(args))
        paths = {('/' + item['path']): item['size'] for item in entries}
        if layout['cluster_size'] != 4096 or len(layout['files']) != len(paths):
            raise ValueError('invalid geometry or file count')
        all_ranges = []
        for item in layout['files']:
            size = paths.pop(item['path'])
            if item['logical_size'] != size or item['initialized_size'] != size:
                raise ValueError('incorrect file size')
            covered = 0
            for run in item['runs']:
                if run['vcn'] != covered or run['lcn'] < 0 or run['clusters'] <= 0:
                    raise ValueError('sparse or discontinuous file')
                start = run['lcn'] * 4096
                end = start + run['clusters'] * 4096
                if start < 4096 or end > volume_bytes:
                    raise ValueError('extent outside volume')
                all_ranges.append((start, end))
                covered += run['clusters']
            if covered * 4096 < size:
                raise ValueError('incomplete file allocation')
        if paths or len({item['mft_inode'] for item in layout['files']}) != len(entries):
            raise ValueError('missing paths or duplicate inode')
        all_ranges.sort()
        if any(a[1] > b[0] for a, b in zip(all_ranges, all_ranges[1:])):
            raise ValueError('overlapping extents')
        bitmap = subprocess.run(['ntfscat', str(image), '/$Bitmap'], check=True,
                                capture_output=True).stdout
        base.validate_bitmap(bitmap, layout)
        base.verify_data_zero(image, layout)
        for item in layout['files']:
            info = base.run(['ntfsinfo', '-v', '-F', item['path'], str(image)])
            if '<HOLE>' in info or 'SPARSE_FILE' in info:
                raise ValueError('NTFS reported sparse media file')
        layout.update(schema_version=1, volume_bytes=volume_bytes,
                      host_physical_bytes=base.assert_physical_budget(image))
        layout_path = directory / 'favorites_layout.json'
        layout_path.write_text(json.dumps(layout, indent=2) + '\n')
        packed = directory / 'favorites_template.sparse.gz'
        packed_hash = base.pack_sparse(image, packed)
        package = {'schema_version': 1, 'template_sparse_sha256': packed_hash,
                   'template_sparse_bytes': packed.stat().st_size,
                   'layout_sha256': hashlib.sha256(layout_path.read_bytes()).hexdigest(),
                   'volume_bytes': volume_bytes, 'layout_bytes': layout_path.stat().st_size,
                   'files': [item['path'] for item in layout['files']]}
        package['package_id'] = hashlib.sha256(json.dumps(package, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        (directory / 'favorites_package.json').write_text(json.dumps(package, indent=2) + '\n')
        return directory, layout
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('entries', type=Path)
    args = parser.parse_args()
    directory, layout = build(json.loads(args.entries.read_text()))
    print(json.dumps({'directory': str(directory), 'files': len(layout['files']),
                      'volume_bytes': layout['volume_bytes']}))


if __name__ == '__main__':
    main()
