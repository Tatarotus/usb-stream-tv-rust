#!/usr/bin/env python3
"""Build a mixed-media (Live TV + VOD) staging package for Samsung ConnectShare.

This script combines approved Live channels (.trp) and ready VOD files (.mp4)
into a single 128 GiB virtual NTFS volume where:
  - Folders are placed directly at the root (Abertos/, Series/<Nome>/, Filmes/)
  - All file sizes and extents are exact and variable
  - Media payloads are served dynamically via FUSE (never stored on disk)
  - Output contains: template.sparse.gz, layout, manifest, package, allowlist,
    SHA256SUMS, and validation summary.
  - Live channels are always published; ready VODs are best-fit selected in the
    given input order and included while they fit. A VOD that does not fit is
    never silently dropped: it is reported in the summary and in an optional
    ``omitted.json`` artifact, and never appears in the manifest or allowlist.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import build_live_favorites_template as base  # noqa: E402
import build_mixed_media_template as mixed_template  # noqa: E402
import vod_history  # noqa: E402
import vod_revision  # noqa: E402

ARTIFACTS = (
    "favorites_template.sparse.gz",
    "favorites_layout.json",
    "favorites_package.json",
    "favorites_manifest.json",
    "vod_allowlist.json",
)

# Copied with 0600: never world-readable, never committed.
PRIVATE_FILES = ("transport.token", "chisel.auth")
# Optional report written only when the volume could not hold every ready VOD.
# It is never a required artifact: an existing package built with nothing
# omitted must keep passing ``staging_is_complete`` unchanged.
OMISSION_ARTIFACT = "omitted.json"
# Files that may accompany a generation, each one covered by SHA256SUMS when it
# is actually present.
EXTRA_FILES = (("fuse_ntfs", "sparse_unpack", "activate_live_favorites_tablet.sh")
               + PRIVATE_FILES + (OMISSION_ARTIFACT,))

CLUSTER_BYTES = 4096


def align_cluster_bytes(size: int) -> int:
    """Cluster-align a logical size exactly as the NTFS allocator does."""
    return (int(size) + CLUSTER_BYTES - 1) // CLUSTER_BYTES * CLUSTER_BYTES


def fnv1a32(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def stable_file_id(namespace: str, channel_id: str) -> int:
    material = f"{namespace}\0{channel_id}".encode("utf-8")
    return fnv1a32(material)


def clean_series_name(name: str) -> str:
    name = re.sub(r"^\s*@\s*", "", str(name or "")).strip()
    if " - " in name:
        name = name.split(" - ")[0].strip()
    name = re.sub(r"\s*\[.*?\]", "", name)
    name = re.sub(r"\s*\(.*?\)", "", name)
    name = re.sub(r"\s+S\d{1,2}\s*E\d{1,3}.*$", "", name, flags=re.I)
    name = re.sub(r"\s+\d{1,2}x\d{1,3}.*$", "", name, flags=re.I)
    name = re.sub(r"[:\s]+$", "", name).strip()
    name = re.sub(r"[<>:\"/\\|?*]", "", name)
    name = " ".join(name.split())
    if len(name) > 60:
        name = name[:60].rstrip(". ")
    return name


def clean_filename(name: str, fallback_id: str) -> str:
    name = re.sub(r"^\s*@\s*", "", str(name or "")).strip()
    if name.lower().endswith(".mp4"):
        base_name = name[:-4].strip()
    elif name.lower().endswith(".trp"):
        base_name = name[:-4].strip()
    else:
        base_name = name.strip()
    base_name = re.sub(r"[<>:\"/\\|?*]", "", base_name)
    base_name = " ".join(base_name.split()).strip(". ")
    if not base_name:
        base_name = f"video_{fallback_id}"
    if len(base_name) > 59:
        base_name = base_name[:59].rstrip(". ")
    return f"{base_name}.mp4"


def canonical_manifest_bytes(entries: list[dict], generation: int,
                             schema_version: int = 1, layout_version: int = 1) -> tuple[bytes, list[dict]]:
    encoded = bytearray(b"tv-live-favorites-manifest\0")

    def u32(val: int):
        encoded.extend(struct.pack("<I", val))

    def u64(val: int):
        encoded.extend(struct.pack("<Q", val))

    def string(val: str):
        data = val.encode("utf-8")
        u32(len(data))
        encoded.extend(data)

    sorted_entries = sorted(
        entries,
        key=lambda item: (
            item["file_id"],
            item["namespace"],
            item["channel_id"],
            item["folder"],
            item["filename"],
        ),
    )
    u32(schema_version)
    u32(layout_version)
    u64(generation)
    u32(len(sorted_entries))
    for item in sorted_entries:
        kind = item["kind"]
        if kind not in ("live", "vod"):
            raise ValueError(f"unsupported entry kind: {kind}")
        u32(item["file_id"])
        string(item["namespace"])
        string(item["channel_id"])
        encoded.append(1 if kind == "live" else 2)
        u32(len(item["folder"]))
        for component in item["folder"]:
            string(component)
        string(item["filename"])
        u64(item["logical_size"])
        string(item["bootstrap_id"])
    return bytes(encoded), sorted_entries


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def process_vod_tasks(tasks: list[dict], *, return_rejected: bool = False) -> list[dict] | tuple[list[dict], list[dict]]:
    """Classify ready VOD tasks into Series and Filmes with unique paths."""
    seen_paths: set[str] = set()
    vod_entries: list[dict] = []
    rejected: list[dict] = []

    for task in tasks:
        if task.get("status") != "ready":
            continue
        task_id = str(task.get("id") or "")
        if not re.fullmatch(r"[0-9a-f]{8}", task_id):
            rejected.append({"task_id": task_id, "title": task.get("title"), "reason": "invalid_task_id_format"})
            continue
        file_size = int(task.get("file_size") or task.get("file_size_bytes") or 0)
        if file_size < 4096:
            rejected.append({"task_id": task_id, "title": task.get("title"), "reason": f"file_size_too_small:{file_size}"})
            continue

        # Trusted identity of the published bytes. A task whose content cannot
        # be verified (no MD5, no readable source) is never published: size
        # alone cannot distinguish mutated content from the original.
        revision = vod_revision.content_revision(task)
        if revision is None:
            rejected.append({"task_id": task_id, "title": task.get("title"),
                             "reason": "unverifiable_content_revision"})
            continue

        title = task.get("title") or ""
        ident = vod_history.resolve_episode_identity(task)
        series_title = task.get("series_title")
        if not series_title and ident:
            series_title = ident.get("series_title")
        if not series_title and (task.get("series_id") or re.search(r"\bS\d{1,2}\s*E\d{1,3}\b", title, re.I)):
            series_title = clean_series_name(title)
        elif series_title:
            series_title = clean_series_name(series_title)

        if series_title:
            folder = ["Series", series_title]
        else:
            folder = ["Filmes"]

        fname = clean_filename(task.get("display_name") or title, task_id)
        path = "/".join(folder + [fname])

        # Disambiguate duplicate path if necessary
        if path.lower() in seen_paths:
            base_fname = fname[:-4]
            fname = f"{base_fname[:49]} [{task_id}].mp4"
            path = "/".join(folder + [fname])

        seen_paths.add(path.lower())

        fid = stable_file_id("vod", task_id)
        vod_entries.append({
            "file_id": fid,
            "channel_id": task_id,
            "namespace": "vod",
            "kind": "vod",
            "folder": folder,
            "filename": fname,
            "path": path,
            "logical_size": file_size,
            "bootstrap_id": "",
            # JSON-only field: deliberately excluded from canonical manifest
            # bytes so the on-disk manifest format and its manifest_id stay
            # byte-identical with the Rust reader.
            "content_revision": revision,
        })

    if return_rejected:
        return vod_entries, rejected
    return vod_entries


def _safe_folder_component(value: str, field: str) -> str:
    """One NTFS-safe path component: no separators, no reserved characters.

    The TV navigates this with ConnectShare, so a component must be a plain
    name. A slash would silently create a nested level nobody asked for, and
    the characters below are rejected by NTFS itself.
    """
    name = str(value or "").strip()
    if not name:
        raise ValueError(f"empty {field}")
    if "/" in name or "\\" in name:
        raise ValueError(f"{field} must not contain a path separator: {name!r}")
    if any(ch in name for ch in ':*?"<>|'):
        raise ValueError(f"{field} contains a character NTFS rejects: {name!r}")
    if name in (".", ".."):
        raise ValueError(f"invalid {field}: {name!r}")
    if "\0" in name:
        raise ValueError(f"invalid {field}: embedded NUL")
    # NTFS counts 255 UTF-16 code units per component; a longer name is not
    # creatable, so it is refused here instead of failing during the pack.
    if len(name.encode("utf-16-le")) // 2 > 255:
        raise ValueError(f"{field} exceeds the NTFS 255-character limit")
    return name


def process_live_identities(export_config: dict, default_live_size: int = 4_000_000_000) -> list[dict]:
    """Extract approved Live channels from export configuration.

    ``live_root`` (optional) nests every Live channel under one root folder, so
    the drive reads ``TV/<categoria>/<canal>.trp`` next to ``Series/`` and
    ``Filmes/``. Without it the historical behaviour (``<categoria>/``) is kept,
    which is what already-published generations were built with.
    """
    live_entries = []
    identities = export_config.get("identities", [])
    root = str(export_config.get("live_root") or "").strip()
    root_component = _safe_folder_component(root, "live_root") if root else ""
    for item in identities:
        if item.get("status") != "approved":
            continue
        chosen = item.get("chosen") or {}
        channel_id = str(chosen.get("channel_id") or "")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", channel_id):
            raise ValueError(f"approved identity {item.get('identity')} has no valid numeric channel ID")

        category = _safe_folder_component(item.get("category", "Abertos"), "category")
        filename = str(item.get("filename") or "").strip()
        if not filename:
            raise ValueError(f"approved identity {item.get('identity')} has no filename")
        if "/" in filename or "\\" in filename:
            raise ValueError(f"filename must not contain a path separator: {filename!r}")
        if any(ch in filename for ch in ':*?"<>|'):
            raise ValueError(f"filename contains a character NTFS rejects: {filename!r}")
        if not filename.lower().endswith(".trp"):
            filename = f"{filename}.trp"
        filename = _safe_folder_component(filename, "filename")

        folder = [root_component, category] if root_component else [category]
        path = "/".join(folder + [filename])
        fid = stable_file_id("live", channel_id)

        live_entries.append({
            "file_id": fid,
            "channel_id": channel_id,
            "namespace": "live",
            "kind": "live",
            "folder": folder,
            "filename": filename,
            "path": path,
            "logical_size": default_live_size,
            "bootstrap_id": f"bootstrap-{fid:08x}",
        })

    # NTFS is case-insensitive: two channels whose paths differ only in case
    # would collide on the drive, so this is refused instead of producing a
    # package where one silently replaces the other.
    seen = set()
    for entry in live_entries:
        key = "/".join(part.casefold() for part in entry["folder"] + [entry["filename"]])
        if key in seen:
            raise ValueError(f"duplicate Live path on a case-insensitive volume: {key}")
        seen.add(key)
    return live_entries


def build_package(
    *,
    vod_tasks: list[dict],
    export_config: dict,
    staging_dir: Path,
    volume_bytes: int = base.DEFAULT_VOLUME_BYTES,
    generation: int | None = None,
    binaries_dir: Path | None = None,
    runtime_files: dict[str, Path] | None = None,
) -> dict:
    if staging_dir.exists():
        raise ValueError(f"staging directory already exists: {staging_dir}")

    # Accept either the task list or the raw ``/api/vod/status`` payload, so the
    # operator can feed the inventory file straight from the server.
    if isinstance(vod_tasks, dict):
        vod_tasks = vod_tasks.get("tasks") or []
    if not isinstance(vod_tasks, list):
        raise ValueError("vod_tasks must be a list of tasks or a status payload")
    if not isinstance(export_config, dict):
        raise ValueError("export_config must be a mapping")

    if generation is None:
        generation = int(export_config.get("generation", 1))

    live_entries = process_live_identities(export_config)
    if not live_entries:
        raise ValueError("no approved Live channels found in export configuration")

    vod_candidates, rejected = process_vod_tasks(vod_tasks, return_rejected=True)

    # Validate volume capacity (including cluster alignment and reserve).
    #
    # Live channels are always published. VODs are best-fit selected in the
    # given input order (the server returns newest-first) and included while
    # they fit; the first VOD that does not fit never stops the scan, because a
    # smaller later video may still fit. An omitted video is always reported,
    # never silently dropped, and the total allocation never exceeds the limit.
    # Instrumentação: o volume que CHEGA aqui é o que decide se a grade cabe. Já
    # houve um build recusando a grade ativa com available=127 GiB (volume de
    # 128 GiB) enquanto todas as constantes lidas diziam 256 GiB, então o valor
    # efetivo e o chamador ficam registrados em vez de inferidos.
    try:
        import sys as _sys
        _caller = _sys._getframe(1).f_code.co_name
    except Exception:
        _caller = "?"
    print("build volume_bytes=%d (%.0f GiB) reserve=%d caller=%s"
          % (volume_bytes, volume_bytes / 1024**3,
             min(1024**3, volume_bytes // 8), _caller), file=sys.stderr, flush=True)

    reserve = min(1024**3, volume_bytes // 8)
    available = volume_bytes - reserve

    aligned_live = sum(align_cluster_bytes(e["logical_size"]) for e in live_entries)
    if aligned_live > available:
        # Even the mandatory Live channels alone do not fit: fail loudly.
        raise ValueError(
            f"capacity exceeded: required {aligned_live} bytes ({aligned_live / 1024**3:.2f} GiB) "
            f"> available {available} bytes ({available / 1024**3:.2f} GiB)"
        )

    titles = {str(task.get("id") or ""): (task.get("title") or task.get("display_name") or "")
              for task in vod_tasks}
    vod_entries: list[dict] = []
    omitted_tasks: list[dict] = []
    aligned_total = aligned_live
    capacity_shortfall = 0
    for entry in vod_candidates:
        size_aligned = align_cluster_bytes(entry["logical_size"])
        if aligned_total + size_aligned <= available:
            aligned_total += size_aligned
            vod_entries.append(entry)
            continue
        capacity_shortfall += size_aligned
        omitted_tasks.append({
            "task_id": entry["channel_id"],
            "title": titles.get(entry["channel_id"]) or entry["filename"],
            "reason": "does_not_fit",
        })

    all_entries = live_entries + vod_entries

    # Validate file ID uniqueness
    file_ids = {e["file_id"] for e in all_entries}
    if len(file_ids) != len(all_entries):
        raise ValueError("file_id collision detected in mixed entries")

    # Validate path uniqueness
    paths = {e["path"].lower() for e in all_entries}
    if len(paths) != len(all_entries):
        raise ValueError("path collision detected between Live and VOD")

    # Build input list for mixed template builder
    template_entries = [{"path": e["path"], "size": e["logical_size"]} for e in all_entries]

    scratch_dir = None
    try:
        scratch_dir, layout = mixed_template.build(template_entries, volume_bytes=volume_bytes)

        staging_dir.mkdir(parents=True, mode=0o755)

        # 1. Manifest
        canonical_bytes, sorted_entries = canonical_manifest_bytes(all_entries, generation=generation)
        manifest_id = hashlib.sha256(canonical_bytes).hexdigest()
        manifest_data = {
            "schema_version": 1,
            "layout_version": 1,
            "generation": generation,
            "manifest_id": manifest_id,
            "entries": sorted_entries,
        }
        manifest_path = staging_dir / "favorites_manifest.json"
        manifest_path.write_text(json.dumps(manifest_data, indent=2) + "\n")

        # 2. Template and Layout
        for name in ("favorites_template.sparse.gz", "favorites_layout.json", "favorites_package.json"):
            shutil.copy2(scratch_dir / name, staging_dir / name)

        # 3. Allowlist for Mixed VOD Proxy (size + trusted content revision)
        allowlist_tasks = [
            {"task_id": e["channel_id"], "size": e["logical_size"],
             "revision": e["content_revision"]}
            for e in sorted_entries
            if e["kind"] == "vod"
        ]
        allowlist_data = {"tasks": allowlist_tasks}
        for item in allowlist_tasks:
            if not vod_revision.is_valid_revision(item["revision"]):
                raise ValueError(
                    f"task {item['task_id']} has no trusted content revision")
        allowlist_path = staging_dir / "vod_allowlist.json"
        allowlist_path.write_text(json.dumps(allowlist_data, indent=2) + "\n")

        # 3b. Omission report. Written only when at least one ready VOD did not
        # fit: an omitted task must never appear in the manifest or the
        # allowlist, but its omission must always be on record.
        omission_report = {
            "requested_count": len(vod_candidates),
            "included_count": len(vod_entries),
            "omitted_count": len(omitted_tasks),
            "capacity_shortfall_bytes": capacity_shortfall,
            "omitted_tasks": omitted_tasks,
            "volume_bytes": volume_bytes,
            "available_bytes": available,
            "allocated_cluster_bytes": aligned_total,
        }
        if omitted_tasks:
            (staging_dir / OMISSION_ARTIFACT).write_text(
                json.dumps(omission_report, indent=2) + "\n")

        # 4. Runtime files: ARM32 binaries, supervisor script and the private
        # transport files (which are never committed and never printed).
        if binaries_dir is not None and Path(binaries_dir).is_dir():
            for b_name in ("fuse_ntfs", "sparse_unpack"):
                src_b = Path(binaries_dir) / b_name
                if src_b.is_file():
                    shutil.copy2(src_b, staging_dir / b_name)
                    os.chmod(staging_dir / b_name, 0o755)
        for r_name, r_src in (runtime_files or {}).items():
            r_src = Path(r_src)
            if not r_src.is_file():
                continue
            shutil.copy2(r_src, staging_dir / r_name)
            os.chmod(staging_dir / r_name,
                     0o600 if r_name in PRIVATE_FILES else 0o755)

        # 5. Verify all expected artifacts exist
        for name in ARTIFACTS:
            if not (staging_dir / name).is_file():
                raise RuntimeError(f"missing artifact {name} in staging")

        # 6. SHA256SUMS (covering artifacts, binaries, and supervisor)
        artifacts_to_sum = list(ARTIFACTS)
        for extra in EXTRA_FILES:
            if (staging_dir / extra).is_file():
                artifacts_to_sum.append(extra)
        artifacts_to_sum.sort()

        sums_path = staging_dir / "SHA256SUMS"
        lines = [f"{sha256_file(staging_dir / name)}  {name}" for name in artifacts_to_sum]
        sums_path.write_text("\n".join(lines) + "\n")

        summary = {
            "staging": str(staging_dir),
            "generation": generation,
            "manifest_id": manifest_id,
            "total_files": len(all_entries),
            "live_count": len(live_entries),
            "vod_count": len(vod_entries),
            "rejected_count": len(rejected),
            "rejected_tasks": rejected,
            "omitted_count": len(omitted_tasks),
            "omitted_tasks": omitted_tasks,
            "included_count": len(vod_entries),
            "requested_count": len(vod_candidates),
            "capacity_shortfall_bytes": capacity_shortfall,
            "volume_bytes": volume_bytes,
            "allocated_cluster_bytes": aligned_total,
            "available_bytes": available,
            "free_bytes": available - aligned_total,
            "hashes": {name: sha256_file(staging_dir / name) for name in artifacts_to_sum},
        }
        (staging_dir / "package_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        return summary
    finally:
        if scratch_dir is not None:
            shutil.rmtree(scratch_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vod-inventory", type=Path, required=True,
                        help="Path to JSON file containing ready VOD tasks")
    parser.add_argument("--export-config", type=Path, required=True,
                        help="Path to reviewed Live export config (e.g. export-revisao-sol.json)")
    parser.add_argument("--staging", type=Path, required=True,
                        help="Staging output directory for the generation package")
    parser.add_argument("--generation", type=int, default=None,
                        help="Package generation number (defaults to export config generation)")
    parser.add_argument("--volume-bytes", type=int, default=base.DEFAULT_VOLUME_BYTES,
                        help="NTFS virtual volume size in bytes (default 128 GiB)")
    parser.add_argument("--binaries-dir", type=Path, default=None,
                        help="Optional directory containing fuse_ntfs, sparse_unpack to include in package")
    args = parser.parse_args()

    vod_tasks = json.loads(args.vod_inventory.read_text())
    export_config = json.loads(args.export_config.read_text())

    summary = build_package(
        vod_tasks=vod_tasks,
        export_config=export_config,
        staging_dir=args.staging.resolve(),
        volume_bytes=args.volume_bytes,
        generation=args.generation,
        binaries_dir=args.binaries_dir,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
