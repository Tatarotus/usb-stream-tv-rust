#!/usr/bin/env python3
"""Build the two-channel test manifest for an existing NTFS layout.

Channel IDs refer to the live catalog; URLs and credentials never enter this
file. Verify the IDs are favorites before invoking the script.
"""
import argparse
import hashlib
import json
import struct
from pathlib import Path


def file_id(channel):
    value = 0x811C9DC5
    for byte in b"live\0" + channel.encode("utf-8"):
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    return value


def manifest(layout, sbt, record):
    selected = {"/Favoritos/Abertos/SBT.trp": sbt, "/Favoritos/Abertos/Record.trp": record}
    if not all(value.isascii() and value.isdecimal() for value in selected.values()):
        raise ValueError("channel IDs must be numeric Live stream IDs")
    if sbt == record or {f["path"] for f in layout["files"]} != set(selected):
        raise ValueError("layout or channel IDs do not match the two-channel test")
    entries = []
    for item in layout["files"]:
        channel = selected[item["path"]]
        identifier = file_id(channel)
        entries.append(dict(file_id=identifier, channel_id=channel, namespace="live", kind="live",
                            folder=["Favoritos", "Abertos"], filename=Path(item["path"]).name,
                            logical_size=item["logical_size"], bootstrap_id=f"bootstrap-{identifier:08x}"))
    entries.sort(key=lambda item: item["file_id"])
    if entries[0]["file_id"] == entries[1]["file_id"]:
        raise ValueError("file ID collision")
    result = dict(schema_version=1, layout_version=1, generation=1, entries=entries)
    encoded = bytearray(b"tv-live-favorites-manifest\0")
    u32 = lambda n: encoded.extend(struct.pack("<I", n))
    u64 = lambda n: encoded.extend(struct.pack("<Q", n))
    def string(value):
        data = value.encode("utf-8")
        u32(len(data))
        encoded.extend(data)
    u32(1); u32(1); u64(1); u32(len(entries))
    for item in entries:
        u32(item["file_id"]); string(item["namespace"]); string(item["channel_id"])
        encoded.append(1)
        u32(len(item["folder"]))
        for component in item["folder"]:
            string(component)
        string(item["filename"]); u64(item["logical_size"]); string(item["bootstrap_id"])
    result["manifest_id"] = hashlib.sha256(encoded).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("layout", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--sbt", required=True)
    parser.add_argument("--record", required=True)
    args = parser.parse_args()
    result = manifest(json.loads(args.layout.read_text()), args.sbt, args.record)
    with args.output.open("x") as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
        out.write("\n")
    print(result["manifest_id"])


if __name__ == "__main__":
    main()
