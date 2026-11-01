#!/usr/bin/env python3
"""Minimal, authenticated bridge between the web container and the publisher.

Why this exists
---------------
The dashboard button runs inside the ``usb-stream-tv`` container, which has no
``adb``, no ``ssh`` client and no NTFS tooling, and it must not get them: the
publisher speaks ADB to the tablet and kills processes, so it belongs on the
host that owns those privileges. A published TCP port would add a new attack
surface; instead both sides meet on a shared, narrowly permissioned directory.

Protocol (one directory, no network)
------------------------------------
``SPOOL`` (0700)::

    requests/     writable ONLY by the container's uid
    processing/   owned by the daemon
    responses/    one token-free JSON answer per request id
    done/         replay guards for the freshness window

A request is a small JSON object written atomically (``O_EXCL`` + rename,
mode 0600, ``O_NOFOLLOW``) into ``requests/<id>.json``::

    {"id": "<32 hex>", "action": "apply"|"verify", "task_id": "<8 hex>",
     "issued_at": <epoch seconds>, "version": 1,
     "confirm_interrupt": <optional boolean, apply only>}

The daemon claims a request by renaming it into ``processing/`` (atomic), then
refuses it unless **all** of the following hold: the file is a regular file
(never a symlink), mode 0600, owned by the configured client uid, at most 4 KiB,
within the freshness window, not already answered, and the schema above holds.
Only then is the publisher executed — always as an argv list, never a shell —
with ``--apply --task-id <id> [--verify|--confirm-interrupt] --json``. Its stdout is the answer,
written back atomically; the token-free payload is the only thing crossing back.

What this deliberately does NOT do: no arbitrary command, no arbitrary path, no
credential in a request or a response, no request without a live uid proof, and
no request replay. The PIN and the private package files stay on the daemon
side, taken from its own environment.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

try:
    import publication_lock
except ImportError as _lock_import_error:  # pragma: no cover - deployment guard
    publication_lock = None
    _LOCK_IMPORT_ERROR: str | None = str(_lock_import_error)
else:
    _LOCK_IMPORT_ERROR = None

REQUEST_VERSION = 1
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 256 * 1024
FRESHNESS_SECONDS = 120
PUBLISHER_TIMEOUT_SECONDS = 900
ACTIONS = ("apply", "verify", "refresh", "verify_inventory")
# Actions that carry no single video (inventory-only refresh).
TASKLESS_ACTIONS = ("refresh", "verify_inventory")
_TASK_ID = re.compile(r"[0-9a-f]{8}\Z")
_HEX_ID = re.compile(r"[0-9a-f]{32}\Z")


class BridgeError(RuntimeError):
    """A request/answer that must be refused (never silently degraded)."""


def _layout(spool: Path) -> dict[str, Path]:
    return {name: spool / name
            for name in ("requests", "processing", "responses", "done")}


def _ensure_layout(spool: Path) -> dict[str, Path]:
    spool.mkdir(parents=True, exist_ok=True)
    os.chmod(spool, 0o700)
    dirs = _layout(spool)
    for path in dirs.values():
        path.mkdir(exist_ok=True)
        os.chmod(path, 0o700)
    return dirs


def _write_atomic(path: Path, data: bytes, *, uid: int | None = None,
                  strict_owner: bool = False) -> None:
    """Write ``path`` through a private temp file and an atomic rename."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o600)
            if uid is not None:
                try:
                    os.fchown(handle.fileno(), uid, -1)
                    if strict_owner and os.fstat(handle.fileno()).st_uid != uid:
                        raise OSError("request owner mismatch")
                except OSError:
                    if strict_owner:
                        raise
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_bounded(path: Path, limit: int) -> tuple[bytes, os.stat_result]:
    """Read a regular file only (never a symlink/device), bounded in size.

    ``O_NONBLOCK`` matters: a FIFO planted in the spool would otherwise block
    the daemon *before* the regular-file check could reject it.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BridgeError("not a regular file")
        if info.st_size > limit:
            raise BridgeError("file too large")
        chunks = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            raise BridgeError("file too large")
        return data, info
    finally:
        os.close(fd)


# --------------------------------------------------------------------- client
def build_request(action: str, task_id: str | None = None, *,
                  now: float | None = None,
                  confirm_interrupt: bool = False) -> dict:
    if action not in ACTIONS:
        raise BridgeError(f"unsupported action: {action}")
    if action in TASKLESS_ACTIONS:
        # An inventory refresh has no single video to name.
        if task_id not in (None, ""):
            raise BridgeError("this action takes no task id")
        task_id = None
    elif not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise BridgeError("invalid task id")
    if not isinstance(confirm_interrupt, bool) or (confirm_interrupt and action != "apply" and action != "refresh"):
        raise BridgeError("invalid interruption confirmation")
    return {
        "version": REQUEST_VERSION,
        "id": uuid.uuid4().hex,
        "action": action,
        "task_id": task_id,
        "issued_at": int(time.time() if now is None else now),
        "confirm_interrupt": confirm_interrupt,
    }


def submit(spool: Path, request: dict, *, request_owner_uid: int | None = None) -> Path:
    if request_owner_uid is not None and (isinstance(request_owner_uid, bool)
            or not isinstance(request_owner_uid, int) or not 0 <= request_owner_uid < 2**32 - 1):
        raise BridgeError("invalid request owner uid")
    dirs = _ensure_layout(spool)
    name = f"{request['id']}.json"
    _write_atomic(dirs["requests"] / name, json.dumps(request).encode("utf-8"),
                  uid=request_owner_uid, strict_owner=request_owner_uid is not None)
    return dirs["responses"] / name


def wait_response(response: Path, *, timeout: float, interval: float = 0.25) -> bytes:
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        if response.exists():
            data, _info = _read_bounded(response, MAX_RESPONSE_BYTES)
            return data
        time.sleep(interval)
    raise BridgeError("timeout waiting for the publisher answer")


def cmd_client(args) -> int:
    spool = Path(args.spool)
    request = build_request(args.action, args.task_id, confirm_interrupt=args.confirm_interrupt)
    request_path = spool / "requests" / f"{request['id']}.json"
    response_path = spool / "responses" / f"{request['id']}.json"
    try:
        response = submit(spool, request, request_owner_uid=args.request_owner_uid)
        data = wait_response(response, timeout=args.timeout)
    except (BridgeError, OSError) as exc:
        print(json.dumps({"ok": False, "available": True,
                          "state": "unknown", "confirmed": None,
                          "reason": f"bridge_{type(exc).__name__.lower()}",
                          "message": "ponte com o publicador indisponível"}))
        return 2
    finally:
        # The answer is consumed once, and the request file never lingers.
        for path in (request_path, response_path):
            try:
                path.unlink()
            except OSError:
                pass
    sys.stdout.write(data.decode("utf-8", "replace"))
    try:
        payload = json.loads(data)
    except ValueError:
        return 1
    return 0 if isinstance(payload, dict) else 1


# --------------------------------------------------------------------- daemon
def validate_request(raw: bytes, info: os.stat_result, *, client_uid: int | None,
                     now: float | None = None) -> dict:
    """Refuse anything that is not a well-formed, fresh, owner-proven request."""
    if not stat.S_ISREG(info.st_mode):
        raise BridgeError("request is not a regular file")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise BridgeError("request file is not private (0600)")
    if client_uid is None or info.st_uid != client_uid:
        # The uid proof is the authentication: without a configured client uid
        # any local user able to reach the spool could ask for a publication.
        raise BridgeError("request owner is not the configured client uid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BridgeError("request is not valid JSON") from None
    if not isinstance(payload, dict):
        raise BridgeError("request is not a JSON object")
    if payload.get("version") != REQUEST_VERSION:
        raise BridgeError("unsupported request version")
    request_id = payload.get("id")
    if not isinstance(request_id, str) or not _HEX_ID.fullmatch(request_id):
        raise BridgeError("invalid request id")
    if payload.get("action") not in ACTIONS:
        raise BridgeError("unsupported action")
    confirmed = payload.get("confirm_interrupt", False)
    if not isinstance(confirmed, bool) or (
            confirmed and payload["action"] not in ("apply", "refresh")):
        raise BridgeError("invalid interruption confirmation")
    task_id = payload.get("task_id")
    if payload["action"] in TASKLESS_ACTIONS:
        if task_id not in (None, ""):
            raise BridgeError("this action takes no task id")
    elif not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise BridgeError("invalid task id")
    issued_at = payload.get("issued_at")
    if isinstance(issued_at, bool) or not isinstance(issued_at, int):
        raise BridgeError("invalid issued_at")
    reference = time.time() if now is None else now
    if abs(reference - issued_at) > FRESHNESS_SECONDS:
        raise BridgeError("request is stale or from the future")
    return payload


def publisher_argv(publisher: list[str], request: dict) -> list[str]:
    """The only commands the daemon will ever run, as an argv list."""
    if request["action"] == "verify_inventory":
        argv = list(publisher) + ["--verify-inventory"]
    elif request["action"] == "refresh":
        argv = list(publisher) + ["--refresh"]
    else:
        argv = list(publisher) + ["--apply", "--task-id", request["task_id"]]
    if request["action"] == "verify":
        argv.append("--verify")
    elif request.get("confirm_interrupt") is True:
        argv.append("--confirm-interrupt")
    argv.append("--json")
    return argv


def _parse_json_output(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except ValueError:
        pass
    start = text.rfind("\n{")
    if start == -1:
        return None
    try:
        payload = json.loads(text[start + 1:])
        return payload if isinstance(payload, dict) else None
    except ValueError:
        return None


def _reap_stale(dirs: dict[str, Path], *, now: float) -> None:
    """Drop leftovers older than the freshness window (never anything newer).

    A daemon that dies mid-request leaves an already-claimed file behind; it is
    never reprocessed (the claim was atomic) and is removed once it could no
    longer be answered. Answers whose consumer died are removed the same way.
    """
    for name in ("done", "requests", "responses", "processing"):
        # A request issued at t may legitimately still arrive until t+window, so
        # its replay guard (and any orphaned request file) must outlive it.
        retention = 2 * FRESHNESS_SECONDS if name in ("done", "requests") \
            else FRESHNESS_SECONDS
        for stale in dirs[name].glob("*.json"):
            try:
                if stale.stat().st_mtime < now - retention:
                    stale.unlink()
            except OSError:
                pass


def _reserve(dirs: dict[str, Path], request_id: str, *, now: float) -> bool:
    """Claim the replay guard for ``request_id`` before any external effect.

    ``O_EXCL`` makes it exclusive, so two daemons (or a resubmitted id) can
    never both execute the request; the marker is created *before* the
    publisher runs, not after.
    """
    marker = dirs["done"] / f"{request_id}.json"
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as handle:
        json.dump({"reserved_ts": int(now)}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def state_file_from_publisher(publisher: list[str]) -> str:
    """The publisher's ``--state-file``, as written in the bridge's own argv.

    The bridge needs it to resolve the *same* coordination lock the publisher
    will resolve; the path is already in ``--publisher`` because that is the argv
    the bridge executes.
    """
    for index, token in enumerate(publisher):
        if token == "--state-file" and index + 1 < len(publisher):
            return publisher[index + 1]
        if token.startswith("--state-file="):
            return token.split("=", 1)[1]
    return ""


def process_one(spool: Path, publisher: list[str], *, client_uid: int | None,
                runner, now: float | None = None,
                publisher_timeout: float = PUBLISHER_TIMEOUT_SECONDS,
                lock_path: Path | None = None) -> str | None:
    """Claim and answer at most one request. Returns the id, or None.

    The SHARED lock is taken *before* the request is renamed out of
    ``requests/`` and held until the publisher exits, so a migration window
    (EXCLUSIVE) can never overlap a claim: while the window is open this returns
    None and the request stays exactly where it is, untouched.
    """
    dirs = _ensure_layout(spool)
    reference = time.time() if now is None else now
    for candidate in sorted(dirs["requests"].glob("*.json")):
        # Every name this bridge ever writes is a minted 32-hex id, and the
        # answer path is derived from it: anything else is ignored, so no
        # request-controlled basename can shape a file in the spool.
        request_id = candidate.name[:-5]
        if not _HEX_ID.fullmatch(request_id):
            continue
        try:
            info = os.lstat(candidate)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue                       # never follow a symlink left behind
        guard = None
        if lock_path is not None:
            try:
                guard = publication_lock.acquire(lock_path, shared=True,
                                                 kind="bridge-claim")
            except publication_lock.LockBusy:
                # A migration window is open. Not claiming is the whole point:
                # the request keeps waiting and no publisher is started, so the
                # migrator cannot cut a publication in half by restarting us.
                return None
        claimed = dirs["processing"] / candidate.name
        try:
            os.rename(candidate, claimed)   # atomic claim: exactly one daemon wins
        except OSError:
            if guard is not None:
                guard.release()
            continue
        try:
            answer = _handle_claimed(claimed, request_id, dirs, publisher,
                                     client_uid=client_uid, runner=runner,
                                     now=reference, publisher_timeout=publisher_timeout)
        finally:
            try:
                claimed.unlink()
            except OSError:
                pass
            if guard is not None:
                guard.release()
        _write_atomic(dirs["responses"] / f"{request_id}.json",
                      json.dumps(answer).encode("utf-8"), uid=client_uid)
        _reap_stale(dirs, now=reference)
        return request_id
    return None


def _handle_claimed(path: Path, request_id: str, dirs: dict[str, Path],
                    publisher: list[str], *, client_uid: int | None, runner,
                    now: float, publisher_timeout: float) -> dict:
    try:
        raw, info = _read_bounded(path, MAX_REQUEST_BYTES)
        request = validate_request(raw, info, client_uid=client_uid, now=now)
    except (BridgeError, OSError) as exc:
        return {"ok": False, "available": True, "state": "rejected",
                "reason": str(exc)}
    if request["id"] != request_id:
        return {"ok": False, "available": True, "state": "rejected",
                "reason": "request id mismatch"}
    if not _reserve(dirs, request_id, now=now):
        return {"ok": False, "available": True, "state": "rejected",
                "reason": "replayed_request"}
    try:
        completed = runner(publisher_argv(publisher, request),
                           timeout=publisher_timeout)
    except subprocess.TimeoutExpired:
        # A killed publisher may already have touched the tablet. Never assert
        # rollback or failure was clean; the journal must be reconciled.
        return {"ok": False, "available": True, "state": "unknown",
                "reason": "publisher_timeout_reconciliation_required"}
    except OSError as exc:
        return {"ok": False, "available": True, "state": "unknown",
                "reason": f"publisher_unavailable:{type(exc).__name__}"}
    payload = _parse_json_output(completed.stdout or "")
    if payload is None:
        return {"ok": False, "available": True, "state": "unknown",
                "reason": "unparseable_publisher_output"}
    payload.setdefault("ok", True)
    payload.setdefault("available", True)
    return payload


def cmd_serve(args) -> int:
    spool = Path(args.spool)
    _ensure_layout(spool)
    publisher = list(args.publisher)
    if not publisher:
        raise BridgeError("no publisher command configured")
    if args.client_uid is None:
        # Refusing here is the point: the uid proof is the authentication.
        raise BridgeError("--client-uid is required (the container's uid)")
    if not math.isfinite(args.publisher_timeout) or not 1 <= args.publisher_timeout <= 3600:
        raise BridgeError("invalid publisher timeout")

    # The coordination lock is mandatory: without it a migration window could
    # not exclude a claim, and "we did not find the file" must never be read as
    # "no publication is running". Refusing to start is the fail-closed choice.
    if publication_lock is None:
        raise BridgeError(
            "publication_lock.py is not importable (%s); refusing to serve "
            "without the quiescence lock" % _LOCK_IMPORT_ERROR)
    publisher_state_file = state_file_from_publisher(publisher)
    lock_path = publication_lock.resolve_lock(
        explicit=os.environ.get(publication_lock.LOCK_ENV),
        state_file=publisher_state_file)
    if lock_path is None:
        lock_path = publication_lock.resolve_lock(
            explicit=os.environ.get(publication_lock.LOCK_ENV), spool=spool)
        print("warning: the publisher argv carries no --state-file; the "
              "quiescence lock falls back to %s and cannot be proven shared "
              "with the publisher (the migrator will refuse in this state)"
              % lock_path, file=sys.stderr, flush=True)

    def runner(argv, timeout):
        environment = dict(os.environ)
        # The child must lock exactly what this daemon locks, even if its own
        # arguments would resolve elsewhere.
        environment[publication_lock.LOCK_ENV] = str(lock_path)
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL,
                              check=False, env=environment)

    handled = 0
    deadline = time.monotonic() + (args.idle_timeout or 0)
    while True:
        request_id = process_one(spool, publisher, client_uid=args.client_uid,
                                 runner=runner,
                                 publisher_timeout=args.publisher_timeout,
                                 lock_path=lock_path)
        if request_id is not None:
            handled += 1
            print(f"handled request={request_id}", flush=True)
            if args.once:
                return 0
            continue
        if args.once or (args.max_requests and handled >= args.max_requests):
            return 0
        if args.idle_timeout and time.monotonic() >= deadline:
            return 0
        time.sleep(args.poll_interval)
        if args.idle_timeout:
            deadline = time.monotonic() + args.idle_timeout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    client = sub.add_parser("client", help="ask the publisher through the spool")
    client.add_argument("--spool", required=True)
    client.add_argument("--action", choices=ACTIONS, required=True)
    client.add_argument("--task-id", default=None,
                        help="required for apply/verify; omitted for refresh")
    client.add_argument("--request-owner-uid", type=int, default=None,
                        help="give the request to the publisher daemon uid")
    client.add_argument("--confirm-interrupt", action="store_true",
                        help="explicitly permit interruption for apply only")
    client.add_argument("--timeout", type=float, default=60.0)

    serve = sub.add_parser("serve", help="answer requests from the web container")
    serve.add_argument("--spool", required=True)
    serve.add_argument("--publisher", nargs=argparse.REMAINDER, default=[],
                       help="publisher argv (no shell), e.g. python3 /opt/.../mixed_generation_service.py")
    serve.add_argument("--client-uid", type=int, required=True,
                       help="only accept requests owned by the container's uid")
    serve.add_argument("--once", action="store_true",
                       help="answer at most one request, then exit")
    serve.add_argument("--max-requests", type=int, default=0)
    serve.add_argument("--idle-timeout", type=float, default=0.0)
    serve.add_argument("--poll-interval", type=float, default=0.5)
    serve.add_argument("--publisher-timeout", type=float, default=PUBLISHER_TIMEOUT_SECONDS,
                       help="maximum publisher runtime (independent of request freshness)")

    args = parser.parse_args(argv)
    try:
        if args.mode == "client":
            return cmd_client(args)
        return cmd_serve(args)
    except BridgeError as exc:
        print(f"[bridge] refusing: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
