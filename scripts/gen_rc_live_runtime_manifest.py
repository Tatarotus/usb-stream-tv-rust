#!/usr/bin/env python3
"""Generate a deterministic SHA-256 manifest of the RC-Live runtime inputs.

RC-Live ships (or directly builds) the files listed in ``RUNTIME_INPUTS``.
This tool walks that fixed allowlist in sorted order and writes a
``sha256sum``-compatible manifest, so the release is reproducible and the
manifest can be verified from the repository alone:

    python3 scripts/gen_rc_live_runtime_manifest.py \
        --out docs/release/RC_LIVE_RUNTIME_MANIFEST.sha256

Deliberately excluded: catalogs/caches, secrets (.env), the inert TASK-006/B
parser probe, Rust build output (target/), and __pycache__.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

# Explicit allowlist. Globs are expanded relative to the repository root.
RUNTIME_INPUTS = [
    # Host runtime (Dockerfile COPY set + generated assets)
    "Dockerfile",
    "compose.yaml",
    ".env.example",
    "README.md",
    "gen_template.py",
    "server.py",
    "torrent_downloader.py",
    "channels.json",
    "generate_slate.sh",
    "dashboard.html",
    "manifest.json",
    "sw.js",
    "app-icon.png",
    "app-icon-512.png",
    "screenshot-desktop.png",
    "screenshot-mobile.png",
    "src/tools/ts_check.py",
    "templates/ntfs_template.sparse.gz",
    # Operational scripts
    "scripts/*.sh",
    "scripts/*.py",
    # Rust workspace build inputs
    "Cargo.toml",
    "Cargo.lock",
    ".cargo/config.toml",
    "crates/**/Cargo.toml",
    "crates/**/*.rs",
]


def _collect(root: Path) -> list[Path]:
    found: set[Path] = set()
    for pattern in RUNTIME_INPUTS:
        for path in root.glob(pattern):
            if path.is_file():
                found.add(path)
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root (default: cwd)")
    parser.add_argument("--out", required=True, help="manifest output path")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    files = _collect(root)
    if not files:
        print("no runtime inputs found", file=sys.stderr)
        return 1

    lines = []
    for path in files:
        rel = "./" + path.relative_to(root).as_posix()
        lines.append(f"{_sha256(path)}  {rel}")

    body = "\n".join(lines) + "\n"
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = root / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")

    manifest_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    print(f"files: {len(lines)}")
    print(f"manifest: {out_path}")
    print(f"manifest_sha256: {manifest_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())