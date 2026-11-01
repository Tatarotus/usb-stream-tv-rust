#!/usr/bin/env python3
"""Build the complete grade staging package from the reviewed export config.

Produces, in a new staging directory: SPARSE01 template, layout, canonical
manifest, package, SHA256SUMS and a validation report. The template is built in
scratch first; the mounted production image is never touched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_live_favorites_template as template  # noqa: E402

ARTIFACTS = ("favorites_template.sparse.gz", "favorites_layout.json",
             "favorites_package.json", "favorites_manifest.json")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--volume-bytes", type=int, default=template.DEFAULT_VOLUME_BYTES)
    parser.add_argument("--file-bytes", type=int, default=template.DEFAULT_FILE_BYTES)
    parser.add_argument("--allow-partial", action="store_true",
                        help="staging only: package the approved subset and report pending identities")
    args = parser.parse_args()
    staging = args.staging.resolve()
    if staging.exists():
        parser.error("staging directory already exists; choose a new generation path")
    config = json.loads(args.export.read_text())
    if config.get("provisional"):
        parser.error("provisional export: measure the prepared output and regenerate the export")
    approved = [item for item in config["identities"] if item["status"] == "approved"]
    pending = [item for item in config["identities"] if item["status"] != "approved"]
    if not approved:
        parser.error("export config has no approved identity")
    if pending and not args.allow_partial:
        parser.error("staging package requires every identity approved; "
                     "use --allow-partial for a staging-only partial grade")
    paths = [f"{item['category']}/{item['filename']}" for item in approved]
    scratch_dir = None
    try:
        scratch_dir, layout = template.build(paths, volume_bytes=args.volume_bytes,
                                             file_bytes=args.file_bytes)
        staging.mkdir(parents=True, mode=0o755)
        manifest_path = staging / "favorites_manifest.json"
        subprocess.run([sys.executable, str(ROOT / "scripts/build_live_favorites_manifest.py"),
                        "--layout", str(scratch_dir / "favorites_layout.json"),
                        "--export", str(args.export),
                        "--output", str(manifest_path)], check=True, capture_output=True)
        for name in ("favorites_template.sparse.gz", "favorites_layout.json",
                     "favorites_package.json"):
            shutil.copy2(scratch_dir / name, staging / name)
        for name in ARTIFACTS:
            if not (staging / name).is_file():
                raise RuntimeError(f"missing artifact {name}")
        sums = staging / "SHA256SUMS"
        lines = [f"{sha256(staging / name)}  {name}" for name in ARTIFACTS]
        for name in ("allocate_live_files",):
            if (scratch_dir / name).exists():
                shutil.copy2(scratch_dir / name, staging / name)
        sums.write_text("\n".join(lines) + "\n")
        summary = {
            "staging": str(staging),
            "generation": config["generation"],
            "identities": len(approved),
            "complete_grade": not pending,
            "pending": [{"identity": item["identity"], "status": item["status"],
                         "quality_status": item.get("quality_status"),
                         "best_channel_id": (item.get("chosen") or {}).get("channel_id")}
                        for item in pending],
            "paths": paths,
            "volume_bytes": layout["volume_bytes"],
            "host_physical_bytes": layout["host_physical_bytes"],
            "hashes": {name: sha256(staging / name) for name in ARTIFACTS},
        }
        (staging / "package_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    finally:
        if scratch_dir is not None:
            shutil.rmtree(scratch_dir, ignore_errors=True)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
