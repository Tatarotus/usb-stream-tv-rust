#!/usr/bin/env python3
"""Build a real, sparse-host NTFS grade image without writing Live payload.

Only newly created scratch/live-favorites-template-* directories are accepted.
The NTFS DATA runs are allocated (never NTFS sparse holes), while the outer
image remains a sparse host file. This is offline metadata preparation only.

The tree is supplied explicitly so the same generator serves the two-channel
physical baseline and the full grade. Category directories live at the volume
root; no intermediate `Favoritos` directory is created for the grade.
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
# O runtime real do tablet usa uma imagem de 256 GiB (medido: 274877906944 B em
# /data/local/tmp/live-favorites/*/mnt/tv_stream.img) e a grade ativa tem 40 canais
# Live (~149 GiB). Com o teto em 128 GiB este validador recusava uma grade que o
# próprio sistema já serve - o build falhava com "capacity exceeded" e a mensagem
# era confundida com falta de disco no host. O teto passa a acompanhar o runtime.
DEFAULT_VOLUME_BYTES = 256 * 1024**3
DEFAULT_FILE_BYTES = 4_000_000_000
MAX_PHYSICAL_BYTES = 256 * 1024**2
MAX_FILES = 32
MAX_PATH_COMPONENT = 200

# Drop-in replacements keep the historical two-file baseline tree.
MVP_PATHS = ("Favoritos/Abertos/SBT.trp", "Favoritos/Abertos/Record.trp")

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

#define MAX_DEPTH 4

static void die(const char *where) { perror(where); exit(1); }

static void encode_name(const char *name, ntfschar *unicode, int *length) {
    size_t n = strlen(name);
    if (n == 0 || n >= 64) { errno = EINVAL; die("name length"); }
    for (size_t i = 0; i < n; i++) unicode[i] = cpu_to_le16((u16)(unsigned char)name[i]);
    *length = (int)n;
}

typedef struct { char path[256]; ntfs_inode *inode; } DirCache;
static DirCache cache[64];
static int cache_len = 0;

static ntfs_inode *dir_for(ntfs_volume *vol, ntfs_inode *root, const char *dirpath) {
    for (int i = 0; i < cache_len; i++) {
        if (strcmp(cache[i].path, dirpath) == 0) return cache[i].inode;
    }
    char parent[256];
    snprintf(parent, sizeof(parent), "%s", dirpath);
    char *slash = strrchr(parent, '/');
    ntfs_inode *parent_inode = root;
    if (slash) {
        *slash = 0;
        parent_inode = dir_for(vol, root, parent);
    }
    const char *name = slash ? slash + 1 : dirpath;
    ntfschar unicode[64];
    int length = 0;
    encode_name(name, unicode, &length);
    ntfs_inode *child = ntfs_create(parent_inode, const_cpu_to_le32(0),
                                    unicode, (u8)length, S_IFDIR);
    if (!child) die("ntfs_create directory");
    if (cache_len >= 64) { errno = EINVAL; die("directory cache overflow"); }
    snprintf(cache[cache_len].path, sizeof(cache[cache_len].path), "%s", dirpath);
    cache[cache_len].inode = child;
    cache_len++;
    return child;
}

static void make_file(ntfs_inode *dir, const char *name, s64 length,
                      const char *path, int comma) {
    ntfschar unicode[64];
    int name_len = 0;
    encode_name(name, unicode, &name_len);
    ntfs_inode *ni = ntfs_create(dir, const_cpu_to_le32(0), unicode, (u8)name_len, S_IFREG);
    if (!ni) die("ntfs_create file");
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
    if (covered * (s64)ni->vol->cluster_size < length) { errno = EINVAL; die("run coverage"); }
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
        if (ctx->attr->non_resident) { errno = EINVAL; die("resident FILE_NAME"); }
        FILE_NAME_ATTR *fn = (FILE_NAME_ATTR *)((u8 *)ctx->attr + le16_to_cpu(ctx->attr->value_offset));
        fn->data_size = cpu_to_sle64(length);
        fn->allocated_size = cpu_to_sle64(na->allocated_size);
        NInoSetDirty(ctx->ntfs_ino);
    }
    if (errno != ENOENT) die("walk FILE_NAME");
    ntfs_attr_put_search_ctx(ctx);
    printf("%s{\"path\":\"/%s\",\"mft_inode\":%" PRIu64
           ",\"logical_size\":%" PRId64 ",\"initialized_size\":%" PRId64 ",\"runs\":[",
           comma ? "," : "", path, (uint64_t)ni->mft_no, (int64_t)length,
           (int64_t)na->initialized_size);
    int first = 1;
    for (runlist_element *run = na->rl; run->length; run++) {
        printf("%s{\"vcn\":%" PRId64 ",\"lcn\":%" PRId64 ",\"clusters\":%" PRId64 "}",
            first ? "" : ",", (int64_t)run->vcn, (int64_t)run->lcn, (int64_t)run->length);
        first = 0;
    }
    printf("]}");
    ntfs_attr_close(na);
    if (ntfs_inode_close_in_dir(ni, dir)) die("close file/index sync");
}

int main(int argc, char **argv) {
    if (argc < 4) return 2;
    char *end = NULL;
    s64 length = strtoll(argv[2], &end, 10);
    if (!end || *end || length < 4096) return 2;
    ntfs_volume *vol = ntfs_mount(argv[1], NTFS_MNT_EXCLUSIVE);
    if (!vol) die("ntfs_mount");
    ntfs_inode *root = ntfs_inode_open(vol, FILE_root);
    if (!root) die("root");
    printf("{\"cluster_size\":%u,\"files\":[", vol->cluster_size);
    for (int arg = 3; arg < argc; arg++) {
        char *copy = strdup(argv[arg]);
        if (!copy) die("strdup");
        char *last = strrchr(copy, '/');
        if (!last) { errno = EINVAL; die("path needs directory and file"); }
        *last = 0;
        if (strchr(copy, '/') || *copy == 0) { errno = EINVAL; die("single category level"); }
        ntfs_inode *dir = dir_for(vol, root, copy);
        make_file(dir, last + 1, length, argv[arg], arg == 3 ? 0 : 1);
        free(copy);
    }
    printf("]}\n");
    while (cache_len > 0) {
        cache_len--;
        char parent[256];
        snprintf(parent, sizeof(parent), "%s", cache[cache_len].path);
        char *slash = strrchr(parent, '/');
        ntfs_inode *parent_inode = root;
        if (slash) {
            *slash = 0;
            for (int i = 0; i < cache_len; i++) {
                if (strcmp(cache[i].path, parent) == 0) { parent_inode = cache[i].inode; break; }
            }
        }
        if (ntfs_inode_close_in_dir(cache[cache_len].inode, parent_inode)) die("close directory");
    }
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


def validate_paths(paths):
    if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= MAX_FILES:
        raise ValueError("File list outside the supported limit")
    cleaned = []
    for path in paths:
        if not isinstance(path, str) or path.startswith("/") or "\\" in path:
            raise ValueError("Invalid relative NTFS path")
        components = path.split("/")
        if not 2 <= len(components) <= 3:
            raise ValueError("Grade files live in one category directory")
        for component in components:
            if not component or component in (".", "..") or component.startswith("."):
                raise ValueError("Invalid path component")
            if not component.isascii():
                raise ValueError("NTFS names must be ASCII for the 2013 firmware")
            if len(component.encode("utf-16-le")) > MAX_PATH_COMPONENT:
                raise ValueError("NTFS name too long")
            if set(component) & set('<>:"/\\|?*'):
                raise ValueError("Reserved NTFS character")
        if not components[-1].lower().endswith(".trp"):
            raise ValueError("Grade files must use .trp")
        cleaned.append("/".join(components))
    if len(set(path.lower() for path in cleaned)) != len(cleaned):
        raise ValueError("Duplicate case-insensitive NTFS path")
    return cleaned


def validate_map(data, volume_bytes, file_bytes, expected_paths):
    cluster_size = data["cluster_size"]
    if cluster_size != 4096 or len(data["files"]) != len(expected_paths):
        raise ValueError("Unexpected NTFS cluster geometry or file count")
    if {item["path"] for item in data["files"]} != {"/" + path for path in expected_paths}:
        raise ValueError("Layout paths do not match the requested tree")
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
            physical.append((start, end, item["path"]))
            covered += run["clusters"]
        if covered * cluster_size < file_bytes:
            raise ValueError("Incomplete runlist")
    physical.sort()
    for left, right in zip(physical, physical[1:]):
        if left[1] > right[0]:
            raise ValueError(f"Overlapping DATA allocation: {left[2]} / {right[2]}")
    if len({item["mft_inode"] for item in data["files"]}) != len(data["files"]):
        raise ValueError("Duplicate MFT record numbers")


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


def _data_segments(fd, size):
    """Host data extents, read through a raw fd (never a buffered object).

    Mixing ``os.lseek`` on a file descriptor with a buffered Python object makes
    ``read()`` serve stale read-ahead bytes from the wrong offset, which
    silently corrupts the packed metadata. All extent walking therefore uses
    the raw descriptor only.
    """
    segments = []
    position = 0
    while position < size:
        try:
            start = os.lseek(fd, position, os.SEEK_DATA)
        except OSError as error:
            if error.errno == errno.ENXIO:
                break
            raise
        end = min(os.lseek(fd, start, os.SEEK_HOLE), size)
        if end <= start:
            break
        segments.append((start, end))
        position = end
    return segments


def pack_sparse(image, output):
    """SPARSE01 format already understood by the project's sparse_unpack."""
    total_size = image.stat().st_size
    fd = os.open(image, os.O_RDONLY)
    try:
        segments = _data_segments(fd, total_size)
        with output.open("xb") as target:
            with gzip.GzipFile(filename="", mode="wb", fileobj=target, compresslevel=9, mtime=0) as compressed:
                compressed.write(b"SPARSE01" + struct.pack("<Q", total_size))
                for start, end in segments:
                    position = start
                    while position < end:
                        count = min(1024**2, end - position)
                        block = os.pread(fd, count, position)
                        if len(block) != count:
                            raise RuntimeError("Unexpected EOF while packing metadata")
                        compressed.write(struct.pack("<QI", position, count))
                        compressed.write(block)
                        position += count
                compressed.write(struct.pack("<Q", 0xffffffffffffffff))
    finally:
        os.close(fd)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def verify_data_zero(image, data):
    """Read only allocated host intersections; host holes are guaranteed zero."""
    size = image.stat().st_size
    fd = os.open(image, os.O_RDONLY)
    try:
        segments = _data_segments(fd, size)
        for item in data["files"]:
            for run in item["runs"]:
                lo = run["lcn"] * data["cluster_size"]
                hi = lo + run["clusters"] * data["cluster_size"]
                for start, end in segments:
                    left, right = max(lo, start), min(hi, end)
                    while left < right:
                        count = min(1024**2, right - left)
                        block = os.pread(fd, count, left)
                        if len(block) != count or any(block):
                            raise ValueError("Allocated DATA contains nonzero host bytes")
                        left += count
    finally:
        os.close(fd)


