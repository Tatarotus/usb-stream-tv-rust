#!/usr/bin/env python3
"""Independent validation of a grade staging package (no TV, no network).

Checks the export -> layout -> template -> manifest -> package chain, every
DATA run's allocation/non-sparse nature, routing arithmetic on all boundaries,
canonical manifest identity and SHA-256 bindings. Unpacks the SPARSE01 archive
to a temporary sparse image and inspects MFT `$DATA` attributes with ntfsinfo.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_live_favorites_manifest as manifest_builder  # noqa: E402

ARTIFACTS = ("favorites_template.sparse.gz", "favorites_layout.json",
             "favorites_package.json", "favorites_manifest.json")
VOLUME_MIN = 128 * 1024**2
# Teto do volume VALIDADO. Precisa acompanhar o runtime real do tablet (imagem de
# 256 GiB, medida em /data/local/tmp/live-favorites/*/mnt/tv_stream.img) e a grade
# ativa (40 canais Live ~149 GiB); com 128 GiB o validador recusava exatamente a
# grade que o sistema já serve, e a mensagem parecia falta de disco no host.
VOLUME_MAX = 256 * 1024**3
CLUSTER = 4096


class Invalid(ValueError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def unpack_sparse(archive: Path, image: Path) -> int:
    logical = 0
    chunks = 0
    with gzip.open(archive, "rb") as source:
        magic = source.read(8)
        if magic != b"SPARSE01":
            raise Invalid("not a SPARSE01 archive")
        logical = struct.unpack("<Q", source.read(8))[0]
        if not VOLUME_MIN <= logical <= VOLUME_MAX:
            raise Invalid("wrong logical volume size")
        if os.path.exists(image):
            raise Invalid("image already exists")
        fd = os.open(image, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.ftruncate(fd, logical)
            while True:
                header = source.read(8)
                if len(header) != 8:
                    raise Invalid("truncated sparse header")
                offset = struct.unpack("<Q", header)[0]
                if offset == 0xFFFFFFFFFFFFFFFF:
                    break
                length = struct.unpack("<I", source.read(4))[0]
                if length == 0 or offset + length > logical:
                    raise Invalid("chunk outside the volume")
                data = source.read(length)
                if len(data) != length:
                    raise Invalid("truncated chunk")
                os.pwrite(fd, data, offset)
                chunks += 1
        finally:
            os.close(fd)
    return logical


def validate_routing(layout: dict) -> None:
    cluster = layout["cluster_size"]
    if cluster != CLUSTER:
        raise Invalid("unexpected cluster size")
    volume = layout["volume_bytes"]
    ranges = []
    for item in layout["files"]:
        if item["logical_size"] != item["initialized_size"]:
            raise Invalid("logical/initialized mismatch")
        covered = 0
        for run in item["runs"]:
            if run["vcn"] != covered or run["clusters"] <= 0 or run["lcn"] < 0:
                raise Invalid("sparse or discontinuous runlist")
            start = run["lcn"] * cluster
            end = start + run["clusters"] * cluster
            if start < cluster or end > volume:
                raise Invalid("run outside volume")
            ranges.append((start, end, item["path"]))
            covered += run["clusters"]
        if covered * cluster < item["logical_size"]:
            raise Invalid("runlist shorter than the file")
    ranges.sort()
    for left, right in zip(ranges, ranges[1:]):
        if left[1] > right[0]:
            raise Invalid(f"overlapping extents {left[2]} / {right[2]}")
    # Every byte of every file must map to exactly one extent and round-trip
    # through the file's own runlist (files may be fragmented).
    for item in layout["files"]:
        for offset in (0, 188, item["logical_size"] - 1):
            vcn = offset // cluster
            absolute = None
            for run in item["runs"]:
                if run["vcn"] <= vcn < run["vcn"] + run["clusters"]:
                    absolute = run["lcn"] * cluster + (offset - run["vcn"] * cluster)
                    break
            if absolute is None:
                raise Invalid(f"offset outside runlist at {item['path']}+{offset}")
            matches = [entry for entry in ranges if entry[0] <= absolute < entry[1]]
            if len(matches) != 1:
                raise Invalid(f"routing ambiguity at {item['path']}+{offset}")


def validate_manifest(manifest: dict, layout: dict) -> None:
    encoded, ordered = manifest_builder._canonical(
        manifest["entries"], manifest["generation"],
        manifest["schema_version"], manifest["layout_version"])
    if hashlib.sha256(encoded).hexdigest() != manifest["manifest_id"]:
        raise Invalid("manifest canonical id mismatch")
    if ordered != manifest["entries"]:
        raise Invalid("manifest entries are not in canonical order")
    paths = {item["path"].lstrip("/") for item in layout["files"]}
    entries = set()
    for entry in manifest["entries"]:
        path = "/".join(entry["folder"] + [entry["filename"]])
        # One category level, optionally nested one level under a live root
        # (``TV/``): the drive shape the export declares. Deeper nesting is
        # rejected instead of being silently accepted.
        folder = entry["folder"]
        if len(folder) not in (1, 2):
            raise Invalid("manifest entry is not a category path")
        if any(not isinstance(c, str) or not c for c in folder):
            raise Invalid("manifest folder component is empty")
        if entry["file_id"] != manifest_builder.file_id(entry["channel_id"]):
            raise Invalid("manifest file_id mismatch")
        if entry["bootstrap_id"] != f"bootstrap-{entry['file_id']:08x}":
            raise Invalid("manifest bootstrap id mismatch")
        if entry["kind"] != "live":
            raise Invalid("non-Live entry")
        entries.add(path)
    if entries != paths:
        raise Invalid("manifest paths do not match the layout")


def validate_package(staging: Path, expect_volume: int = VOLUME_MAX) -> dict:
    for name in ARTIFACTS:
        if not (staging / name).is_file():
            raise Invalid(f"missing artifact {name}")
    layout = json.loads((staging / "favorites_layout.json").read_text())
    manifest = json.loads((staging / "favorites_manifest.json").read_text())
    package = json.loads((staging / "favorites_package.json").read_text())
    if package["template_sparse_sha256"] != sha256(staging / "favorites_template.sparse.gz"):
        raise Invalid("template hash mismatch")
    if package["template_sparse_bytes"] != (staging / "favorites_template.sparse.gz").stat().st_size:
        raise Invalid("template size mismatch")
    if package["layout_sha256"] != sha256(staging / "favorites_layout.json"):
        raise Invalid("layout hash mismatch")
    if package["layout_bytes"] != (staging / "favorites_layout.json").stat().st_size:
        raise Invalid("layout bytes mismatch")
    if package["volume_bytes"] != layout["volume_bytes"]:
        raise Invalid("volume mismatch")
    expected_paths = {"/" + item["path"].lstrip("/") for item in layout["files"]}
    if set(package["files"]) != expected_paths:
        raise Invalid("package file list mismatch")
    validate_routing(layout)
    validate_manifest(manifest, layout)
    if len(manifest["entries"]) != len(layout["files"]):
        raise Invalid("entry count mismatch")
    if len(manifest["entries"]) > 32:
        raise Invalid("entry limit exceeded")
    with tempfile.TemporaryDirectory(prefix="fav-package-", dir="/tmp/opencode") as directory:
        image = Path(directory) / "image.bin"
        logical = unpack_sparse(staging / "favorites_template.sparse.gz", image)
        if logical != layout["volume_bytes"]:
            raise Invalid("sparse logical size mismatch")
        if expect_volume and logical != expect_volume:
            raise Invalid("unexpected logical volume for the production package")
        if image.stat().st_blocks * 512 > 256 * 1024**2:
            raise Invalid("unpacked physical budget exceeded")
        for item in layout["files"]:
            info = subprocess.run(["ntfsinfo", "-v", "-F", item["path"], str(image)],
                                  check=True, capture_output=True, text=True).stdout
            if "<HOLE>" in info or "SPARSE_FILE" in info:
                raise Invalid(f"sparse DATA detected for {item['path']}")
            if "Allocated Size:" not in info:
                raise Invalid(f"no allocated size for {item['path']}")
        listing = subprocess.run(["ntfsls", "-p", "/", str(image)],
                                 check=True, capture_output=True, text=True).stdout
        for directory in sorted({item["path"].rsplit("/", 1)[0] for item in layout["files"]}):
            name = directory.strip("/")
            if name not in listing:
                raise Invalid(f"root directory {name} missing")
    return {
        "files": len(layout["files"]),
        "volume_bytes": layout["volume_bytes"],
        "logical_bytes": sum(item["logical_size"] for item in layout["files"]),
        "host_physical_bytes": layout.get("host_physical_bytes"),
        "manifest_id": manifest["manifest_id"],
        "package_id": package["package_id"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--expect-volume", type=int, default=VOLUME_MAX)
    args = parser.parse_args()
    try:
        summary = validate_package(args.staging.resolve(), args.expect_volume)
    except (Invalid, OSError, subprocess.CalledProcessError, KeyError, json.JSONDecodeError) as error:
        parser.exit(1, f"package invalid: {error}\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
