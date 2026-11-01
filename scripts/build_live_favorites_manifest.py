#!/usr/bin/env python3
"""Build the canonical favorites manifest for an existing NTFS layout.

The reviewed export configuration is the single source of identity, folder,
filename and chosen source. Channel IDs refer to the Live catalog; URLs and
credentials never enter this file. The canonical bytes must stay byte-compatible
with `crates/fuse_ntfs/src/favorites/manifest.rs`.
"""
import argparse
import hashlib
import json
import re
import struct
from pathlib import Path

MAX_ENTRIES = 32


def file_id(channel):
    value = 0x811C9DC5
    for byte in b"live\0" + channel.encode("utf-8"):
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    return value


def _canonical(entries, generation, schema_version=1, layout_version=1):
    encoded = bytearray(b"tv-live-favorites-manifest\0")

    def u32(value):
        encoded.extend(struct.pack("<I", value))

    def u64(value):
        encoded.extend(struct.pack("<Q", value))

    def string(value):
        data = value.encode("utf-8")
        u32(len(data))
        encoded.extend(data)

    entries = sorted(entries, key=lambda item: (
        item["file_id"], item["namespace"], item["channel_id"],
        item["folder"], item["filename"],
    ))
    u32(schema_version)
    u32(layout_version)
    u64(generation)
    u32(len(entries))
    for item in entries:
        if item["kind"] not in ("live", "vod"):
            raise ValueError("manifest contains unsupported entry")
        u32(item["file_id"])
        string(item["namespace"])
        string(item["channel_id"])
        encoded.append(1 if item["kind"] == "live" else 2)
        u32(len(item["folder"]))
        for component in item["folder"]:
            string(component)
        string(item["filename"])
        u64(item["logical_size"])
        string(item["bootstrap_id"])
    return bytes(encoded), entries


def build(layout, config):
    if config.get("schema_version") != 1:
        raise ValueError("unsupported export config schema")
    generation = int(config.get("generation", 0))
    if generation <= 0:
        raise ValueError("generation must be positive")
    identities = config.get("identities")
    if not isinstance(identities, list) or not 1 <= len(identities) <= MAX_ENTRIES:
        raise ValueError("export identity count outside the supported limit")
    approved = {}
    for item in identities:
        if item.get("status") != "approved":
            continue
        chosen = item.get("chosen") or {}
        channel = str(chosen.get("channel_id", ""))
        if not re.fullmatch(r"[1-9][0-9]{0,19}", channel):
            raise ValueError("approved identity has no numeric chosen candidate")
        path = f"{item['category']}/{item['filename']}"
        if path in approved:
            raise ValueError("duplicate approved NTFS path")
        approved[path] = (channel, item)
    if not approved:
        raise ValueError("export config has no approved identity")
    layout_paths = {item["path"].lstrip("/"): item for item in layout.get("files", [])}
    if set(layout_paths) != set(approved):
        missing = sorted(set(approved) - set(layout_paths))
        extra = sorted(set(layout_paths) - set(approved))
        raise ValueError(f"layout/export mismatch missing={missing} extra={extra}")
    entries = []
    for path, (channel, item) in approved.items():
        layout_file = layout_paths[path]
        identifier = file_id(channel)
        folder = path.split("/")[:-1]
        filename = path.split("/")[-1]
        entries.append(dict(
            file_id=identifier, channel_id=channel, namespace="live", kind="live",
            folder=folder, filename=filename,
            logical_size=layout_file["logical_size"],
            bootstrap_id=f"bootstrap-{identifier:08x}",
        ))
    ids = [entry["file_id"] for entry in entries]
    if len(set(ids)) != len(ids):
        raise ValueError("file ID collision")
    encoded, ordered = _canonical(entries, generation)
    return dict(schema_version=1, layout_version=1, generation=generation,
                entries=ordered, manifest_id=hashlib.sha256(encoded).hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout", type=Path, required=True)
    parser.add_argument("--export", type=Path, required=True, dest="export_config")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(json.loads(args.layout.read_text()),
                   json.loads(args.export_config.read_text()))
    with args.output.open("x") as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
        out.write("\n")
    print(result["manifest_id"])


if __name__ == "__main__":
    main()
