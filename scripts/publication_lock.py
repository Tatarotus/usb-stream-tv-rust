#!/usr/bin/env python3
"""Atomic quiescence lock shared by the bridge, the publisher and the migrator.

Why this exists
---------------
Restarting the bridge while it is *claiming* or *running* a publication kills the
publisher too (systemd's default ``KillMode=control-group``), and a publisher
killed halfway through may already have touched the tablet. Checking the spool
before and after the stop can only *detect* that race after the damage: the
request is renamed into ``processing/`` and the child is spawned inside the very
window the migrator is closing. Detection is not exclusion.

One file, three users, two modes:

* the **publisher** holds SHARED for as long as a publication runs;
* the **bridge** holds SHARED from the claim of a request until its publisher
  exits (covering the gap between the rename and the child holding its own lock);
* the **migrator** holds EXCLUSIVE for the whole migration window.

So a SHARED holder blocks an EXCLUSIVE acquirer (the migrator refuses instead of
killing anything) and an EXCLUSIVE holder blocks every claim and every
publication (nothing new starts while the window is open). The exclusion is a
property of ``flock(2)`` on one inode, not of a check performed by a script.

Properties that matter here
---------------------------
* **Crash-safe by construction**: the kernel releases a ``flock`` when the last
  holder's file descriptor closes, so a killed holder leaves no stale lock and
  no PID to trust. Nothing polls a heartbeat to decide safety.
* **The file is never unlinked**: a new inode would let two holders both believe
  they are exclusive. Cleanup/retention code must never touch this path.
* **No signals, no ``kill``**: acquiring a lock that is taken only reports who
  holds it (advisory sidecar) and fails.
* **Fail-closed for the window**: if the migrator cannot acquire EXCLUSIVE it
  must refuse to migrate (see the script); this module never "downgrades" to
  proceeding.

Path resolution (identical for the three users, so they cannot disagree by
accident):

1. ``$MIXED_PUBLICATION_LOCK`` (absolute path) when set;
2. next to the state file: ``<dirname(--state-file)>/publication.lock``
   (the bridge finds the publisher's ``--state-file`` in its own
   ``--publisher`` argv, the publisher has it as an argument, the migrator reads
   it from the unit);
3. inside the spool: ``<spool>/publication.lock`` (bridge-only fallback).

The bridge *exports* the resolved path to the publisher it spawns, so the child
cannot resolve a different file by omission.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import sys
import time
from pathlib import Path

LOCK_ENV = "MIXED_PUBLICATION_LOCK"
LOCK_NAME = "publication.lock"
OWNER_SUFFIX = ".owner"
EXIT_BUSY = 3
EXIT_ERROR = 4


class LockBusy(RuntimeError):
    """The lock is held by somebody else: refuse, never wait forever."""

    def __init__(self, path: Path, holder: str = ""):
        self.path = Path(path)
        self.holder = holder or "unknown holder"
        super().__init__(f"{self.path} is held by {self.holder}")


class LockError(RuntimeError):
    """The lock could not be used at all (bad path, permissions)."""


def resolve_lock(*, explicit: str | None = None, state_file=None,
                 spool=None) -> Path | None:
    """The coordination file, or None when this caller cannot name one.

    ``explicit`` (or the environment) wins; then the state file's directory;
    then the spool directory.
    """
    override = explicit or os.environ.get(LOCK_ENV) or ""
    if override.strip():
        return Path(override.strip())
    if state_file not in (None, ""):
        return Path(str(state_file)).expanduser().parent / LOCK_NAME
    if spool not in (None, ""):
        return Path(str(spool)).expanduser() / LOCK_NAME
    return None


def resolve_lock_for_publisher(*, explicit: str | None = None, state_file=None,
                               spool=None) -> Path | None:
    """The path the *publisher* will use (no spool fallback: it has none)."""
    override = explicit or os.environ.get(LOCK_ENV) or ""
    if override.strip():
        return Path(override.strip())
    if state_file not in (None, ""):
        return Path(str(state_file)).expanduser().parent / LOCK_NAME
    return None


def owner_hint(path) -> str:
    """Advisory description of the holder, for refusal messages only.

    Never used to decide anything: a stale sidecar must not be able to make the
    migrator believe the lock is free, and a missing sidecar must not make it
    believe the lock is taken.
    """
    try:
        payload = json.loads(Path(str(path) + OWNER_SUFFIX).read_text())
    except Exception:
        return "unknown holder (no owner record)"
    if not isinstance(payload, dict):
        return "unknown holder (bad owner record)"
    kind = str(payload.get("kind") or "?")
    pid = payload.get("pid")
    mode = payload.get("mode") or "?"
    started = payload.get("started_at")
    age = ""
    if isinstance(started, (int, float)):
        age = " age=%.0fs" % max(0.0, time.time() - started)
    return f"{kind} pid={pid} mode={mode}{age}"


def _open_lock(path: Path, *, create: bool) -> int:
    flags = os.O_RDWR | os.O_CLOEXEC
    if create:
        flags |= os.O_CREAT
    mode = 0o666 if create else 0o600
    try:
        fd = os.open(path, flags, mode)
    except FileNotFoundError as exc:
        raise LockError(f"{path} does not exist") from exc
    except OSError as exc:
        raise LockError(f"{path} cannot be opened: {exc}") from exc
    if create:
        # The lock carries no data; it must be usable by whatever account runs
        # each of the three users (root for the migrator, the app's account for
        # the bridge and the publisher). A different account must never be able
        # to make the others unable to open it.
        try:
            os.fchmod(fd, 0o666)
        except OSError:
            pass
    return fd


def _write_owner(path: Path, *, mode: str, kind: str) -> None:
    payload = {"pid": os.getpid(), "mode": mode, "kind": kind,
               "started_at": time.time()}
    tmp = Path(f"{path}{OWNER_SUFFIX}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload))
        os.chmod(tmp, 0o666)
        os.replace(tmp, f"{path}{OWNER_SUFFIX}")
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _clear_owner(path: Path) -> None:
    """Remove the sidecar only when it is ours (best effort, never decisive)."""
    sidecar = Path(f"{path}{OWNER_SUFFIX}")
    try:
        payload = json.loads(sidecar.read_text())
    except Exception:
        return
    if isinstance(payload, dict) and payload.get("pid") == os.getpid():
        try:
            sidecar.unlink()
        except OSError:
            pass


class Lock:
    """A held lock. ``release()`` is idempotent; death releases it too."""

    def __init__(self, path: Path, fd: int, mode: str, kind: str):
        self.path = Path(path)
        self.shared = mode == "shared"
        self.kind = kind
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, -1
        if fd < 0:
            return
        _clear_owner(self.path)
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> "Lock":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def acquire(path, *, shared: bool, timeout: float = 0.0, kind: str = "",
            interval: float = 0.05) -> Lock:
    """Take the lock, or raise :class:`LockBusy` after ``timeout`` seconds.

    ``timeout=0`` is a single non-blocking attempt: the caller decides whether a
    short wait is acceptable (the migrator uses one to ride out a transient
    claim of the bridge, never a running publication).
    """
    path = Path(path)
    deadline = time.monotonic() + max(0.0, float(timeout))
    flag = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    mode = "shared" if shared else "exclusive"
    while True:
        fd = _open_lock(path, create=True)
        try:
            fcntl.flock(fd, flag | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                raise LockError(f"{path} cannot be locked: {exc}") from exc
            if time.monotonic() >= deadline:
                raise LockBusy(path, owner_hint(path)) from None
            time.sleep(interval)
            continue
        _write_owner(path, mode=mode, kind=kind or ("publisher" if shared else "migration"))
        return Lock(path, fd, mode, kind)


def probe(path) -> tuple[str, str]:
    """``("free"|"shared"|"exclusive", description)`` without holding anything."""
    path = Path(path)
    try:
        fd = _open_lock(path, create=False)
    except LockError:
        return "free", "no lock file yet"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            # Nothing blocks EXCLUSIVE. A sidecar may still be there from a holder
            # that died: it is a diagnostic, never the source of truth.
            hint = owner_hint(path)
            if hint.startswith("unknown holder (no owner record"):
                return "free", "no holder"
            return "free", f"no holder (stale record: {hint})"
        except OSError:
            pass
        try:
            # A SHARED holder would be compatible with this probe, so if it is
            # refused the holder must be EXCLUSIVE.
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return "shared", owner_hint(path)
        except OSError:
            return "exclusive", owner_hint(path)
    finally:
        os.close(fd)


def ensure_file(path, *, owner_of=None) -> int:
    """Create the lock file (mode 0666) and, when possible, match an owner.

    Called by the migrator (root) so the file does not end up owned by root in a
    directory the app's account must write to.
    """
    path = Path(path)
    fd = _open_lock(path, create=True)
    if owner_of not in (None, ""):
        try:
            info = os.stat(str(owner_of))
            os.fchown(fd, info.st_uid, info.st_gid)
        except OSError:
            pass
    return fd


# ------------------------------------------------------------------- CLI
def _cmd_hold(args) -> int:
    ready = Path(args.ready_file) if args.ready_file else None
    parent = os.getppid()

    def announce(text: str) -> None:
        if ready is None:
            print(text, flush=True)
            return
        tmp = ready.with_name(ready.name + f".{os.getpid()}.tmp")
        try:
            tmp.write_text(text + "\n")
            os.replace(tmp, ready)
        except OSError:
            pass

    try:
        lock = acquire(args.lock, shared=not args.exclusive, timeout=args.timeout,
                       kind=args.kind)
    except LockBusy as busy:
        announce(f"busy {busy.holder}")
        return EXIT_BUSY
    except LockError as exc:
        announce(f"error {exc}")
        return EXIT_ERROR
    announce(f"locked mode={'shared' if not args.exclusive else 'exclusive'} "
             f"pid={os.getpid()} path={lock.path}")
    try:
        # Hold until the parent goes away (its death must not leave the window
        # open) or the descriptor closes. Nothing else is touched.
        while True:
            if os.getppid() != parent:
                return 0
            try:
                chunk = os.read(0, 1)
            except OSError:
                chunk = b""
            if not chunk:
                time.sleep(0.2)
                if args.stdin_eof_exits:
                    return 0
                continue
    finally:
        lock.release()


def _cmd_state(args) -> int:
    state, holder = probe(args.lock)
    print(f"{state} {holder}")
    return 0 if state == "free" else 1


def _cmd_show(args) -> int:
    path = resolve_lock(explicit=os.environ.get(LOCK_ENV), state_file=args.state_file,
                        spool=args.spool)
    if path is None:
        print("unresolved")
        return 1
    print(path)
    return 0


def _cmd_ensure(args) -> int:
    path = resolve_lock(explicit=os.environ.get(LOCK_ENV), state_file=args.state_file,
                        spool=args.spool)
    if path is None:
        print("unresolved", file=sys.stderr)
        return EXIT_ERROR
    fd = ensure_file(path, owner_of=args.owner_of)
    os.close(fd)
    print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    hold = sub.add_parser("hold", help="hold the lock until this process dies")
    hold.add_argument("--lock", required=True)
    hold.add_argument("--exclusive", action="store_true",
                      help="migration window (default: shared operation)")
    hold.add_argument("--timeout", type=float, default=0.0)
    hold.add_argument("--kind", default="")
    hold.add_argument("--ready-file", default="")
    hold.add_argument("--stdin-eof-exits", action="store_true")
    hold.set_defaults(func=_cmd_hold)

    state = sub.add_parser("state", help="report free/shared/exclusive")
    state.add_argument("--lock", required=True)
    state.set_defaults(func=_cmd_state)

    show = sub.add_parser("show", help="print the resolved path")
    show.add_argument("--state-file", default="")
    show.add_argument("--spool", default="")
    show.set_defaults(func=_cmd_show)

    ensure = sub.add_parser("ensure", help="create the lock file with mode 0666")
    ensure.add_argument("--state-file", default="")
    ensure.add_argument("--spool", default="")
    ensure.add_argument("--owner-of", default="")
    ensure.set_defaults(func=_cmd_ensure)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except LockError as exc:
        print(f"[publication-lock] {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
