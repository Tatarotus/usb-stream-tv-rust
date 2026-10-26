#!/usr/bin/env python3
"""Build a real, sparse-host NTFS MVP image without writing Live payload.

Only newly created scratch/live-favorites-template-* directories are accepted.
The NTFS DATA runs are allocated (never NTFS sparse holes), while the outer
image remains a sparse host file. This is offline metadata preparation only.
"""

import argparse
import errno
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
DEFAULT_VOLUME_BYTES = 128 * 1024**3
DEFAULT_FILE_BYTES = 4_000_000_000
MAX_PHYSICAL_BYTES = 256 * 1024**2

# The library allocates clusters and updates the bitmap/MFT/indexes. It does
# not initialize payload, because the final raw-disk FUSE backend supplies it.
# initialized_size is intentionally published only for this brand-new virtual
# image, after proving every DATA byte has a real allocated run.
ALLOCATOR_C = r'''
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <ntfs-3g/volume.h>
#include <ntfs-3g/dir.h>
#include <ntfs-3g/endians.h>

static void die(const char *where) { perror(where); exit(1); }
static ntfs_inode *make_child(ntfs_inode *parent, const char *name, mode_t type) {
    size_t n = strlen(name);
    ntfschar unicode[64];
    if (n == 0 || n >= 64) { errno = EINVAL; die("name"); }
    for (size_t i=0; i<n; i++) unicode[i] = cpu_to_le16((u16)(unsigned char)name[i]);
    ntfs_inode *child = ntfs_create(parent, const_cpu_to_le32(0), unicode, (u8)n, type);
    if (!child) die(name);
    return child;
}
static void make_file(ntfs_inode *dir, const char *name, s64 length, int comma) {
    ntfs_inode *ni = make_child(dir, name, S_IFREG);
    ntfs_attr *na = ntfs_attr_open(ni, AT_DATA, AT_UNNAMED, 0);
    if (!na) die("ntfs_attr_open");
    if (ntfs_attr_truncate_solid(na, length)) die("ntfs_attr_truncate_solid");
    if (!NAttrNonResident(na) || NAttrSparse(na) || NAttrCompressed(na)) {
        errno = EINVAL; die("DATA must be allocated and uncompressed");
    }
    if (ntfs_attr_map_whole_runlist(na)) die("map runlist");
    s64 covered = 0;
    for (runlist_element *run = na->rl; run->length; run++) {
        if (run->lcn < 0 || run->vcn != covered || run->length < 0) {
            errno = EINVAL; die("sparse/incomplete runlist");
        }
        covered += run->length;
    }
    if (covered * (s64)ni->vol->cluster_size < length) { errno=EINVAL; die("run coverage"); }
    ntfs_attr_search_ctx *ctx = ntfs_attr_get_search_ctx(ni, NULL);
    if (!ctx || ntfs_attr_lookup(AT_DATA, AT_UNNAMED, 0, CASE_SENSITIVE, 0, NULL, 0, ctx))
        die("lookup DATA record");
    ctx->attr->initialized_size = cpu_to_sle64(length);
    na->initialized_size = length;
    ni->data_size = length;
    ni->allocated_size = na->allocated_size;
    NInoSetDirty(ni);
    NInoFileNameSetDirty(ni);
    ntfs_attr_put_search_ctx(ctx);
    ctx = ntfs_attr_get_search_ctx(ni, NULL);
    if (!ctx) die("FILE_NAME context");
    while (!ntfs_attr_lookup(AT_FILE_NAME, NULL, 0, CASE_SENSITIVE, 0, NULL, 0, ctx)) {
        if (ctx->attr->non_resident) { errno=EINVAL; die("resident FILE_NAME"); }
        FILE_NAME_ATTR *fn = (FILE_NAME_ATTR *)((u8 *)ctx->attr + le16_to_cpu(ctx->attr->value_offset));
        fn->data_size = cpu_to_sle64(length);
        fn->allocated_size = cpu_to_sle64(na->allocated_size);
        NInoSetDirty(ctx->ntfs_ino);
    }
    if (errno != ENOENT) die("walk FILE_NAME");
    ntfs_attr_put_search_ctx(ctx);
    printf("%s{\"path\":\"/Favoritos/Abertos/%s\",\"mft_inode\":%" PRIu64
           ",\"logical_size\":%" PRId64 ",\"initialized_size\":%" PRId64 ",\"runs\":[",
           comma ? "," : "", name, (uint64_t)ni->mft_no, (int64_t)length, (int64_t)na->initialized_size);
    int first = 1;
    for (runlist_element *run=na->rl; run->length; run++) {
        printf("%s{\"vcn\":%" PRId64 ",\"lcn\":%" PRId64 ",\"clusters\":%" PRId64 "}",
            first ? "" : ",", (int64_t)run->vcn, (int64_t)run->lcn, (int64_t)run->length);
        first=0;
    }
    printf("]}");
    ntfs_attr_close(na);
    if (ntfs_inode_close_in_dir(ni, dir)) die("close file/index sync");
}
int main(int argc, char **argv) {
    if (argc != 3) return 2;
    char *end=NULL;
    s64 length = strtoll(argv[2], &end, 10);
    if (!end || *end || length < 4096) return 2;
    ntfs_volume *vol = ntfs_mount(argv[1], NTFS_MNT_EXCLUSIVE);
    if (!vol) die("ntfs_mount");
    ntfs_inode *root = ntfs_inode_open(vol, FILE_root);
    if (!root) die("root");
    ntfs_inode *favorites = make_child(root, "Favoritos", S_IFDIR);
    ntfs_inode *open = make_child(favorites, "Abertos", S_IFDIR);
    printf("{\"cluster_size\":%u,\"files\":[", vol->cluster_size);
    make_file(open, "SBT.trp", length, 0);
    make_file(open, "Record.trp", length, 1);
    printf("]}\n");
    if (ntfs_inode_close_in_dir(open, favorites)) die("close Abertos");
    if (ntfs_inode_close_in_dir(favorites, root)) die("close Favoritos");
    if (ntfs_inode_close(root)) die("close root");
    if (ntfs_umount(vol, FALSE)) die("ntfs_umount");
    return 0;
}
'''


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, capture_output=True, **kwargs).stdout