def build(paths, output_dir=None, volume_bytes=DEFAULT_VOLUME_BYTES,
          file_bytes=DEFAULT_FILE_BYTES):
    paths = validate_paths(paths)
    if volume_bytes < 128 * 1024**2 or volume_bytes > DEFAULT_VOLUME_BYTES or volume_bytes % 4096:
        raise ValueError("Volume must be cluster-aligned and between 128 MiB and 128 GiB")
    reserve = min(1024**3, volume_bytes // 8)
    if file_bytes < 4096 or file_bytes * len(paths) > volume_bytes - reserve:
        raise ValueError("Grade files exceed safe volume capacity")
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
    run(["cc", "-Wall", "-Wextra", "-Werror", "-O2", "-x", "c", "-", "-lntfs-3g", "-o", str(allocator)],
        input=ALLOCATOR_C)
    result = json.loads(run([str(allocator), str(image), str(file_bytes)] + paths))
    assert_physical_budget(image)
    validate_map(result, volume_bytes, file_bytes, paths)
    bitmap = subprocess.run(["ntfscat", str(image), "/$Bitmap"], check=True, capture_output=True).stdout
    validate_bitmap(bitmap, result)
    verify_data_zero(image, result)
    for item in result["files"]:
        info = run(["ntfsinfo", "-v", "-F", item["path"], str(image)])
        if "<HOLE>" in info or "SPARSE_FILE" in info:
            raise RuntimeError("Independent NTFS reader detected sparse DATA")
        safe = item["path"].strip("/").replace("/", "__")
        (directory / (safe + ".ntfsinfo.txt")).write_text(info)
    directories = sorted({"/" + path.rsplit("/", 1)[0] for path in paths})
    listing_all = []
    for directory_path in directories:
        listing = run(["ntfsls", "-p", directory_path, str(image)])
        listing_all.append(f"== {directory_path}\n{listing}")
        for path in paths:
            if "/" + path.rsplit("/", 1)[0] == directory_path:
                if path.rsplit("/", 1)[1] not in listing:
                    raise RuntimeError(f"Independent reader did not enumerate {path}")
        directory_info = run(["ntfsinfo", "-v", "-F", directory_path, str(image)])
        safe_dir = directory_path.strip("/").replace("/", "__")
        (directory / (safe_dir + ".ntfsinfo.txt")).write_text(directory_info)
    (directory / "ntfsls.txt").write_text("\n".join(listing_all))
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
    parser.add_argument("--path", action="append",
                        help="relative NTFS path such as Abertos/SBT.trp; repeatable")
    parser.add_argument("--mvp", action="store_true",
                        help="build the historical two-channel Favoritos/Abertos tree")
    args = parser.parse_args()
    paths = list(args.path or ())
    if args.mvp:
        paths = list(MVP_PATHS)
    if not paths:
        parser.error("provide --path or --mvp")
    try:
        directory, result = build(paths, args.output_dir, args.volume_bytes, args.file_bytes)
    except subprocess.CalledProcessError as error:
        parser.exit(1, (error.stderr or str(error)) + "\n")
    except (ValueError, RuntimeError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps({"directory": str(directory), "physical_bytes": result["host_physical_bytes"],
                      "volume_bytes": result["volume_bytes"], "files": len(result["files"])}, indent=2))


if __name__ == "__main__":
    main()
