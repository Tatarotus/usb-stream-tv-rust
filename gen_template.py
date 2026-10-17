#!/usr/bin/env python3
"""gen_template.py — builds fat_template.bin for fuse_direct.
Contains ONLY static metadata sectors (boot, FSInfo, root dir); all FAT
sectors and file data are synthesized/served live by the daemon.
Geometry MUST match fuse_direct constants:
  reserved=32, fats=2, spf=8192, root cluster 2, file cluster 3, spc=8.

Supports:
- Single-file mode: build_fat_template(file_name, file_size, out_path)
- Multi-file playlist mode: build_fat_template_multi(files, out_path)
  where files = [(name1, size1), (name2, size2), ...]
"""
import os
import re
import sys
import struct
import argparse

BPS = 512
SPC = 8
RSV = 32
FATS = 2
SPF = 8192
DATA_SEC = RSV + FATS * SPF          # 16416: cluster 2 (root dir)
FILE_SEC = DATA_SEC + SPC            # 16424: cluster 3 (file data)
DEFAULT_FILE_SIZE = 1800000000       # 1.8 GB
TOTAL_CLUS = 1048576                 # 4.0 GB virtual disk

def make_sfn(name):
    """Generate a clean 11-byte 8.3 Short File Name."""
    base, ext = os.path.splitext(name)
    ext = ext.lstrip('.').upper()[:3]
    base_clean = re.sub(r'[^A-Z0-9]', '', base.upper())
    if not base_clean:
        base_clean = "FILE"
    if len(base_clean) > 6:
        base_sfn = base_clean[:6] + "~1"
    else:
        base_sfn = f"{base_clean:<8}"[:8]
    ext_sfn = f"{ext:<3}"[:3]
    sfn_bytes = f"{base_sfn:<8}{ext_sfn}".encode('ascii')[:11]
    return sfn_bytes

def make_unique_sfn(name, existing_sfns):
    """Generate a unique 11-byte 8.3 Short File Name avoiding collisions in directory."""
    base, ext = os.path.splitext(name)
    ext = ext.lstrip('.').upper()[:3]
    base_clean = re.sub(r'[^A-Z0-9]', '', base.upper())
    if not base_clean:
        base_clean = "FILE"
    
    seq = 1
    while True:
        if seq == 1 and len(base_clean) <= 8 and not any(c in base for c in " -_"):
            candidate = f"{base_clean:<8}"[:8]
        else:
            seq_str = f"~{seq}"
            max_base = 8 - len(seq_str)
            candidate = (base_clean[:max_base] + seq_str)[:8]
        
        sfn_bytes = f"{candidate:<8}{ext:<3}".encode('ascii')[:11]
        if sfn_bytes not in existing_sfns:
            existing_sfns.add(sfn_bytes)
            return sfn_bytes
        seq += 1