def physical_bytes(path):
    return path.stat().st_blocks * 512


def assert_physical_budget(path, budget=MAX_PHYSICAL_BYTES):
    actual = physical_bytes(path)
    if actual > budget:
        raise RuntimeError(f"Physical template budget exceeded: {actual} > {budget}")
    return actual


def validate_output_dir(path):
    path = Path(path).resolve()
    if path.parent != SCRATCH.resolve() or not path.name.startswith("live-favorites-template-"):
        raise ValueError("Output must be a new scratch/live-favorites-template-* directory")
    if path.exists():
        raise ValueError("Refusing existing output directory")
    return path


def validate_map(data, volume_bytes, file_bytes):
    cluster_size = data["cluster_size"]
    if cluster_size != 4096 or len(data["files"]) != 2:
        raise ValueError("Unexpected NTFS cluster geometry or file count")
    physical = []
    for item in data["files"]:
        if item["logical_size"] != file_bytes or item["initialized_size"] != file_bytes:
            raise ValueError("DATA size/initialized-size mismatch")
        covered = 0
        for run in item["runs"]:
            if run["vcn"] != covered or run["lcn"] < 0 or run["clusters"] <= 0:
                raise ValueError("Sparse or discontinuous logical runlist")
            start = run["lcn"] * cluster_size
            end = start + run["clusters"] * cluster_size
            if start < cluster_size or end > volume_bytes:
                raise ValueError("Run outside volume")
            physical.append((start, end))
            covered += run["clusters"]
        if covered * cluster_size < file_bytes:
            raise ValueError("Incomplete runlist")
    physical.sort()
    if any(left[1] > right[0] for left, right in zip(physical, physical[1:])):
        raise ValueError("Overlapping DATA allocation")


