#!/usr/bin/env python3
"""pack_sparse_template.py — packs an ext4 sparse disk image into a compressed sparse stream.

Format of unpacked stream:
- 8 bytes magic: b'SPARSE01'
- 8 bytes uint64: total virtual file size
- Repeat:
    - 8 bytes uint64: chunk offset
    - 4 bytes uint32: chunk length
    - bytes: chunk data
- 8 bytes uint64: 0xFFFFFFFFFFFFFFFF (end marker)

The output is compressed using gzip level 9.
"""

import sys
import os
import gzip
import struct

SPARSE_MAGIC = b"SPARSE01"
CHUNK_MAX_SIZE = 1024 * 1024 # 1 MB chunks

def pack_sparse(img_path, out_path):
    print(f"[*] Packing sparse image: {img_path} -> {out_path}")
    total_size = os.path.getsize(img_path)
    
    fd = os.open(img_path, os.O_RDONLY)
    pos = 0
    segments = []
    
    while pos < total_size:
        try:
            data_pos = os.lseek(fd, pos, os.SEEK_DATA)
        except OSError:
            break
        try:
            hole_pos = os.lseek(fd, data_pos, os.SEEK_HOLE)
        except OSError:
            hole_pos = total_size
        segments.append((data_pos, hole_pos - data_pos))
        pos = hole_pos

    total_data_bytes = sum(l for _, l in segments)
    print(f"[*] Found {len(segments)} data segments ({total_data_bytes / 1024 / 1024:.2f} MB non-zero)")

    with gzip.open(out_path, "wb", compresslevel=9) as gz:
        # Header
        gz.write(SPARSE_MAGIC)
        gz.write(struct.pack("<Q", total_size))

        chunks_written = 0
        bytes_written = 0

        for seg_off, seg_len in segments:
            cur = seg_off
            rem = seg_len
            while rem > 0:
                to_read = min(rem, CHUNK_MAX_SIZE)
                os.lseek(fd, cur, os.SEEK_SET)
                chunk_data = os.read(fd, to_read)
                if not chunk_data:
                    break
                
                # Write chunk header
                gz.write(struct.pack("<QI", cur, len(chunk_data)))
                gz.write(chunk_data)

                chunks_written += 1
                bytes_written += len(chunk_data)
                cur += len(chunk_data)
                rem -= len(chunk_data)

        # End marker
        gz.write(struct.pack("<Q", 0xFFFFFFFFFFFFFFFF))

    os.close(fd)
    out_size = os.path.getsize(out_path)
    print(f"[✓] Successfully packed {chunks_written} chunks ({bytes_written} bytes) into {out_path} ({out_size / 1024:.1f} KB)")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: pack_sparse_template.py <input.img> <output.sparse.gz>")
        sys.exit(1)
    pack_sparse(sys.argv[1], sys.argv[2])
