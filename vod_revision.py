#!/usr/bin/env python3
"""Trusted content revision for ready VOD objects.

Lives at the repository root so the production container (which only copies
root-level modules) and the operational scripts share one implementation.

Every layer that can serve VOD bytes must agree on a trusted identity of the
bytes, so a mutated object can never be served under an old generation,
allowlist or cache entry:

* the server computes it from its authoritative task record and exposes it in
  ``/api/vod/status`` (field ``content_revision``);
* the generation builder copies that value into the manifest, the allowlist and
  the generation fingerprint;
* the proxy enforces the allowlist value and forwards it upstream;
* the FUSE client enforces it against its local allowlist copy;
* the server re-validates it on every internal range request.

A revision is one of:

* ``md5:<32 hex>``  -- published object MD5 (Google Drive uploads);
* ``fs:<32 hex>``   -- SHA-256 of ``size:mtime_ns`` of the local source file;
* ``rev:<32 hex>``  -- SHA-256 of an explicit opaque revision string.

``None`` means "cannot be verified": callers must fail closed and refuse to
serve, cache or publish such an object instead of trusting size alone.
"""
from __future__ import annotations

import hashlib
import os
import re

REVISION_HEADER = "X-Vod-Revision"
REVISION_RE = re.compile(r"^(?:md5|fs|rev):[0-9a-f]{32}$")
MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_TASK_RE = re.compile(r"^[0-9a-f]{8}$")


def is_valid_revision(value: object) -> bool:
    """True only for a canonical revision string produced by this module."""
    return isinstance(value, str) and bool(REVISION_RE.fullmatch(value))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _md5_revision(md5: object) -> str | None:
    text = str(md5 or "").strip().lower()
    return f"md5:{text}" if MD5_RE.fullmatch(text) else None


def _fs_revision(path: object) -> str | None:
    path = os.fspath(path) if path else ""
    if not path:
        return None
    try:
        info = os.stat(path)
    except OSError:
        return None
    return f"fs:{_digest(f'{info.st_size}:{info.st_mtime_ns}')}"


def content_revision(task: dict) -> str | None:
    """Trusted revision for one VOD task, or ``None`` when unverifiable.

    An explicit ``content_revision``/``media_revision`` value (already computed
    by the authoritative owner) wins. Otherwise the digest of the published
    object is used, then the local source file identity. Size alone is never
    accepted as a revision.
    """
    if not isinstance(task, dict):
        return None
    explicit = task.get("content_revision")
    if is_valid_revision(explicit):
        return explicit
    for key in ("remote_md5", "md5", "content_md5"):
        revision = _md5_revision(task.get(key))
        if revision:
            return revision
    revision = _fs_revision(task.get("file_path"))
    if revision:
        return revision
    opaque = task.get("media_revision") or task.get("revision")
    if isinstance(opaque, str) and opaque.strip():
        return f"rev:{_digest(opaque.strip())}"
    return None


def is_task_id(value: object) -> bool:
    return isinstance(value, str) and bool(_TASK_RE.fullmatch(value))


def allowlist_entry(task: dict) -> dict | None:
    """Canonical allowlist record, or ``None`` when the task is unverifiable."""
    task_id = task.get("id")
    size = task.get("file_size") or task.get("file_size_bytes")
    revision = content_revision(task)
    if not is_task_id(task_id) or isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    if revision is None:
        return None
    return {"task_id": task_id, "size": size, "revision": revision}


def parse_allowlist_entry(item: object) -> tuple[str, int, str] | None:
    """Validate one allowlist record; returns ``(task_id, size, revision)``."""
    if not isinstance(item, dict):
        return None
    task_id = item.get("task_id")
    size = item.get("size")
    revision = item.get("revision")
    if not is_task_id(task_id):
        return None
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    if not is_valid_revision(revision):
        return None
    return task_id, size, revision