def validate_bitmap(bitmap, data):
    """Verify every declared DATA cluster with an independently read $Bitmap."""
    for item in data["files"]:
        for run in item["runs"]:
            start = run["lcn"]
            end = start + run["clusters"]
            if end > len(bitmap) * 8:
                raise ValueError("Cluster allocation exceeds bitmap")
            for cluster in range(start, min(end, (start + 7) // 8 * 8)):
                if not (bitmap[cluster // 8] & (1 << (cluster % 8))):
                    raise ValueError("Unallocated DATA cluster")
            first_byte = (start + 7) // 8
            last_byte = end // 8
            if any(value != 255 for value in bitmap[first_byte:last_byte]):
                raise ValueError("Unallocated DATA cluster")
            for cluster in range(max(start, last_byte * 8), end):
                if not (bitmap[cluster // 8] & (1 << (cluster % 8))):
                    raise ValueError("Unallocated DATA cluster")


def pack_sparse(image, output):
    """SPARSE01 format already understood by the project's sparse_unpack."""
    total_size = image.stat().st_size
    with image.open("rb") as source, output.open("xb") as target:
        with gzip.GzipFile(filename="", mode="wb", fileobj=target, compresslevel=9, mtime=0) as compressed:
            compressed.write(b"SPARSE01" + struct.pack("<Q", total_size))
            position = 0
            while position < total_size:
                try:
                    start = os.lseek(source.fileno(), position, os.SEEK_DATA)
                except OSError as error:
                    if error.errno == errno.ENXIO:
                        break
                    raise
                end = min(os.lseek(source.fileno(), start, os.SEEK_HOLE), total_size)
                source.seek(start)
                position = start
                while position < end:
                    block = source.read(min(1024**2, end - position))
                    if not block:
                        raise RuntimeError("Unexpected EOF while packing metadata")
                    compressed.write(struct.pack("<QI", position, len(block)))
                    compressed.write(block)
                    position += len(block)
            compressed.write(struct.pack("<Q", 0xffffffffffffffff))
    return hashlib.sha256(output.read_bytes()).hexdigest()


def verify_data_zero(image, data):
    """Read only allocated host intersections; host holes are guaranteed zero."""
    size = image.stat().st_size
    segments = []
    with image.open("rb") as source:
        position = 0
        while position < size:
            try:
                start = os.lseek(source.fileno(), position, os.SEEK_DATA)
            except OSError as error:
                if error.errno == errno.ENXIO:
                    break
                raise
            end = min(os.lseek(source.fileno(), start, os.SEEK_HOLE), size)
            segments.append((start, end))
            position = end
        for item in data["files"]:
            for run in item["runs"]:
                lo = run["lcn"] * data["cluster_size"]
                hi = lo + run["clusters"] * data["cluster_size"]
                for start, end in segments:
                    left, right = max(lo, start), min(hi, end)
                    if left >= right:
                        continue
                    source.seek(left)
                    while left < right:
                        block = source.read(min(1024**2, right - left))
                        if not block or any(block):
                            raise ValueError("Allocated DATA contains nonzero host bytes")
                        left += len(block)


def build(output_dir=None, volume_bytes=DEFAULT_VOLUME_BYTES, file_bytes=DEFAULT_FILE_BYTES):
    if volume_bytes < 128 * 1024**2 or volume_bytes > DEFAULT_VOLUME_BYTES or volume_bytes % 4096:
        raise ValueError("Volume must be cluster-aligned and between 128 MiB and 128 GiB")
    if file_bytes < 4096 or file_bytes * 2 > volume_bytes * 3 // 4:
        raise ValueError("MVP files exceed safe volume capacity")
    for program in ("cc", "mkntfs", "ntfsinfo", "ntfsls", "ntfscat"):
        if not shutil.which(program):
            raise RuntimeError(f"Missing required program: {program}")
    SCRATCH.mkdir(exist_ok=True)
    if output_dir:
        directory = validate_output_dir(output_dir)
        directory.mkdir(mode=0o700)
    else:
        directory = Path(tempfile.mkdtemp(prefix="live-favorites-template-", dir=SCRATCH)).resolve()
    image = directory / "favorites_template.bin"
    with image.open("xb") as handle:
        handle.truncate(volume_bytes)
    mkntfs = subprocess.run(["mkntfs", "-F", "-Q", "-T", "-c", "4096", "-s", "512", "-L", "FAVORITOS", str(image)],
                            check=True, text=True, capture_output=True)
    (directory / "mkntfs.log").write_text(mkntfs.stdout + mkntfs.stderr)
    assert_physical_budget(image)
    allocator = directory / "allocate_live_files"
    run(["cc", "-Wall", "-Wextra", "-Werror", "-O2", "-x", "c", "-", "-lntfs-3g", "-o", str(allocator)], input=ALLOCATOR_C)
    result = json.loads(run([str(allocator), str(image), str(file_bytes)]))
    assert_physical_budget(image)
    validate_map(result, volume_bytes, file_bytes)
    bitmap = subprocess.run(["ntfscat", str(image), "/$Bitmap"], check=True, capture_output=True).stdout
    validate_bitmap(bitmap, result)
    verify_data_zero(image, result)
    for item in result["files"]:
        info = run(["ntfsinfo", "-v", "-F", item["path"], str(image)])
        if "<HOLE>" in info or "SPARSE_FILE" in info:
            raise RuntimeError("Independent NTFS reader detected sparse DATA")
        (directory / (Path(item["path"]).name + ".ntfsinfo.txt")).write_text(info)
    listing = run(["ntfsls", "-p", "/Favoritos/Abertos", str(image)])
    if "SBT.trp" not in listing or "Record.trp" not in listing:
        raise RuntimeError("Independent reader did not enumerate both files")
    (directory / "ntfsls.txt").write_text(listing)
    # Inspect the I30 directory index as well as the per-file DATA attribute.
    directory_info = run(["ntfsinfo", "-v", "-F", "/Favoritos/Abertos", str(image)])
    (directory / "Abertos.ntfsinfo.txt").write_text(directory_info)
    result.update(schema_version=1, volume_bytes=volume_bytes,
                  host_physical_bytes=assert_physical_budget(image),
                  note="Offline virtual metadata only; no source IDs, media transport or bootstrap included")
    manifest = directory / "favorites_layout.json"
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    packed = directory / "favorites_template.sparse.gz"
    packed_hash = pack_sparse(image, packed)
    layout_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    package = {"schema_version": 1, "template_sparse_sha256": packed_hash,
               "template_sparse_bytes": packed.stat().st_size,
               "layout_sha256": layout_hash, "volume_bytes": volume_bytes,
               "layout_bytes": manifest.stat().st_size,
               "files": [item["path"] for item in result["files"]]}
    package["package_id"] = hashlib.sha256(json.dumps(package, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    (directory / "favorites_package.json").write_text(json.dumps(package, indent=2) + "\n")
    return directory, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir")
    parser.add_argument("--volume-bytes", type=int, default=DEFAULT_VOLUME_BYTES)
    parser.add_argument("--file-bytes", type=int, default=DEFAULT_FILE_BYTES)
    args = parser.parse_args()
    try:
        directory, result = build(args.output_dir, args.volume_bytes, args.file_bytes)
    except subprocess.CalledProcessError as error:
        parser.exit(1, (error.stderr or str(error)) + "\n")
    except (ValueError, RuntimeError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps({"directory": str(directory), "physical_bytes": result["host_physical_bytes"],
                      "volume_bytes": result["volume_bytes"], "files": len(result["files"])}, indent=2))


if __name__ == "__main__":
    main()