def build_fat_template_multi(files, out_path="fat_template.bin", volume_label="LIVETV"):
    """
    Builds a FAT32 template for multiple files (e.g. series episodes).
    files: list of (file_name, file_size) tuples
    Returns: (bytes of template, list of layout dicts)
    """
    if not files:
        files = [("TV AO VIVO.ts", DEFAULT_FILE_SIZE)]
        
    out = {}
    total_sec = DATA_SEC + (TOTAL_CLUS - 2) * SPC
    
    # 1. Sector 0: Boot Sector
    boot = bytearray(512)
    boot[0:3] = b'\xEB\x58\x90'
    boot[3:11] = b'mkfs.fat'
    struct.pack_into('<H', boot, 11, BPS)
    boot[13] = SPC
    struct.pack_into('<H', boot, 14, RSV)
    boot[16] = FATS
    boot[21] = 0xF8
    struct.pack_into('<H', boot, 24, 63)
    struct.pack_into('<H', boot, 26, 255)
    struct.pack_into('<I', boot, 32, total_sec)
    struct.pack_into('<I', boot, 36, SPF)
    struct.pack_into('<I', boot, 44, 2)  # root cluster
    struct.pack_into('<H', boot, 48, 1)  # fsinfo sector
    struct.pack_into('<H', boot, 50, 6)  # backup boot sector
    boot[64] = 0x80
    boot[66] = 0x29
    struct.pack_into('<I', boot, 67, 0x12345678)
    boot[71:82] = f"{volume_label:<11}".encode('ascii')[:11]
    boot[82:90] = b'FAT32   '
    boot[510] = 0x55
    boot[511] = 0xAA
    out[0] = bytes(boot)
    out[6] = bytes(boot)
    
    # 2. Layout files
    existing_sfns = set()
    file_layouts = []
    cur_clus = 3
    for name, sz in files:
        sz = int(sz)
        if sz > 0xFFFFFFFF:
            raise ValueError(f"File size {sz} for '{name}' exceeds 32-bit FAT32 limit (4GB)")
        n_clus = (sz + SPC * BPS - 1) // (SPC * BPS)
        sfn = make_unique_sfn(name, existing_sfns)
        file_layouts.append({
            "name": name,
            "size": sz,
            "sfn": sfn,
            "start_clus": cur_clus,
            "end_clus": cur_clus + n_clus - 1 if n_clus > 0 else cur_clus,
            "n_clus": n_clus
        })
        cur_clus += n_clus
        
    total_file_clus = cur_clus - 3
    if total_file_clus > (TOTAL_CLUS - 2):
        raise ValueError(f"Total size of all files ({total_file_clus * 4096} bytes) exceeds 4GB volume")
        
    free_clus = (TOTAL_CLUS - 2) - 1 - total_file_clus
    
    # 3. Sector 1: FSInfo Sector
    fsinfo = bytearray(512)
    fsinfo[0:4] = b'RRaA'
    fsinfo[484:488] = b'rrAa'
    struct.pack_into('<I', fsinfo, 488, max(0, free_clus))
    struct.pack_into('<I', fsinfo, 492, cur_clus)
    fsinfo[508:512] = b'\x00\x00\x55\xAA'
    out[1] = bytes(fsinfo)
    
    # 4. Cluster 2: Root Directory Sectors (DATA_SEC .. DATA_SEC + SPC - 1)
    root = bytearray(SPC * BPS)
    vol = bytearray(32)
    vol[0:11] = f"{volume_label:<11}".encode('ascii')[:11]
    vol[11] = 0x08
    root[0:32] = vol
    
    off = 32
    for f in file_layouts:
        short_sfn = f["sfn"]
        name = f["name"]
        sz = f["size"]
        start_clus = f["start_clus"]
        
        # Calculate LFN Checksum
        chksum = 0
        for byte in short_sfn:
            chksum = (((chksum & 1) << 7) | ((chksum & 0xfe) >> 1)) + byte
            chksum &= 0xff
            
        lfn = list(name) + ['\x00']
        while len(lfn) % 13:
            lfn.append('\xff')
        n_lfn = len(lfn) // 13
        
        entries_needed = n_lfn + 1
        if off + entries_needed * 32 > len(root):
            raise ValueError(f"Root directory overflow: cannot fit {name} in Cluster 2")
            
        for seq in range(n_lfn, 0, -1):
            e = bytearray(32)
            sb = seq | (0x40 if seq == n_lfn else 0)
            e[0] = sb
            chunk_chars = lfn[(seq - 1) * 13:seq * 13]
            cb = ''.join(chunk_chars).encode('utf-16le')
            e[1:11] = cb[0:10]
            e[11] = 0x0F  # LFN attribute
            e[12] = 0x00
            e[13] = chksum
            e[14:26] = cb[10:22]
            e[26:28] = b'\x00\x00'
            e[28:32] = cb[22:26]
            root[off:off + 32] = e
            off += 32
            
        sfn = bytearray(32)
        sfn[0:11] = short_sfn
        sfn[11] = 0x20  # Archive
        struct.pack_into('<H', sfn, 14, 0x6000)  # create time
        struct.pack_into('<H', sfn, 16, 0x524F)  # create date
        struct.pack_into('<H', sfn, 18, 0x524F)  # access date
        struct.pack_into('<H', sfn, 20, (start_clus >> 16) & 0xFFFF)
        struct.pack_into('<H', sfn, 22, 0x6000)  # modify time
        struct.pack_into('<H', sfn, 24, 0x524F)  # modify date
        struct.pack_into('<H', sfn, 26, start_clus & 0xFFFF)
        struct.pack_into('<I', sfn, 28, sz)
        root[off:off + 32] = sfn
        off += 32
        
    for i in range(DATA_SEC, DATA_SEC + SPC):
        out[i] = bytes(root[(i - DATA_SEC) * BPS:(i - DATA_SEC + 1) * BPS])
        
    buf = bytearray()
    for sec in sorted(out):
        assert len(out[sec]) == BPS, sec
        buf.extend(struct.pack('<I', sec) + out[sec])
        
    if out_path:
        with open(out_path, "wb") as fp:
            fp.write(buf)
            
    return bytes(buf)

def build_fat_template(file_name="TV AO VIVO.ts", file_size=DEFAULT_FILE_SIZE, out_path="fat_template.bin"):
    """Backward compatible wrapper for single file template."""
    return build_fat_template_multi([(file_name, file_size)], out_path=out_path)

def main():
    parser = argparse.ArgumentParser(description="Build fat_template.bin for USB Stream TV")
    parser.add_argument("--name", default="TV AO VIVO.ts", help="File name shown to TV")
    parser.add_argument("--size", type=int, default=DEFAULT_FILE_SIZE, help="File size in bytes")
    parser.add_argument("--out", default="fat_template.bin", help="Output file path")
    args = parser.parse_args()

    build_fat_template(file_name=args.name, file_size=args.size, out_path=args.out)
    print(f"[✓] Template gerado com sucesso: '{args.name}' ({args.size} bytes) -> {args.out}")

if __name__ == "__main__":
    main()
