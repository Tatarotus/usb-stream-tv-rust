#!/usr/bin/env python3
"""Mixed media (Live TV + VOD) auto-generation and safe publication service.

Responsibilities
----------------
* Build the next generation package offline in isolated staging whenever the
  ready-VOD inventory or the approved Live configuration changes.
* Publish that package only with *proven* TV idleness: transfer it to the
  tablet, prove both tunnels and real VOD byte delivery, activate it under the
  package supervisor and confirm on the tablet that exactly the expected
  generation and manifest are the active ones before anything is marked ACTIVE.
* Never interrupt playback, never switch the LUN while the state is unknown and
  never promote a generation that was not confirmed by the tablet.

Failure model
-------------
Publication is journalled in the state file (phase + generation + manifest id +
staging dir). A restart resumes from the last confirmed phase: the tablet is
re-verified first, and an activation that cannot be confirmed is rolled back
instead of being recorded as successful.

Concurrency
-----------
All state mutations happen under an exclusive ``flock`` on ``<state>.lock``, so
two instances cannot build, promote or publish the same generation at once.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
import math
import atexit
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from subprocess import CompletedProcess, run as subprocess_run
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]

# Operational volume for a mixed generation.
#
# The 128 GiB baseline cannot hold the 29-channel grid: 29 Live x 4 GB plus the
# VOD catalog is ~197 GiB of logical media. The patched fuse_ntfs reports the
# volume from the NTFS BPB (instead of a hardcoded 128 GiB) and validates the
# manifest capacity against this same value, so the two must agree.
#
# This lives in the source, not only in the systemd environment, on purpose:
# the bridge re-executes this script per request and the child inherits the
# daemon's environment, so an env-only change would need a bridge restart —
# which tears down the processes serving the TV. `MIXED_VOLUME_BYTES` stays as
# an override for tests and one-off runs.
OPERATIONAL_VOLUME_BYTES = int(os.environ.get("MIXED_VOLUME_BYTES") or 274877906944)
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import build_mixed_media_package as pkg_builder  # noqa: E402
import vod_revision  # noqa: E402
from mixed_ssh_transfer import push_via_ssh  # noqa: E402
from mixed_generation_contract import validate_generation  # noqa: E402

try:
    import publication_lock  # noqa: E402
except ImportError as _lock_import_error:  # pragma: no cover - deployment guard
    publication_lock = None
    _LOCK_IMPORT_ERROR: str | None = str(_lock_import_error)
else:
    _LOCK_IMPORT_ERROR = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("mixed_gen_service")

# Generation artifacts that must exist and be covered by SHA256SUMS.
REQUIRED_ARTIFACTS = tuple(pkg_builder.ARTIFACTS)
# Runtime files that must be present before a package may be published.
REQUIRED_RUNTIME = (
    "fuse_ntfs",
    "sparse_unpack",
    "activate_live_favorites_tablet.sh",
    "transport.token",
    "chisel.auth",
)
RUNTIME_SOURCES = {
    "activate_live_favorites_tablet.sh": ROOT / "scripts" / "activate_live_favorites_tablet.sh",
}
DEFAULT_BINARIES_DIR = ROOT / "target" / "armv7-unknown-linux-gnueabihf" / "release"

# Every field a status payload must carry before idleness can even be assessed.
# A missing, malformed or stale field is treated as playback, never as idle:
# absence of telemetry is not evidence that the TV is free.
ESSENTIAL_STATUS_TYPES = {
    "active_vod": (str, type(None)),
    "tv_on": (bool,),
    "tv_usb_state": (str,),
    "in_standby": (bool,),
    "listeners": (int,),
    "stream_bitrate_kbps": (int, float),
    "tablet_online": (bool,),
    "server_time": (int, float),
}
IDLE_USB_STATE = "DISCONNECTED"
# Registry key of an inventory-only refresh (no single video to anchor on).
PUBLISH_BLOCKED_MESSAGES = {
    "stale_for_export": "O pacote foi montado para outra grade de canais; "
                        "toque em NA TV para preparar de novo",
    "stale_for_inventory": "O catálogo mudou depois da preparação; toque em NA TV de novo",
    "inventory_unavailable": "Não foi possível conferir o catálogo; "
                             "nada foi transferido para a TV",
}

REFRESH_KEY = "inventory"
_SESSION_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_SHA_LINE_RE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9_./-]{1,128})$")


class StateLockedError(RuntimeError):
    """Another instance currently owns the state file lock."""


class PublicationError(RuntimeError):
    """Publication could not proceed; the journal records the reason."""


class TabletCommandError(PublicationError):
    """A tablet-side command failed."""


class SubprocessRunner:
    """Default command runner: real processes, no shell interpolation."""

    def __call__(self, argv: list[str], timeout: float) -> CompletedProcess:
        return subprocess_run(argv, capture_output=True, text=True, timeout=timeout)


class TabletTransport:
    """Executes tablet commands, optionally through an ssh hop, without secrets.

    * ``adb_serial`` is the adb target used on the machine that owns the ADB
      connection (``ssh_host`` when remote).
    * No credential is ever placed on a command line; the package's private
      files stay in the package directory on the device.
    """

    def __init__(self, *, ssh_host: str | None = None, adb_serial: str = "127.0.0.1:25555",
                 base: str = "/data/local/tmp", runner=None, dry_run: bool = False,
                 timeout: float = 300.0):
        if not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,128}", adb_serial):
            raise ValueError("invalid adb serial")
        self.ssh_host = ssh_host
        self.adb_serial = adb_serial
        self.base = base
        self.runner = runner or SubprocessRunner()
        self.dry_run = dry_run
        self.timeout = timeout

    # -- command construction -------------------------------------------------
    def _wrap(self, adb_argv: list[str]) -> list[str]:
        if self.ssh_host:
            return ["ssh", "-o", "BatchMode=yes", self.ssh_host, " ".join(_quote(a) for a in adb_argv)]
        return adb_argv

    def adb(self, *args: str, check: bool = True, timeout: float | None = None) -> CompletedProcess:
        argv = self._wrap(["adb", "-s", self.adb_serial, *args])
        return self._run(argv, check=check, timeout=timeout)

    def root_shell(self, command: str, *, check: bool = True,
                   timeout: float | None = None) -> CompletedProcess:
        """Run ``command`` as root on the device through ``adb shell su -c``."""
        return self.adb("shell", "su", "-c", command, check=check, timeout=timeout)

    def push(self, local_dir: Path, remote_dir: str, *, check: bool = True) -> CompletedProcess:
        if self.ssh_host:
            return push_via_ssh(self, local_dir, remote_dir, check=check)
        return self.adb("push", "--sync", str(local_dir) + "/.", remote_dir.rstrip("/") + "/",
                        check=check)

    def _run(self, argv: list[str], *, check: bool, timeout: float | None) -> CompletedProcess:
        if self.dry_run:
            logger.info("dry-run: %s", " ".join(argv))
            return CompletedProcess(argv, 0, "", "")
        try:
            completed = self.runner(argv, timeout or self.timeout)
        except Exception as exc:  # pragma: no cover - exercised through fakes
            raise TabletCommandError(f"command failed to start: {type(exc).__name__}") from exc
        if check and completed.returncode != 0:
            raise TabletCommandError(
                f"tablet command failed (rc={completed.returncode})")
        return completed


_REDACT_HEX32 = re.compile(r"\b[0-9a-f]{32}\b")


def sanitize_tablet_output(text: str, limit: int = 200) -> str:
    """Tablet output reduced to something safe to log and to journal.

    Tokens never belong in a diagnostic: any 32-hex run (handoff/guard tokens)
    is replaced before the text is stored or printed, and the result is trimmed
    to a bounded tail.
    """
    if not isinstance(text, str):
        return ""
    collapsed = " ".join(text.split())
    if not collapsed:
        return "(no output)"
    return _REDACT_HEX32.sub("<redacted>", collapsed)[-limit:]


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_sums(text: str) -> dict[str, str]:
    """Parse a SHA256SUMS file, rejecting anything that is not a plain digest."""
    entries: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _SHA_LINE_RE.fullmatch(line)
        if not match:
            raise PublicationError("malformed SHA256SUMS entry")
        name = match.group(2)
        if ".." in name.split("/") or name.startswith("/"):
            raise PublicationError("unsafe SHA256SUMS path")
        if name in entries:
            raise PublicationError(f"duplicate SHA256SUMS entry: {name}")
        entries[name] = match.group(1)
    return entries


class PreviousSupervisorTakeoverRefused(PublicationError):
    """The tablet refused the takeover before signalling anything.

    Nothing was switched, so this is a known failure; the tablet's own reason
    code must reach the journal instead of a generic message.
    """


class PreviousSupervisorNotUpgradable(PublicationError):
    """The running supervisor predates the handoff protocol.

    It would restore the legacy VOD mode when signalled and never acknowledge,
    so the switch must not be attempted: a tested, supervised upgrade of that
    session comes first.
    """


class ActivationUncertain(PublicationError):
    """The activation request may have been delivered; the answer was lost."""


class ActivationNotStarted(PublicationError):
    """The tablet never began the supervised activation.

    Proven, not guessed: either it refused to detach the start, or the log the
    device creates before running it never appeared. Undoing the server-side
    switch is therefore safe.
    """


class TakeoverUncertain(PublicationError):
    """The tablet may or may not have completed the takeover.

    Its supervisor is gone but no acceptance record proves the handoff, so the
    only honest reading is "unknown": neither success nor a clean refusal.
    """


class GuardLost(PublicationError):
    """The exclusive server-side guard was lost; nothing may proceed."""


class GuardKeeper:
    """Keeps the exclusive mode guard alive during long, multi-step work.

    The guard TTL is bounded (15-120 s) while a package transfer or a generation
    switch can take longer. A background renewer maintains ownership and records
    the first loss; ``ensure()`` re-validates right before every mutating step
    and right after the long ones, so the publisher never performs a switch or a
    LUN activation after losing exclusivity.
    """

    def __init__(self, service: "MixedGenerationService", token: str, *,
                 interval: float | None = None, extra=None):
        self.service = service
        self.token = token
        # Optional second ownership that must be renewed in lockstep (the
        # tablet handoff lease). It raises on loss, which aborts the section.
        self.extra = extra
        default = max(0.5, float(getattr(service, "guard_ttl", 60)) / 3.0)
        self.interval = max(0.02, float(interval if interval is not None else default))
        self.lost = False
        self.renewals = 0
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "GuardKeeper":
        self._thread = threading.Thread(target=self._loop, name="mode-guard-keeper",
                                        daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            if not self._renew():
                return

    def _renew(self) -> bool:
        try:
            ok = bool(self.service.renew_guard(self.token))
        except PublicationError as exc:
            ok = False
            self.last_error = str(exc)
        if ok and self.extra is not None:
            try:
                self.extra()
            except PublicationError as exc:
                ok = False
                self.last_error = str(exc)
        if ok:
            self.renewals += 1
            return True
        if not self.lost:
            self.lost = True
            logger.warning("mode guard lost; aborting the publication")
        return False

    def ensure(self, phase: str) -> None:
        if self.lost or not self._renew():
            self.lost = True
            raise GuardLost(f"mode guard lost before {phase}")

    def stop(self) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval * 3))
        return not self.lost


class MixedGenerationService:
    def __init__(
        self,
        *,
        server_url: str = "http://127.0.0.1:8080",
        pin: str = "",
        export_config_path: Path = SCRATCH / "grade-1080p/export-revisao-sol.json",
        staging_base: Path = SCRATCH / "mixed-staging",
        state_file: Path = SCRATCH / "mixed_generation_state.json",
        volume_bytes: int = 128 * 1024 * 1024 * 1024,
        binaries_dir: Path | None = None,
        private_dir: Path | None = None,
        transport: TabletTransport | None = None,
        live_port: int = 18190,
        vod_port: int = 18191,
        status_max_age: float = 60.0,
        publication_timeout: float = 120.0,
        poll_interval: float = 2.0,
        guard_owner: str = "mixed-publisher",
        guard_ttl: int = 60,
        vps_switch_script: Path | None = None,
        vps_switch_root: str | None = None,
        vps_upstream: str | None = None,
        vps_app_dir: str | None = None,
        vps_package_root: str | None = None,
        vps_switch_script_remote: str | None = None,
        rust_validate: bool = False,
        now=time.time,
    ):
        self.server_url = server_url.rstrip("/")
        self.pin = pin
        self.export_config_path = Path(export_config_path)
        self.staging_base = Path(staging_base)
        self.staging_base.mkdir(parents=True, exist_ok=True)
        self.state_file = Path(state_file)
        self.volume_bytes = volume_bytes
        self.binaries_dir = Path(binaries_dir) if binaries_dir else DEFAULT_BINARIES_DIR
        self.private_dir = Path(private_dir) if private_dir else None
        self.transport = transport
        self.live_port = int(live_port)
        self.vod_port = int(vod_port)
        self.status_max_age = float(status_max_age)
        if not math.isfinite(self.status_max_age) or self.status_max_age <= 0:
            raise ValueError("status_max_age must be finite and positive")
        self.publication_timeout = float(publication_timeout)
        self.poll_interval = float(poll_interval)
        self.guard_owner = guard_owner
        self.guard_ttl = max(15, min(120, int(guard_ttl)))
        self.vps_switch_script = (
            Path(vps_switch_script) if vps_switch_script
            else Path(os.environ.get("MIXED_VPS_SWITCH_SCRIPT", str(ROOT / "scripts"
                                                                  / "switch_mixed_generation_vps.sh"))))
        self.vps_switch_root = vps_switch_root or os.environ.get("MIXED_VPS_SWITCH_ROOT")
        self.vps_upstream = vps_upstream or os.environ.get("MIXED_VPS_UPSTREAM")
        self.vps_app_dir = vps_app_dir or os.environ.get("MIXED_VPS_APP_DIR")
        self.vps_package_root = (vps_package_root
                                 or os.environ.get("MIXED_VPS_PACKAGE_ROOT")
                                 or "/var/lib/usb-stream-tv/generations")
        self.vps_switch_script_remote = (vps_switch_script_remote
                                         or os.environ.get("MIXED_VPS_SWITCH_SCRIPT_REMOTE")
                                         or "scripts/switch_mixed_generation_vps.sh")
        self.rust_validate = bool(rust_validate)
        self._now = now
        self._lock_fd = None
        self._lock_depth = 0
        self._thread_lock = threading.RLock()
        self.state = self._load_state()
        self._saved_digest = self._state_digest()

    # ------------------------------------------------------------------ state
    def _state_digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.state, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _load_state(self) -> dict:
        state = {}
        try:
            payload = self.state_file.read_text()
        except FileNotFoundError:
            payload = None
        except OSError as exc:
            raise PublicationError("cannot read the publication journal") from exc
        if payload is not None:
            try:
                loaded = json.loads(payload)
            except Exception as exc:
                # Fail closed: acting on a corrupt journal could republish a
                # generation whose real state is unknown.
                raise PublicationError("publication journal is corrupt") from exc
            if not isinstance(loaded, dict):
                raise PublicationError("publication journal must be an object")
            state = loaded
        base = {
            "active_generation": 1,
            "active_manifest_id": None,
            "active_inventory_hash": None,
            "active_session": None,
            "active_index": None,
            "pending_generation": None,
            "pending_staging_dir": None,
            "pending_inventory_hash": None,
            "pending_manifest_id": None,
            "pending_session": None,
            "pending_index": None,
            "publication": None,
            "last_check_ts": 0,
        }
        base.update(state)
        return base

    def _save_state(self) -> None:
        """Atomic save: unique temporary file, fsync, then replace."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_name(
                f".{self.state_file.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
            payload = json.dumps(self.state, indent=2, sort_keys=True) + "\n"
            with tmp.open("w") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.state_file)
            dir_fd = os.open(self.state_file.parent, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception as exc:
            # A publication journal that cannot be persisted must never be
            # treated as written: raise so the caller aborts instead.
            raise PublicationError("cannot durably save the publication journal") from exc
        self._saved_digest = self._state_digest()

    def _reload_if_changed_by_peer(self) -> None:
        """Detect a commit made by another instance after this one loaded.

        The in-memory copy stays authoritative for this instance, but a peer
        that committed a different state must not be silently overwritten and a
        state that is neither ours nor the peer's is a hard conflict.
        """
        try:
            payload = self.state_file.read_text()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise PublicationError("cannot read the publication journal") from exc
        try:
            peer = json.loads(payload)
        except Exception as exc:
            raise PublicationError("publication journal is corrupt") from exc
        if not isinstance(peer, dict):
            raise PublicationError("publication journal must be an object")
        peer_digest = hashlib.sha256(
            json.dumps(peer, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if peer_digest in (self._saved_digest, self._state_digest()):
            return
        if self._state_digest() != self._saved_digest:
            raise PublicationError("state changed both locally and by a peer")
        base = self._load_state()
        self.state = base
        self._saved_digest = peer_digest
        logger.info("state refreshed from a peer commit")

    @contextmanager
    def state_lock(self):
        """Exclusive lock over every state mutation.

        Thread-safe (one thread at a time per instance, re-entrant for the
        owner thread) and process-safe (``flock``). A peer commit made after
        this instance loaded is detected before any mutation.
        """
        if not self._thread_lock.acquire(blocking=False):
            raise StateLockedError("another thread owns the state lock")
        try:
            with self._process_state_lock():
                yield
        finally:
            self._thread_lock.release()

    @contextmanager
    def _process_state_lock(self):
        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        lock_path = self.state_file.with_name(self.state_file.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise StateLockedError(
                    "another mixed-generation instance owns the state lock") from exc
            self._reload_if_changed_by_peer()
            self._lock_fd = fd
            self._lock_depth = 1
            try:
                yield
            finally:
                self._lock_depth = 0
                self._lock_fd = None
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # --------------------------------------------------------------- telemetry
    def _fetch_json(self, path: str) -> dict | None:
        req = Request(f"{self.server_url}{path}")
        if self.pin:
            req.add_header("X-Auth-PIN", self.pin)
        try:
            with urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.debug("Could not reach %s: %s", path, exc)
        return None

    def fetch_vod_status(self) -> dict | None:
        return self._fetch_json("/api/vod/status")

    def fetch_server_status(self) -> dict | None:
        return self._fetch_json("/api/status")

    def is_playback_active(self, server_status: dict | None = None,
                           *, now: float | None = None) -> tuple[bool, str]:
        """Strict, fail-closed playback detection.

        Returns ``(True, reason)`` whenever playback may be in progress, the
        payload is incomplete/malformed/stale, or the tablet telemetry is
        missing. Only a fully populated, fresh payload proving an idle,
        disconnected, unpowered TV returns ``(False, "idle_tv_off")``.
        """
        if server_status is None:
            server_status = self.fetch_server_status()
        if not server_status or not isinstance(server_status, dict):
            return (True, "server_unreachable_fail_closed")

        for field, types in ESSENTIAL_STATUS_TYPES.items():
            if field not in server_status:
                return (True, f"missing_telemetry:{field}")
            value = server_status[field]
            if isinstance(value, bool) and bool not in types:
                return (True, f"invalid_telemetry:{field}")
            if not isinstance(value, types):
                return (True, f"invalid_telemetry:{field}")

        reference = self._now() if now is None else now
        try:
            reference = float(reference)
            timestamp = float(server_status["server_time"])
            bitrate = float(server_status["stream_bitrate_kbps"])
            listeners = int(server_status["listeners"])
            max_age = float(self.status_max_age)
        except (TypeError, ValueError, OverflowError):
            return (True, "invalid_numeric_telemetry_fail_closed")
        if not all(math.isfinite(value)
                   for value in (reference, timestamp, bitrate, max_age)):
            return (True, "nonfinite_telemetry_fail_closed")
        if max_age <= 0 or listeners < 0 or bitrate < 0:
            return (True, "invalid_numeric_telemetry_fail_closed")
        age = reference - timestamp
        if age > max_age:
            return (True, "stale_status_fail_closed")
        if age < -max_age:
            return (True, "skewed_status_fail_closed")

        if server_status["tablet_online"] is not True:
            return (True, "tablet_telemetry_offline_fail_closed")

        active_vod = server_status["active_vod"]
        if active_vod is not None:
            return (True, f"active_vod_selected:{active_vod}")

        # Any measured stream activity blocks, even without a counted listener.
        if listeners > 0 or bitrate > 0:
            return (True, f"active_live_stream:listeners={listeners},bitrate={bitrate:.0f}kbps")

        tv_on = bool(server_status["tv_on"])
        usb_state = str(server_status["tv_usb_state"])
        # A powered TV with the virtual pendrive mounted is the NORMAL state in
        # which the user stops the video, taps the button and then browses the
        # folders on the TV. It is neither proof of playback nor treated as
        # playback: every measurable reproduction signal above (active VOD,
        # listeners, bitrate) already blocked. Any other USB state other than a
        # plain disconnect stays fail-closed, even with the TV powered on.
        if usb_state == "CONFIGURED":
            return (False, "idle_tv_on_no_playback")
        if usb_state != IDLE_USB_STATE:
            return (True, f"unknown_usb_state_fail_closed:{usb_state or 'empty'}")
        if tv_on:
            # Powered TV with the pendrive judged disconnected: nothing from the
            # virtual drive can be playing, so it is switch-safe like standby.
            return (False, "idle_tv_on_no_playback")

        # ``in_standby`` is informational: the IPTV FFmpeg stops by itself after
        # STANDBY_TIMEOUT without listeners, so it cannot be playback. It must
        # still be present and well-typed (checked above).
        return (False, "idle_tv_off")

    UNKNOWN_PLAYBACK_PREFIXES = (
        "server_unreachable", "missing_telemetry", "invalid_", "nonfinite_",
        "stale_", "skewed_", "tablet_telemetry_offline", "unknown_usb_state",
        "invalid_numeric_telemetry",
    )

    # Legacy signals do not prove that the mixed pendrive is being read. The
    # explicit manual request also authorises proceeding with unknown telemetry.
    INTERRUPTIBLE_PLAYBACK_PREFIXES = (
        "active_vod_selected:", "active_live_stream:",
    )

    @classmethod
    def may_interrupt_playback(cls, reason: str, *, manual: bool,
                               confirm_interrupt: bool) -> bool:
        """Only an explicitly authorised manual request may override telemetry.

        The automatic path can never override, and unrecognised reasons remain
        blocked. Transport, guard and publication checks remain independent.
        """
        return bool(manual and confirm_interrupt
                    and isinstance(reason, str)
                    and reason.startswith(cls.INTERRUPTIBLE_PLAYBACK_PREFIXES
                                          + cls.UNKNOWN_PLAYBACK_PREFIXES))

    def playback_state(self, server_status: dict | None = None, *,
                       now: float | None = None) -> tuple[str, str]:
        """Three-way classification: ``idle`` | ``playing`` | ``unknown``.

        Missing, stale or malformed telemetry and an offline tablet are
        classified as ``unknown``. Callers separately check authorisation and
        the transport and publication safeguards.
        """
        active, reason = self.is_playback_active(server_status, now=now)
        if not active:
            return ("idle", reason)
        if reason.startswith(self.UNKNOWN_PLAYBACK_PREFIXES):
            return ("unknown", reason)
        return ("playing", reason)

    def playback_status(self, server_status: dict | None = None) -> tuple[bool, str]:
        return self.is_playback_active(server_status)

    # ------------------------------------------------------------- fingerprint
    @staticmethod
    def _task_fingerprint(task: dict) -> str:
        revision = vod_revision.content_revision(task)
        parts = [
            str(task.get("id") or ""),
            str(task.get("file_size") or task.get("file_size_bytes") or 0),
            str(task.get("title") or ""),
            str(task.get("display_name") or ""),
            str(task.get("series_id") or ""),
            str(task.get("series_title") or ""),
            str(task.get("season_num") or ""),
            str(task.get("episode_num") or ""),
            str(task.get("storage") or "local"),
            str(task.get("remote_path") or ""),
            revision or "revision-unknown",
        ]
        return ":".join(parts)

    def _live_fingerprint(self, export_config: dict | None) -> str:
        if not export_config or not isinstance(export_config, dict):
            return ""
        parts = []
        for item in export_config.get("identities", []) or []:
            if not isinstance(item, dict) or item.get("status") != "approved":
                continue
            chosen = item.get("chosen") or {}
            parts.append(":".join([
                str(item.get("identity") or ""),
                str(item.get("category") or ""),
                str(item.get("filename") or ""),
                str(chosen.get("channel_id") or ""),
            ]))
        parts.sort()
        return hashlib.sha256(";".join(parts).encode("utf-8")).hexdigest()[:16]

    def compute_inventory_hash(self, tasks: list[dict], export_config: dict | None = None) -> str:
        """Deterministic fingerprint of the ready inventory and Live config.

        Covers task identity, size, both series identifiers, the trusted content
        revision and the approved Live channels. Only ``status == "ready"``
        tasks take part, so a finished download changes the fingerprint while a
        running one does not.
        """
        ready = [self._task_fingerprint(t) for t in tasks if t.get("status") == "ready"]
        ready.sort()

        if export_config is None and self.export_config_path.is_file():
            try:
                export_config = json.loads(self.export_config_path.read_text())
            except Exception:
                export_config = None

        combined = f"live={self._live_fingerprint(export_config)}|" + ",".join(ready)
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------ preparation
    def session_name(self, generation: int, inventory_hash: str) -> str:
        name = f"mixed-gen{int(generation)}-{inventory_hash[:8]}"
        if not _SESSION_RE.fullmatch(name):
            raise PublicationError("invalid session name")
        return name

    def _runtime_sources(self) -> dict[str, Path]:
        sources: dict[str, Path] = dict(RUNTIME_SOURCES)
        sources["fuse_ntfs"] = self.binaries_dir / "fuse_ntfs"
        sources["sparse_unpack"] = self.binaries_dir / "sparse_unpack"
        if self.private_dir:
            sources["transport.token"] = self.private_dir / "transport.token"
            sources["chisel.auth"] = self.private_dir / "chisel.auth"
        return sources

    def check_and_prepare_generation(self, tasks: list[dict],
                                     export_config: dict | None = None) -> dict | None:
        """Build the next generation in staging when the inventory changed."""
        with self.state_lock():
            return self._prepare_locked(tasks, export_config)

    def _prepare_locked(self, tasks: list[dict], export_config: dict | None) -> dict | None:
        current_hash = self.compute_inventory_hash(tasks, export_config)
        self.state["last_check_ts"] = self._now()

        if current_hash == self.state.get("active_inventory_hash"):
            return None

        if current_hash == self.state.get("pending_inventory_hash"):
            logger.info("Pending generation already prepared for current inventory.")
            return self.state

        next_gen = int(self.state.get("active_generation", 1)) + 1
        session = self.session_name(next_gen, current_hash)
        staging_dir = self.staging_base / session

        # A previous attempt for this generation must not leave stale bytes in
        # the package: rebuild from scratch.
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)

        if export_config is None:
            if not self.export_config_path.is_file():
                logger.error("Export config not found: %s", self.export_config_path)
                return None
            export_config = json.loads(self.export_config_path.read_text())

        logger.info("Building generation %s package in staging: %s", next_gen, staging_dir)
        try:
            sources = self._runtime_sources()
            binaries_dir = self.binaries_dir if self.binaries_dir.is_dir() else None
            summary = pkg_builder.build_package(
                vod_tasks=tasks,
                export_config=export_config,
                staging_dir=staging_dir,
                volume_bytes=self.volume_bytes,
                generation=next_gen,
                binaries_dir=binaries_dir,
                runtime_files=sources,
            )
        except Exception as exc:
            logger.error("Failed to build package for generation %s: %s", next_gen, exc)
            shutil.rmtree(staging_dir, ignore_errors=True)
            return None

        missing = [name for name in REQUIRED_RUNTIME if name not in summary.get("hashes", {})]
        try:
            pending_index = self.generation_index(staging_dir)
        except (OSError, ValueError, KeyError, TypeError):
            pending_index = {}
        self.state["pending_generation"] = next_gen
        self.state["pending_staging_dir"] = str(staging_dir)
        self.state["pending_inventory_hash"] = current_hash
        self.state["pending_manifest_id"] = summary["manifest_id"]
        self.state["pending_session"] = session
        self.state["pending_index"] = pending_index
        self.state["publication"] = {
            "generation": next_gen,
            "manifest_id": summary["manifest_id"],
            "inventory_hash": current_hash,
            "staging_dir": str(staging_dir),
            "session": session,
            "phase": "prepared" if not missing else "incomplete",
            "missing": missing,
            "omitted_count": int(summary.get("omitted_count") or 0),
            "omitted_tasks": list(summary.get("omitted_tasks") or []),
            "attempts": 0,
            "last_error": None,
            "created_ts": self._now(),
            "updated_ts": self._now(),
        }
        self._save_state()
        logger.info(
            "Generation %s %s (%s files, free: %.2f GiB%s)",
            next_gen, "prepared" if not missing else "incomplete",
            summary.get("total_files"), summary.get("free_bytes", 0) / 1024**3,
            "" if not missing else f", missing: {','.join(missing)}")
        return summary

    # ---------------------------------------------------------- integrity gate
    def staging_is_complete(self, staging_dir: str | Path,
                            required_extra: tuple[str, ...] = ()) -> tuple[bool, str]:
        """Prove the staging package is complete and byte-identical to its sums."""
        path = Path(staging_dir)
        if not path.is_dir():
            return False, "staging_dir_missing"
        sums = path / "SHA256SUMS"
        if not sums.is_file():
            return False, "sha256sums_missing"
        text = sums.read_text()
        if not text.strip():
            return False, "sha256sums_empty"
        try:
            entries = parse_sums(text)
        except PublicationError as exc:
            return False, f"sha256sums_invalid:{exc}"
        for name in list(REQUIRED_ARTIFACTS) + list(REQUIRED_RUNTIME) + list(required_extra):
            if name not in entries:
                return False, f"not_hashed:{name}"
            if not (path / name).is_file():
                return False, f"file_missing:{name}"
        for name, digest in entries.items():
            target = path / name
            if not target.is_file():
                return False, f"listed_file_missing:{name}"
            if sha256_file(target) != digest:
                return False, f"hash_mismatch:{name}"
        return True, "ok"

    def manifest_identity(self, staging_dir: str | Path) -> tuple[int, str]:
        """Read (generation, manifest_id) from the canonical manifest file."""
        path = Path(staging_dir) / "favorites_manifest.json"
        data = json.loads(path.read_text())
        generation = int(data["generation"])
        manifest_id = str(data["manifest_id"])
        recomputed = hashlib.sha256(_canonical_manifest_bytes(data)).hexdigest()
        if recomputed != manifest_id:
            raise PublicationError("manifest_id does not match canonical manifest bytes")
        return generation, manifest_id

    # ------------------------------------------------------------ mode guard
    def guard_request(self, action: str, *, token: str | None = None,
                      ttl: int | None = None, phase: str | None = None) -> dict:
        """Talk to the server-side exclusive mode guard (never logs the token)."""
        body = {"action": action, "owner": self.guard_owner, "ttl": ttl or self.guard_ttl}
        if token:
            body["token"] = token
        if phase:
            body["phase"] = phase
        payload = json.dumps(body).encode("utf-8")
        req = Request(f"{self.server_url}/api/mode_guard", data=payload, method="POST",
                      headers={"Content-Type": "application/json"})
        if self.pin:
            req.add_header("X-Auth-PIN", self.pin)
        try:
            with urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8"))
            except Exception:
                data = {"ok": False, "error": f"guard_http_{exc.code}"}
        except (URLError, OSError, ValueError, TimeoutError) as exc:
            # A guard that cannot be reached is unknown: never treat it as free.
            raise PublicationError(f"mode guard unreachable: {type(exc).__name__}") from exc
        if not isinstance(data, dict):
            raise PublicationError("mode guard returned an invalid response")
        return data

    def acquire_guard(self, *, phase: str = "publishing") -> dict:
        data = self.guard_request("acquire", ttl=self.guard_ttl, phase=phase)
        if data.get("ok") and isinstance(data.get("token"), str):
            return data
        if data.get("busy"):
            holder = data.get("holder") or {}
            raise PublicationError(
                f"mode guard busy: owner={holder.get('owner')} phase={holder.get('phase')}")
        raise PublicationError(f"mode guard refused: {data.get('error') or 'unknown'}")

    def renew_guard(self, token: str) -> bool:
        data = self.guard_request("renew", token=token)
        return bool(data.get("ok"))

    def release_guard(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            data = self.guard_request("release", token=token)
        except PublicationError:
            return False
        return bool(data.get("ok"))

    def guard_status(self) -> dict:
        data = self.guard_request("status")
        return data

    # ------------------------------------------------------ generation index
    @staticmethod
    def generation_index(staging_dir: str | Path) -> dict:
        """``{task_id: {"size","revision","path"}}`` for a generation's VODs."""
        manifest = json.loads((Path(staging_dir) / "favorites_manifest.json").read_text())
        index: dict[str, dict] = {}
        for entry in manifest.get("entries", []):
            if entry.get("kind") != "vod":
                continue
            index[str(entry.get("channel_id"))] = {
                "size": int(entry.get("logical_size") or 0),
                "revision": str(entry.get("content_revision") or ""),
                "path": "/".join(list(entry.get("folder") or []) + [str(entry.get("filename") or "")]),
            }
        return index

    def index_contains(self, index: dict | None, task: dict) -> bool:
        """True when the recorded generation already holds these exact bytes."""
        if not isinstance(index, dict):
            return False
        recorded = index.get(str(task.get("id") or ""))
        if not recorded:
            return False
        revision = vod_revision.content_revision(task)
        size = int(task.get("file_size") or task.get("file_size_bytes") or 0)
        return (int(recorded.get("size") or 0) == size
                and str(recorded.get("revision") or "") == str(revision or ""))

    # ------------------------------------------------------------- manual apply
    def apply_task(self, task_id: str, *, tasks: list[dict] | None = None,
                   manual: bool = True, confirm_interrupt: bool = False) -> dict:
        """Apply the generation that contains ``task_id`` (manual, guarded).

        Idempotent: a second call while the same task is being applied reports
        the in-flight state instead of starting another publication.
        ``confirm_interrupt`` records that the manual request authorises an
        interruption, including when playback telemetry is unknown.
        """
        if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{8}", task_id):
            return {"state": "failed", "reason": "invalid_task_id",
                    "message": "Vídeo inválido", "generation": None,
                    "interrupts_playback": False}
        with self.state_lock():
            result = self._apply_locked(task_id, tasks=tasks, manual=manual,
                                        confirm_interrupt=confirm_interrupt)
            self._record_apply(task_id, result)
            return result

    def refresh_inventory(self, *, tasks: list[dict] | None = None,
                          manual: bool = True,
                          confirm_interrupt: bool = False) -> dict:
        """Publish the CURRENT inventory, with or without a specific video.

        Deleting the last ready VOD (or only excluded videos) leaves no task to
        anchor ``apply_task`` on, yet the TV list still has to lose the file.
        This path has no task identity at all: it compares the inventory
        fingerprint, prepares a generation when it differs and publishes it.
        The registry entry is keyed by ``inventory`` instead of a task id so the
        dashboard can follow it through the same status endpoint.
        """
        with self.state_lock():
            result = self._refresh_locked(tasks=tasks, manual=manual,
                                          confirm_interrupt=confirm_interrupt)
            self._record_apply(REFRESH_KEY, result)
            return result

    def _refresh_locked(self, *, tasks: list[dict] | None, manual: bool,
                        confirm_interrupt: bool) -> dict:
        if tasks is None:
            status = self.fetch_vod_status()
            # An unreachable or malformed catalog is NOT an empty catalog: the
            # difference is the whole TV list. Treating a failure as "no videos"
            # would publish a generation that removes everything.
            if not isinstance(status, dict) or not isinstance(status.get("tasks"), list):
                self._update_journal(last_apply_state="unknown",
                                     last_blocked_reason="inventory_unavailable")
                return {"state": "unknown", "generation": self.state.get("active_generation"),
                        "interrupts_playback": False, "reconnected": False,
                        "confirmed": None, "reason": "inventory_unavailable",
                        "message": "⚠️ Não foi possível ler o catálogo; nada foi alterado."}
            tasks = status["tasks"]
        if not isinstance(tasks, list):
            return {"state": "unknown", "generation": self.state.get("active_generation"),
                    "interrupts_playback": False, "reconnected": False,
                    "confirmed": None, "reason": "inventory_unavailable",
                    "message": "⚠️ Catálogo inválido; nada foi alterado."}
        tasks = [t for t in tasks if isinstance(t, dict)]
        journal = self._journal()
        if journal.get("phase") in ("publishing", "transferred", "activating"):
            requested = str(journal.get("requested_task") or "")
            if requested and requested != REFRESH_KEY:
                return self._apply_result(
                    "blocked", journal, task_id=REFRESH_KEY,
                    reason="another_task_in_progress",
                    message="Outra atualização está em andamento")
            # Same kind of update already running: a second click must report it
            # instead of starting another publication.
            return self._apply_result(
                "in_progress", journal, task_id=REFRESH_KEY,
                message="Atualização da lista já está em andamento")

        current_hash = self.compute_inventory_hash(tasks)
        active_hash = self.state.get("active_inventory_hash")
        if active_hash and current_hash == active_hash:
            # The published generation already matches the live inventory. The
            # tablet still has to prove it, because the journal can be stale
            # after a rollback or a disconnection.
            confirmation = self.confirm_inventory(tasks=tasks)
            if confirmation.get("confirmed") is True:
                self._update_journal(last_apply_state="already_current")
                return {"state": "already_current",
                        "generation": self.state.get("active_generation"),
                        "interrupts_playback": False, "reconnected": False,
                        "confirmed": True,
                        "message": "✅ Lista da TV já corresponde ao catálogo",
                        "reason": None}
            if confirmation.get("confirmed") is None:
                self._update_journal(last_apply_state="unknown",
                                     last_blocked_reason="tablet_unconfirmed")
                return {"state": "unknown",
                        "generation": self.state.get("active_generation"),
                        "interrupts_playback": False, "reconnected": False,
                        "confirmed": False,
                        "message": "⚠️ Não foi possível confirmar a lista na TV. "
                                   "Nada foi alterado.",
                        "reason": confirmation.get("reason") or "tablet_unconfirmed"}
            # Different list on the tablet: republish.

        if journal.get("phase") == "unconfirmed" and not manual:
            return self._apply_result("unconfirmed", journal, task_id=REFRESH_KEY,
                                      reason="manual_reconciliation_required",
                                      message="Ativação anterior não confirmada; reconciliação manual necessária")

        if not manual:
            # Preparation is automatic; PUBLICATION is only ever authorised by
            # the manual click. A daemon cycle must never reconnect the TV.
            self._update_journal(last_apply_state="prepared")
            return {"state": "prepared", "generation": self.state.get("pending_generation"),
                    "interrupts_playback": False, "reconnected": False,
                    "confirmed": None, "reason": "manual_publication_required",
                    "message": "Lista preparada; toque em NA TV para aplicar"}

        playback, playback_reason = self.playback_state()
        if playback != "idle":
            if self.may_interrupt_playback(playback_reason, manual=manual,
                                           confirm_interrupt=confirm_interrupt):
                self._update_journal(interrupted_legacy=playback_reason,
                                     interrupt_confirmed_ts=self._now())
            else:
                self._update_journal(last_apply_state="blocked",
                                     last_blocked_reason=playback_reason)
                return self._apply_result(
                    "blocked", self._journal(), task_id=REFRESH_KEY,
                    reason=playback_reason, message="TV em uso; confirme a interrupção",
                    interruptible=self.may_interrupt_playback(
                        playback_reason, manual=manual, confirm_interrupt=True))

        self._mark_apply_in_progress(REFRESH_KEY)
        prepared = self.check_and_prepare_generation(tasks)
        pending = self._journal()
        if not prepared and not pending.get("generation"):
            return self._apply_result("failed", pending, task_id=REFRESH_KEY,
                                      reason="prepare_failed",
                                      message="Não foi possível preparar a lista")
        # The catalog can change while the package is being built. Publishing a
        # stale generation would show a video the user just deleted, so the
        # fingerprint is re-read and, when it moved, the package is rebuilt once
        # before anything touches the tablet.
        if tasks is not None:
            confirmed_tasks = self._refetch_tasks()
            if confirmed_tasks is not None:
                confirmed_hash = self.compute_inventory_hash(confirmed_tasks)
                if confirmed_hash != pending.get("inventory_hash"):
                    logger.info("inventory changed while preparing; rebuilding once")
                    tasks = confirmed_tasks
                    self._mark_apply_in_progress(REFRESH_KEY)
                    prepared = self.check_and_prepare_generation(tasks)
                    pending = self._journal()
                    if not prepared and not pending.get("generation"):
                        return self._apply_result(
                            "failed", pending, task_id=REFRESH_KEY,
                            reason="prepare_failed",
                            message="Não foi possível preparar a lista")
                    if self.compute_inventory_hash(tasks) != pending.get("inventory_hash"):
                        # It moved again: say so instead of publishing a list
                        # that is already out of date.
                        self._update_journal(last_apply_state="prepared")
                        return {"state": "prepared",
                                "generation": pending.get("generation"),
                                "interrupts_playback": False, "reconnected": False,
                                "confirmed": None, "reason": "inventory_changed",
                                "message": "O catálogo mudou durante a atualização; "
                                           "toque em NA TV de novo"}
        self.state["pending_index"] = self.generation_index(
            str(pending.get("staging_dir") or ""))
        self._save_state()
        result = self._publish_locked(manual=manual,
                                      confirm_interrupt=confirm_interrupt,
                                      tasks=tasks)
        state = result.get("state")
        final = state if state in ("active", "already_current", "blocked", "incomplete",
                                   "unconfirmed", "failed", "unknown") else "failed"
        messages = {
            "active": "✅ Lista da TV atualizada",
            "already_current": "✅ Lista da TV já corresponde ao catálogo",
            "blocked": "⏳ TV em uso; confirme a interrupção e tente de novo",
            "incomplete": "A lista ficou incompleta; veja pendências",
            "unconfirmed": "⚠️ Ativação não confirmada pela TV",
            "unknown": "⚠️ Estado da TV desconhecido; nada foi afirmado",
            "failed": "Falha ao atualizar a lista",
        }
        self._update_journal(last_apply_state=final,
                             interrupts_playback=self.reconnected_pendrive(final))
        return {"state": final, "generation": result.get("generation"),
                "interrupts_playback": self.reconnected_pendrive(final),
                "reconnected": self.reconnected_pendrive(final),
                "reason": result.get("reason"),
                "message": messages[final], "detail": result}

    def _record_apply(self, task_id: str, result: dict) -> None:
        """Top-level apply record consumed by the server/dashboard poller.

        It never contains a token, a PIN or a filesystem path.
        """
        self.state["apply"] = {
            "task_id": task_id,
            "state": str(result.get("state") or "failed"),
            "message": str(result.get("message") or ""),
            "reason": result.get("reason"),
            "interrupts_playback": bool(result.get("interrupts_playback")),
            "reconnected": (bool(result.get("reconnected"))
                            if result.get("reconnected") is not None
                            else bool(result.get("interrupts_playback"))),
            "generation": result.get("generation"),
            "confirmed": result.get("confirmed"),
            "updated_ts": self._now(),
        }
        self._save_state()

    def _mark_apply_in_progress(self, task_id: str) -> None:
        self.state["apply"] = {
            "task_id": task_id,
            "state": "in_progress",
            "message": "⏳ Atualizando a lista do pendrive…",
            "reason": None,
            "interrupts_playback": True,
            "generation": self.state.get("pending_generation"),
            "updated_ts": self._now(),
        }
        self._save_state()

    def _apply_locked(self, task_id: str, *, tasks: list[dict] | None,
                      manual: bool, confirm_interrupt: bool = False) -> dict:
        journal = self._journal()
        requested = journal.get("requested_task")
        if journal.get("phase") in ("publishing", "transferred", "activating") and requested == task_id:
            return self._apply_result("in_progress", journal, task_id=task_id,
                                      message="Atualização da lista já está em andamento")
        if journal.get("phase") in ("publishing", "transferred", "activating") and requested \
                and requested != task_id:
            return self._apply_result("blocked", journal, task_id=task_id,
                                      reason="another_task_in_progress",
                                      message="Outra atualização está em andamento")

        if tasks is None:
            status = self.fetch_vod_status()
            tasks = status.get("tasks", []) if status else []
        task = next((t for t in tasks if str(t.get("id")) == task_id), None)
        if not task or task.get("status") != "ready":
            return self._apply_result("failed", journal, task_id=task_id,
                                      reason="task_not_ready",
                                      message="O vídeo ainda não está pronto na TV")
        if journal.get("phase") == "unconfirmed" and not manual:
            return self._apply_result("unconfirmed", journal, task_id=task_id,
                                      reason="manual_reconciliation_required",
                                      message="Ativeção anterior não confirmada; reconciliação manual necessária")

        # Already present in the journal: prove it on the tablet before
        # announcing anything. After a rollback, a disconnection or a
        # generation switch the journal may be stale.
        # Membership alone does not prove the *list* is current: another
        # episode may have been deleted (or metadata / Live config changed).
        # A missing fingerprint is deliberately not treated as a match.
        inventory_matches = (bool(self.state.get("active_inventory_hash")) and
                             self.compute_inventory_hash(tasks) == self.state.get("active_inventory_hash"))
        if inventory_matches and self.index_contains(self.state.get("active_index"), task):
            confirmation = self.confirm_task(task_id, tasks=tasks)
            if confirmation.get("confirmed") is True:
                self._update_journal(requested_task=task_id, requested_ts=self._now(),
                                     last_apply_state="already_current")
                return {"state": "already_current",
                        "generation": self.state.get("active_generation"),
                        "interrupts_playback": False, "confirmed": True,
                        "message": "✅ Lista atualizada — abra o vídeo na TV",
                        "reason": None}
            if confirmation.get("confirmed") is None:
                # The journal says it is published, but the tablet cannot be
                # reached: never claim success and never switch blindly.
                self._update_journal(requested_task=task_id, requested_ts=self._now(),
                                     last_apply_state="unknown",
                                     last_blocked_reason="tablet_unconfirmed")
                return {"state": "unknown", "generation": self.state.get("active_generation"),
                        "interrupts_playback": False, "confirmed": False,
                        "message": "⚠️ Não foi possível confirmar a lista na TV. "
                                   "Nada foi alterado; verifique o tablet e toque de novo.",
                        "reason": confirmation.get("reason") or "tablet_unconfirmed"}
            # Journal stale or revision changed: fall through and republish.

        # New content: this click authorises a manual publication.
        playback, playback_reason = self.playback_state()
        if playback != "idle":
            self._update_journal(requested_task=task_id, requested_ts=self._now(),
                                 last_apply_state="blocked",
                                 last_blocked_reason=playback_reason)
            if self.may_interrupt_playback(playback_reason, manual=manual,
                                           confirm_interrupt=confirm_interrupt):
                # The manual click is the authorisation.
                # Recorded so the interruption is never silent.
                self._update_journal(interrupted_legacy=playback_reason,
                                     interrupt_confirmed_ts=self._now())
            else:
                if playback == "playing":
                    message = ("⚠️ Atualizar a lista vai interromper a reprodução; "
                               "confirme para continuar")
                else:
                    message = ("⚠️ Estado da TV desconhecido; não alterei nada. "
                               "Aguarde a telemetria e toque de novo.")
                return self._apply_result(
                    "blocked", self._journal(), task_id=task_id,
                    reason=playback_reason, message=message,
                    interruptible=self.may_interrupt_playback(
                        playback_reason, manual=manual, confirm_interrupt=True))

        # Mark the attempt before the long work so a repeated click is
        # idempotent even while the publisher is still preparing/transferring.
        self._mark_apply_in_progress(task_id)
        prepared = self.check_and_prepare_generation(tasks)
        pending = self._journal()
        if not prepared and not pending.get("generation"):
            return self._apply_result("failed", pending, task_id=task_id,
                                      reason="prepare_failed",
                                      message="Não foi possível preparar a lista")
        prepared_index = self.generation_index(str(pending.get("staging_dir") or ""))
        if not self.index_contains(prepared_index, task):
            # The requested video did not fit / is not publishable: say so.
            return self._apply_result("incomplete", pending, task_id=task_id,
                                      reason="task_not_in_generation",
                                      message="O vídeo não entrou na lista (capacidade ou revisão); "
                                              "veja pendências do pacote")
        self.state["pending_index"] = prepared_index
        self._save_state()
        self._update_journal(requested_task=task_id, requested_ts=self._now(),
                             interrupts_playback=False)
        result = self._publish_locked(manual=manual, confirm_interrupt=confirm_interrupt,
                                      tasks=tasks)
        state = result.get("state")
        mapping = {
            "active": ("active", "✅ Lista atualizada — abra o vídeo na TV"),
            "already_current": ("already_current", "✅ Lista atualizada"),
            "blocked": ("blocked", "⏳ TV em uso; pare a reprodução e toque de novo"),
            "incomplete": ("incomplete", "A lista ficou incompleta; veja pendências"),
            "unconfirmed": ("unconfirmed", "Ativação não confirmada; verifique a TV"),
            "failed": ("failed", "Falha ao atualizar a lista; nada foi trocado"),
        }
        final, message = mapping.get(state, ("failed", "Falha ao atualizar a lista"))
        self._update_journal(last_apply_state=final,
                             interrupts_playback=self.reconnected_pendrive(final))
        return {"state": final, "generation": result.get("generation"),
                "interrupts_playback": self.reconnected_pendrive(final),
                "reconnected": self.reconnected_pendrive(final),
                "reason": result.get("reason"),
                "message": message, "detail": result}

    # States in which the pendrive really was reconnected (or handed over) on
    # the tablet: the TV lost its drive for a moment, so whoever was watching
    # was interrupted. Everything else leaves the TV untouched.
    RECONNECTED_STATES = ("active",)

    @classmethod
    def reconnected_pendrive(cls, state: str) -> bool:
        """True only when the publication actually swapped the TV's drive.

        This is the honest value of ``interrupts_playback``: a publication that
        ended in ``active`` reconnected the pendrive (services were switched,
        the LUN was re-activated). ``already_current``, ``blocked``,
        ``failed``, ``unknown`` and ``unconfirmed`` never claim an interruption
        they did not perform.
        """
        return state in cls.RECONNECTED_STATES

    def _apply_result(self, state: str, journal: dict, *, task_id: str,
                      message: str, reason: str | None = None,
                      interruptible: bool = False) -> dict:
        return {"state": state, "generation": journal.get("generation"),
                "interrupts_playback": state == "blocked",
                "interruptible": bool(interruptible),
                "reason": reason, "message": message}

    def apply_status(self, task_id: str | None = None) -> dict:
        """Shape consumed by the dashboard polling endpoint (no secrets)."""
        journal = self._journal()
        phase = journal.get("phase")
        available = True
        selected = False
        if task_id and re.fullmatch(r"[0-9a-f]{8}", task_id or ""):
            selected = str(journal.get("requested_task") or "") == task_id and phase == "active"
            if not selected and self.state.get("active_index"):
                selected = task_id in self.state["active_index"]
        state = journal.get("last_apply_state") or phase or "idle"
        if state == "incomplete":
            missing = (self.state.get("publication") or {}).get("missing") or []
            if missing:
                state_message = "Pacote incompleto: faltam " + ", ".join(str(m) for m in missing)
            else:
                state_message = "A lista ficou incompleta"
        else:
            state_message = None
        messages = {
            "active": "✅ Lista atualizada",
            "already_current": "✅ Lista atualizada",
            "publishing": "⏳ Atualizando a lista do pendrive…",
            "transferred": "⏳ Atualizando a lista do pendrive…",
            "activating": "⏳ Atualizando a lista do pendrive…",
            "blocked": "⏳ TV em uso; pare a reprodução e toque de novo",
            "unconfirmed": "⚠️ Ativação não confirmada; verifique a TV",
            "failed": "Falha ao atualizar a lista",
            "incomplete": "A lista ficou incompleta",
            "prepared": "📥 Lista nova preparada (ainda não aplicada)",
            "idle": "Lista atual",
        }
        return {
            "available": available,
            "generation": self.state.get("active_generation"),
            "pending_generation": self.state.get("pending_generation"),
            "active_session": self.state.get("active_session"),
            "state": state,
            "phase": phase,
            "message": state_message or messages.get(state, "Lista atual"),
            "interrupts_playback": state in ("in_progress", "publishing", "transferred",
                                             "activating"),
            "selected_in_active": bool(selected),
            "requested_task": journal.get("requested_task"),
            "omitted_count": int(journal.get("omitted_count") or 0),
            "reason": journal.get("last_error") or journal.get("last_blocked_reason"),
        }

    def _read_tablet_file(self, package_dir: str, name: str, *,
                          max_bytes: int = 2 * 1024 * 1024) -> str | None:
        """Read a small package file from the tablet (read-only, no shell eval)."""
        transport = self._require_transport()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            raise PublicationError("invalid package file name")
        completed = transport.root_shell(
            f"cat {_quote(package_dir.rstrip('/') + '/' + name)}", check=False)
        output = completed.stdout or ""
        if completed.returncode != 0 or not output or len(output) > max_bytes:
            return None
        return output

    def confirm_task(self, task_id: str, *, tasks: list[dict] | None = None) -> dict:
        """Prove on the tablet that the active generation serves this exact VOD.

        Read-only: it never switches the LUN, so it is safe while the TV is on.
        Returns ``confirmed`` as ``True`` (proved), ``False`` (the tablet
        answered and the generation/revision does not match) or ``None``
        (unknown: unreachable, malformed answer, or the current inventory
        cannot be read).
        """
        if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{8}", task_id):
            return {"confirmed": False, "state": "invalid", "reason": "invalid_task_id"}
        try:
            with self.state_lock():
                return self._confirm_task_locked(task_id, tasks=tasks)
        except StateLockedError:
            return {"confirmed": None, "state": "in_progress",
                    "reason": "publication_in_progress"}

    def _confirm_task_locked(self, task_id: str, *, tasks: list[dict] | None) -> dict:
        session = self.state.get("active_session")
        generation = self.state.get("active_generation")
        manifest_id = self.state.get("active_manifest_id")
        recorded = (self.state.get("active_index") or {}).get(task_id)
        if not session or not isinstance(generation, int) or not manifest_id:
            return {"confirmed": False, "state": "no_active_generation",
                    "reason": "no_active_generation"}
        if not recorded:
            return {"confirmed": False, "state": "not_in_active_generation",
                    "reason": "not_in_active_generation"}

        if tasks is None:
            status = self.fetch_vod_status()
            tasks = status.get("tasks", []) if isinstance(status, dict) else None
        task = None
        if isinstance(tasks, list):
            task = next((t for t in tasks if str(t.get("id")) == task_id), None)
        if not isinstance(task, dict):
            # Without the current inventory the revision cannot be proven.
            return {"confirmed": None, "state": "unknown",
                    "reason": "inventory_unavailable"}

        if not self.index_contains({task_id: recorded}, task):
            expected_revision = vod_revision.content_revision(task)
            if not expected_revision:
                # The current inventory cannot prove the bytes: fail closed and
                # report unknown instead of "revision changed" (which would
                # authorise a blind republish).
                return {"confirmed": None, "state": "unknown",
                        "reason": "task_revision_unavailable"}
            return {"confirmed": False, "state": "revision_changed",
                    "reason": "revision_or_size_changed",
                    "generation": generation, "manifest_id": manifest_id}

        if self.transport is None:
            return {"confirmed": None, "state": "unknown", "reason": "no_tablet_transport"}
        try:
            verification = self.verify_active_on_tablet(session, generation, manifest_id)
        except (PublicationError, TabletCommandError):
            return {"confirmed": None, "state": "unknown", "reason": "tablet_unreachable"}
        if not verification.get("answered"):
            return {"confirmed": None, "state": "unknown", "reason": "tablet_unreachable"}
        if not verification.get("verified"):
            return {"confirmed": False, "state": "tablet_not_active",
                    "reason": "tablet_reports_another_generation",
                    "generation": verification.get("generation")}

        package_dir = self._package_dir_on_tablet(session)
        raw = self._read_tablet_file(package_dir, "vod_allowlist.json")
        if raw is None:
            return {"confirmed": None, "state": "unknown", "reason": "allowlist_unreadable"}
        try:
            allowlist = json.loads(raw)
            entries = {str(item.get("task_id")): item
                       for item in allowlist.get("tasks", []) if isinstance(item, dict)}
        except (ValueError, TypeError, AttributeError):
            return {"confirmed": None, "state": "unknown", "reason": "allowlist_malformed"}
        entry = entries.get(task_id)
        if not isinstance(entry, dict):
            return {"confirmed": False, "state": "not_in_active_generation",
                    "reason": "not_in_tablet_allowlist"}
        expected_revision = vod_revision.content_revision(task)
        size_ok = int(entry.get("size") or 0) == int(recorded.get("size") or 0)
        revision_ok = str(entry.get("revision") or "") == str(expected_revision or "")
        if not (size_ok and revision_ok):
            return {"confirmed": False, "state": "revision_changed",
                    "reason": "tablet_revision_mismatch", "size_match": size_ok,
                    "revision_match": revision_ok,
                    "generation": generation, "manifest_id": manifest_id}
        return {"confirmed": True, "state": "confirmed",
                "reason": "tablet_active_generation_matches",
                "generation": generation, "manifest_id": manifest_id,
                "size_match": True, "revision_match": True}

    def _refetch_tasks(self) -> list[dict] | None:
        """Read the catalog once more, or None when it cannot be read.

        None means "unknown", never "empty": the caller must keep the snapshot
        it already has instead of publishing a list built from nothing.
        """
        try:
            status = self.fetch_vod_status()
        except Exception:                       # never mask the real step
            return None
        if not isinstance(status, dict) or not isinstance(status.get("tasks"), list):
            return None
        return [t for t in status["tasks"] if isinstance(t, dict)]

    def confirm_inventory(self, *, tasks: list[dict] | None = None) -> dict:
        """Prove the WHOLE active list, not a single file.

        ``confirm_task`` is anchored on one video; a deletion (especially the
        last one) has no such anchor, so the list itself must be proven. The
        tablet has to report the recorded generation *and* serve an allowlist
        that matches the recorded index exactly, in both directions: a task the
        tablet still offers but the generation should no longer contain (a
        deleted VOD) is a mismatch, never a success.
        """
        try:
            with self.state_lock():
                if tasks is None:
                    status = self.fetch_vod_status()
                    if not isinstance(status, dict) or not isinstance(status.get("tasks"), list):
                        return {"confirmed": None, "state": "unknown", "reason": "inventory_unavailable"}
                    tasks = status["tasks"]
                if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
                    return {"confirmed": None, "state": "unknown", "reason": "inventory_unavailable"}
                active_hash = self.state.get("active_inventory_hash")
                if not active_hash:
                    return {"confirmed": None, "state": "unknown", "reason": "inventory_hash_unavailable"}
                if self.compute_inventory_hash(tasks) != active_hash:
                    return {"confirmed": False, "state": "list_mismatch", "reason": "inventory_changed"}
                return self._confirm_inventory_locked()
        except StateLockedError:
            return {"confirmed": None, "state": "in_progress",
                    "reason": "publication_in_progress"}

    def _confirm_inventory_locked(self) -> dict:
        session = self.state.get("active_session")
        generation = self.state.get("active_generation")
        manifest_id = self.state.get("active_manifest_id")
        if not session or not isinstance(generation, int) or not manifest_id:
            return {"confirmed": False, "state": "no_active_generation",
                    "reason": "no_active_generation"}
        recorded = self.state.get("active_index")
        if not isinstance(recorded, dict):
            # Without the recorded index the list cannot be compared at all.
            return {"confirmed": None, "state": "unknown",
                    "reason": "active_index_unavailable"}
        if self.transport is None:
            return {"confirmed": None, "state": "unknown",
                    "reason": "no_tablet_transport"}
        try:
            verification = self.verify_active_on_tablet(session, generation, manifest_id)
        except (PublicationError, TabletCommandError):
            return {"confirmed": None, "state": "unknown", "reason": "tablet_unreachable"}
        if not verification.get("answered"):
            return {"confirmed": None, "state": "unknown", "reason": "tablet_unreachable"}
        if not verification.get("verified"):
            return {"confirmed": False, "state": "tablet_not_active",
                    "reason": "tablet_reports_another_generation",
                    "generation": verification.get("generation")}
        package_dir = self._package_dir_on_tablet(session)
        raw = self._read_tablet_file(package_dir, "vod_allowlist.json")
        if raw is None:
            return {"confirmed": None, "state": "unknown",
                    "reason": "allowlist_unreadable"}
        try:
            allowlist = json.loads(raw)
            entries = {str(item.get("task_id")): item
                       for item in allowlist.get("tasks", []) if isinstance(item, dict)}
        except (ValueError, TypeError, AttributeError):
            return {"confirmed": None, "state": "unknown",
                    "reason": "allowlist_malformed"}
        missing = sorted(set(recorded) - set(entries))
        extra = sorted(set(entries) - set(recorded))
        changed = sorted(
            task_id for task_id in (set(recorded) & set(entries))
            if int(entries[task_id].get("size") or 0) != int(recorded[task_id].get("size") or 0)
            or str(entries[task_id].get("revision") or "")
            != str(recorded[task_id].get("revision") or ""))
        if missing or extra or changed:
            return {"confirmed": False, "state": "list_mismatch",
                    "reason": "tablet_list_differs_from_generation",
                    "missing_count": len(missing), "extra_count": len(extra),
                    "changed_count": len(changed),
                    "generation": generation, "manifest_id": manifest_id}
        return {"confirmed": True, "state": "confirmed",
                "reason": "tablet_list_matches_generation",
                "entries": len(recorded), "generation": generation,
                "manifest_id": manifest_id}

    # --------------------------------------------------------------- transport
    def _require_transport(self) -> TabletTransport:
        if self.transport is None:
            raise PublicationError("no tablet transport configured")
        return self.transport

    def _package_dir_on_tablet(self, session: str) -> str:
        transport = self._require_transport()
        if not _SESSION_RE.fullmatch(session):
            raise PublicationError("invalid session name")
        return f"{transport.base}/live-favorites/{session}"

    def _script_command(self, package_dir: str, *args: str) -> str:
        return ("busybox sh " + package_dir + "/activate_live_favorites_tablet.sh "
                + " ".join(_quote(a) for a in args))

    def verify_active_on_tablet(self, session: str, generation: int,
                                manifest_id: str) -> dict:
        """Ask the tablet to prove which generation/manifest is active."""
        transport = self._require_transport()
        package_dir = self._package_dir_on_tablet(session)
        command = self._script_command(package_dir, "verify-active", package_dir,
                                       str(int(generation)), manifest_id)
        completed = transport.root_shell(command, check=False)
        output = (completed.stdout or "") + (completed.stderr or "")
        result = {
            "verified": False,
            "answered": False,
            "generation": None,
            "manifest_id": None,
            "returncode": completed.returncode,
        }
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("verify "):
                result["answered"] = True
                fields = dict(
                    part.split("=", 1) for part in line.split()[1:] if "=" in part)
                result["verified"] = fields.get("verified") == "1"
                try:
                    result["generation"] = int(fields.get("generation", ""))
                except (TypeError, ValueError):
                    result["generation"] = None
                result["manifest_id"] = fields.get("manifest_id") or None
                result["lock_owned"] = fields.get("lock_owned") == "1"
                result["lease"] = fields.get("lease") or "none"
        if completed.returncode != 0:
            result["verified"] = False
        if not (result["verified"]
                and result["generation"] == int(generation)
                and result["manifest_id"] == manifest_id):
            result["verified"] = False
        return result

    def transfer_package(self, session: str, staging_dir: Path) -> None:
        transport = self._require_transport()
        package_dir = self._package_dir_on_tablet(session)
        ok, reason = self.staging_is_complete(staging_dir)
        if not ok:
            raise PublicationError(f"staging package incomplete: {reason}")
        transport.root_shell(
            f"mkdir -p {_quote(transport.base + '/live-favorites')} "
            f"&& chmod 700 {_quote(package_dir)} 2>/dev/null || true")
        with self._publication_duration("tablet_push"):
            transport.push(staging_dir, package_dir)
        with self._publication_duration("tablet_hash_verification"):
            verify = transport.root_shell(
                f"cd {_quote(package_dir)} && busybox sha256sum -c SHA256SUMS", check=False)
        output = (verify.stdout or "") + (verify.stderr or "")
        if verify.returncode != 0 or "FAILED" in output:
            raise PublicationError("tablet package hash verification failed")
        transport.root_shell(
            f"chmod 600 {_quote(package_dir + '/transport.token')} "
            f"{_quote(package_dir + '/chisel.auth')} 2>/dev/null || true")

    # Success markers printed by the tablet script. They are the proof that a
    # step really ran: ``adb shell su -c`` on this tablet returns exit status 0
    # even when the remote command failed, so a return code alone would accept a
    # missing tunnel or a failed preflight.
    TUNNEL_OK_MARKERS = ("reachable through loopback", "tunnels tested",
                         "tunnel tested on port")
    PREFLIGHT_OK_MARKERS = ("preflight ok (",)

    @staticmethod
    def _tablet_output(completed) -> str:
        return ((completed.stdout or "") + (completed.stderr or "")).strip()

    @classmethod
    def _tablet_ran_ok(cls, completed, markers) -> bool:
        if completed is None or completed.returncode != 0:
            return False
        text = cls._tablet_output(completed)
        return any(marker in text for marker in markers)

    @classmethod
    def _tablet_tail(cls, completed, limit: int = 160) -> str:
        text = cls._tablet_output(completed)
        return text[-limit:] if text else "(no output)"

    def ensure_tunnels(self, session: str) -> None:
        transport = self._require_transport()
        package_dir = self._package_dir_on_tablet(session)
        check = self._script_command(package_dir, "tunnel-check", package_dir,
                                     str(self.live_port), str(self.vod_port))
        if self._tablet_ran_ok(transport.root_shell(check, check=False),
                               self.TUNNEL_OK_MARKERS):
            return
        start = self._script_command(package_dir, "tunnel-start", package_dir,
                                     str(self.live_port), str(self.vod_port))
        started = transport.root_shell(start, check=False)
        if not self._tablet_ran_ok(started, self.TUNNEL_OK_MARKERS):
            raise PublicationError(
                "could not establish live+videos tunnels: " + self._tablet_tail(started))
        if not self._tablet_ran_ok(transport.root_shell(check, check=False),
                                   self.TUNNEL_OK_MARKERS):
            raise PublicationError("tunnels did not become reachable")

    def preflight_media(self, session: str) -> None:
        """Real end-to-end proof: tablet -> tunnel -> proxy -> server bytes."""
        transport = self._require_transport()
        package_dir = self._package_dir_on_tablet(session)
        command = self._script_command(package_dir, "preflight", package_dir,
                                       str(self.live_port), str(self.vod_port))
        completed = transport.root_shell(command, check=False)
        if not self._tablet_ran_ok(completed, self.PREFLIGHT_OK_MARKERS):
            raise PublicationError(
                "end-to-end media preflight failed: " + self._tablet_tail(completed))

    def activate_package(self, session: str, *, handoff_token: str | None = None) -> None:
        """Ask the tablet to supervise this session, then prove it really began.

        The tablet script detaches ``start`` itself (``start-detached``): doing
        it there means the shell that creates the log is the device's own, so no
        quoting, parenthesis or ampersand has to survive ``adb shell -> su -c``
        — a silent no-start becomes impossible, and the wait below turns any
        remaining failure into a visible error.
        """
        transport = self._require_transport()
        package_dir = self._package_dir_on_tablet(session)
        log = f"{package_dir}/activation/start.log"
        args = ["start-detached", package_dir, f"127.0.0.1:{self.live_port}", session,
                f"127.0.0.1:{self.vod_port}"]
        if handoff_token:
            args.append(handoff_token)
        command = self._script_command(package_dir, *args)
        try:
            completed = transport.root_shell(command, check=False)
        except (PublicationError, TabletCommandError, OSError) as exc:
            raise ActivationUncertain(
                "tablet activation outcome unknown: transport failure "
                f"({type(exc).__name__})") from exc
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode != 0 or "start-detached pid=" not in output:
            raise ActivationNotStarted(
                "tablet refused to detach the session start: "
                + sanitize_tablet_output(output))
        deadline = time.monotonic() + min(30.0, max(5.0, self.publication_timeout / 4))
        while time.monotonic() < deadline:
            probe = transport.root_shell(
                f"test -f {_quote(log)} && echo started", check=False)
            if "started" in (probe.stdout or ""):
                return
            time.sleep(0.5)
        raise ActivationNotStarted("tablet did not start the supervised session")

    def handoff_lease(self, session: str, token: str,
                     *, previous_session: str | None = None) -> None:
        """Ask the tablet to publish our exclusive handoff lease.

        ``previous_session`` authorises a controlled handover: a live mode lock
        owned by that exact (recorded) generation is expected and will be taken
        over next; any other owner still refuses the lease.
        """
        transport = self._require_transport()
        package_dir = self._package_dir_on_tablet(session)
        args = ["handoff-lease", package_dir, token, str(self.guard_ttl)]
        if previous_session:
            if not _SESSION_RE.fullmatch(previous_session):
                raise PublicationError("invalid previous session name")
            args.append(previous_session)
        command = self._script_command(package_dir, *args)
        completed = transport.root_shell(command, check=False)
        output = (completed.stdout or "") + (completed.stderr or "")
        # The lease answer must be an exact marker; the tablet's own reason is
        # preserved (redacted) so a refusal is investigable instead of generic.
        answers = [line.strip() for line in (completed.stdout or "").splitlines()
                   if line.strip().startswith("handoff-lease token=")]
        if answers == [f"handoff-lease token={token} expires_in={int(self.guard_ttl)}"]:
            return
        raise PublicationError(
            "tablet refused the handoff lease: " + sanitize_tablet_output(output))
        if "already_active" in output:
            raise PublicationError("tablet reports this session already active")

    def handoff_renew(self, session: str, token: str) -> None:
        """Extend the tablet handoff lease; only its token owner may do so."""
        transport = self._require_transport()
        package_dir = self._package_dir_on_tablet(session)
        command = self._script_command(package_dir, "handoff-renew", package_dir,
                                       token, str(self.guard_ttl))
        completed = transport.root_shell(command, check=False)
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode != 0 or f"token={token}" not in output:
            raise PublicationError("tablet did not renew the handoff lease")

    # Marker the tablet supervisor must understand to accept a handoff and
    # record its acceptance. An older revision restores the legacy VOD mode when
    # it is signalled and never acknowledges, which is how a handoff used to be
    # lost after killing the running session.
    HANDOFF_PROTOCOL_MARKER = "handoff.ack"
    # Exact line a running supervisor writes once it is supervising under a
    # revision that understands (and records) the handoff acceptance.
    HANDOFF_PROTOCOL_ANNOUNCEMENT = "handoff.ack 1"

    def previous_supervisor_capability(self, previous_session: str) -> tuple[bool, str]:
        """Can the RUNNING supervisor of ``previous_session`` take a handoff?

        Read-only: it inspects the script that session is actually executing.
        Refusing to signal a supervisor that cannot acknowledge is what keeps a
        generation switch from destroying a working session.
        """
        if not _SESSION_RE.fullmatch(previous_session or ""):
            return False, "invalid_previous_session"
        if self.transport is None:
            return False, "no_tablet_transport"
        package_dir = self._package_dir_on_tablet(previous_session)
        # The RUNNING supervisor's own announcement is the proof. Reading the
        # script file is not: a later revision can replace it while the old
        # process still supervises, which would make the file claim a protocol
        # the running process does not implement.
        try:
            probe = self._require_transport().root_shell(
                f"cat {_quote(package_dir + '/activation/handoff.protocol')}",
                check=False)
        except (PublicationError, TabletCommandError):
            return False, "previous_supervisor_capability_unreadable"
        marker = (probe.stdout or "").strip()
        if marker == self.HANDOFF_PROTOCOL_ANNOUNCEMENT:
            return True, "ok"
        # No announcement. That is only a refusal when there is actually a
        # running supervisor of that session to signal: killing a supervisor
        # that cannot acknowledge is what strands the tablet in legacy mode.
        # With no live lock owned by it there is nothing to take over, so the
        # switch is free.
        try:
            lock = self._require_transport().root_shell(
                "cat /data/local/tmp/mode_switch.lock/pid", check=False)
        except (PublicationError, TabletCommandError):
            return False, "previous_supervisor_capability_unreadable"
        owner = (lock.stdout or "").strip()
        if not owner or not owner.isdigit():
            return True, "no_previous_supervisor_running"
        try:
            cmdline = self._require_transport().root_shell(
                f"tr '\\000' '\\n' < /proc/{int(owner)}/cmdline", check=False)
        except (PublicationError, TabletCommandError, ValueError):
            return False, "previous_supervisor_capability_unreadable"
        cmd = cmdline.stdout or ""
        if f"/live-favorites/{previous_session}/" not in cmd:
            # The lock belongs to somebody else: not our previous supervisor.
            return True, "no_previous_supervisor_running"
        return False, "previous_supervisor_not_handoff_capable"

    def handoff_takeover(self, session: str, token: str,
                         previous_session: str) -> bool:
        """Stop the previous supervisor under our lease, then free the lock.

        The tablet only terminates a process whose ``/proc/<pid>/cmdline``
        proves it belongs to ``previous_session``; anything else is refused.
        A supervisor that cannot acknowledge the handoff is refused too: killing
        it would strand the tablet in the legacy mode with no record.

        Raises:
            PreviousSupervisorNotUpgradable: the running supervisor cannot
                acknowledge, so it is refused before any signal.
            PreviousSupervisorTakeoverRefused: the tablet refused before
                signalling, with its own reason code.
            TakeoverUncertain: the signal may have been delivered and the
                answer lost (unknown outcome).
        """
        transport = self._require_transport()
        capable, reason = self.previous_supervisor_capability(previous_session)
        if not capable:
            raise PreviousSupervisorNotUpgradable(
                f"previous supervisor cannot accept a handoff ({reason})")
        package_dir = self._package_dir_on_tablet(session)
        command = self._script_command(package_dir, "handoff-takeover", package_dir,
                                       token, previous_session)
        try:
            completed = transport.root_shell(command, check=False)
        except (PublicationError, TabletCommandError, OSError) as exc:
            # The command may have been delivered and only the answer lost:
            # that is unknown, never "nothing happened".
            raise TakeoverUncertain(
                "tablet takeover outcome unknown: transport failure "
                f"({type(exc).__name__})") from exc
        output = (completed.stdout or "") + (completed.stderr or "")
        # ``adb shell su -c`` on this tablet returns exit status 0 even when the
        # remote command failed, so the return code proves nothing: the printed
        # marker is the contract. Only a single, exact marker line on stdout is
        # an answer; an incidental mention on stderr or a lookalike value
        # (``released=10``) is not one.
        # Strict grammar: exactly ONE line may speak, it must start with
        # "takeover", and its fields are parsed into a mapping. A contradictory
        # or duplicated answer is never proof of success.
        answers = []
        reasons = []
        for line in (completed.stdout or "").splitlines():
            text = line.strip()
            if not text:
                continue
            fields = text.split()
            if fields[0] != "takeover":
                continue
            if any(f.startswith("released=") for f in fields[1:]):
                answers.append(text)
                reasons.append(text)
        # Exactly one line, one released= field: anything else is not an answer.
        parsed = None
        if len(answers) == 1:
            fields = answers[0].split()[1:]
            values = {}
            for field in fields:
                if "=" in field:
                    key, _, value = field.partition("=")
                    values.setdefault(key, value)
            if "released" in values and len(
                    [f for f in fields if f.startswith("released=")]) == 1:
                parsed = values
        if parsed is not None and parsed["released"] == "1":
            # 'acked=1' is the supervisor's own acceptance; 'already_free=1'
            # means there was nothing to take over. Both are safe to proceed,
            # and any other combination is treated as unknown below.
            if parsed.get("acked") == "1" or parsed.get("already_free") == "1":
                return True
            raise TakeoverUncertain(
                "tablet takeover answer lacks proof: "
                + sanitize_tablet_output(answers[0], limit=120))
        if parsed is not None and parsed["released"] == "0":
            # A refusal BEFORE any signal: nothing changed, and the tablet's
            # own code must survive so the journal says why.
            raise PreviousSupervisorTakeoverRefused(
                "tablet refused the takeover ("
                + sanitize_tablet_output(answers[0], limit=120) + ")")
        if parsed is not None and parsed["released"] == "unknown":
            # Without the acceptance record the old supervisor may have
            # reinstated the legacy mode: the outcome is UNKNOWN, never "it did
            # not happen". The caller must not roll anything back blindly.
            raise TakeoverUncertain(
                "tablet takeover outcome unknown: " + sanitize_tablet_output(output))
        # No usable answer: the signal may have taken effect. Unknown, and the
        # tablet's own reason is kept (redacted) so it stays investigable.
        raise TakeoverUncertain(
            "tablet gave no takeover answer: " + sanitize_tablet_output(output))

    def restore_previous_generation(self, session: str, generation: int,
                                    manifest_id: str) -> dict:
        """Best effort: bring the previous generation back after a takeover."""
        if not session:
            return {"restored": False, "reason": "no_previous_session"}
        if not _SESSION_RE.fullmatch(session):
            return {"restored": False, "reason": "invalid_previous_session"}
        try:
            token = hashlib.sha256(
                f"restore:{session}:{self._now()}".encode("utf-8")).hexdigest()[:32]
            self.handoff_lease(session, token)
            self.activate_package(session, handoff_token=token)
            confirmation = self._poll_confirmation(session, int(generation or 0),
                                                   str(manifest_id or ""))
            return {"restored": bool(confirmation.get("verified")),
                    "reason": None if confirmation.get("verified") else "not_confirmed"}
        except (PublicationError, TabletCommandError) as exc:
            return {"restored": False, "reason": type(exc).__name__}

    def handoff_release(self, session: str, token: str) -> None:
        transport = self._require_transport()
        package_dir = self._package_dir_on_tablet(session)
        command = self._script_command(package_dir, "handoff-release", package_dir, token)
        transport.root_shell(command, check=False)

    # ------------------------------------------------------- VPS generation set
    def vps_target(self) -> str | None:
        """The ssh hop that owns the server-side consumers, if any."""
        if self.transport is not None and self.transport.ssh_host:
            return self.transport.ssh_host
        return None

    def vps_app_dir_effective(self) -> str:
        # ``vps_switch_root`` is the checkout root; ``vps_app_dir`` is the
        # deployed app dir. Either may be unset, but an empty value is never
        # forwarded as ``--app-dir ''``.
        return (self.vps_app_dir or self.vps_switch_root or "").strip()

    def _vps_argv(self, *args: str) -> list[str]:
        host = self.vps_target()
        if host:
            app_dir = self.vps_app_dir_effective()
            script = self.vps_switch_script_remote or "scripts/switch_mixed_generation_vps.sh"
            prefix = (f"cd {_quote(app_dir)} && " if app_dir else "")
            return ["ssh", "-o", "BatchMode=yes", host,
                    prefix + "sh " + _quote(script) + " "
                    + " ".join(_quote(a) for a in args)]
        # Local switch: the services of the running generation were started by
        # an earlier, privileged invocation, so they belong to root. Without
        # privilege the switch can neither read that session's state.json nor
        # signal its pids, the shared ports stay bound, and the candidate dies
        # with "invalid proxy configuration" (measured: `sam kill -0 <pid>` ->
        # EPERM; `sam cat state.json` -> EACCES).
        # ``-E`` is mandatory, not cosmetic: sudo resets the environment by
        # default, and SWITCH_ROOT/ENV_FILE/EXPORT_CONFIG_FILE would silently
        # fall back ($HOME/.mixed-vod, the wrong export) — the exact regression
        # that already cost a publication. ``-n`` keeps it non-interactive so a
        # missing sudoers grant fails instead of hanging on a password prompt.
        return ["sudo", "-n", "-E", "sh", str(self.vps_switch_script), *args]

    def vps_publish_package(self, staging_dir: Path, session: str) -> str:
        """Make the validated package persistent on the host that serves it.

        Without an ssh hop the media consumers run beside the publisher, so the
        local staging directory already is the stable path. With a remote hop
        the package is copied first (tar over scp), extracted into a private
        per-generation directory and re-verified there: the generation switch
        never receives a local path that the remote host cannot see.
        """
        if not _SESSION_RE.fullmatch(session):
            raise PublicationError("invalid session name")
        if not self.vps_target():
            return str(staging_dir)
        ok, reason = self.staging_is_complete(staging_dir)
        if not ok:
            raise PublicationError(f"staging package incomplete: {reason}")
        root = str(self.vps_package_root or "").rstrip("/")
        if not root.startswith("/") or len(root) < 2 or ".." in root.split("/"):
            raise PublicationError("invalid VPS package root")
        staging_dir = Path(staging_dir)
        final_dir = f"{root}/{session}"
        nonce = f"{os.getpid()}-{int(time.time() * 1000)}"
        stage_dir = f"{root}/.{session}.{nonce}.new"
        remote_tar = f"/tmp/mixed-pkg-{session}-{nonce}.tgz"
        tar_path = Path(self.staging_base) / f".{session}.{nonce}.tgz"
        transport = self._require_transport()
        try:
            completed = subprocess_run(
                ["tar", "-czf", str(tar_path), "-C", str(staging_dir), "."],
                capture_output=True, text=True, timeout=self.publication_timeout)
            if completed.returncode != 0:
                raise PublicationError("could not pack the generation for transfer")
            transport._run(["scp", "-q", str(tar_path), f"{self.vps_target()}:{remote_tar}"],
                           check=True, timeout=self.publication_timeout)
            # A BRAND NEW private staging directory receives the package, the
            # hashes are re-verified there, and only then is it moved into
            # place. An existing generation directory is never removed or
            # overwritten: a retry that finds one refuses instead of erasing a
            # directory that may be serving the live generation. Nothing is
            # ever deleted except the private tarball this run uploaded.
            remote = (
                "umask 077; base=" + _quote(root)
                + "; final=" + _quote(final_dir)
                + "; stage=" + _quote(stage_dir)
                + "; tar=" + _quote(remote_tar) + "; "
                "mkdir -p -- \"$base\"; chmod 700 -- \"$base\"; "
                "if [ -e \"$final\" ]; then rm -f -- \"$tar\"; "
                "echo generation-directory-exists; exit 3; fi; "
                "mkdir -m 700 -- \"$stage\" || { rm -f -- \"$tar\"; exit 4; }; "
                "tar -xzf \"$tar\" -C \"$stage\" || { rm -f -- \"$tar\"; exit 4; }; "
                "rm -f -- \"$tar\"; "
                "cd \"$stage\" && sha256sum -c SHA256SUMS >/dev/null || exit 5; "
                "mv -T -- \"$stage\" \"$final\" || exit 6; "
                "chmod 700 -- \"$final\"; "
                "echo pkg-ready"
            )
            verify = transport._run(["ssh", "-o", "BatchMode=yes", self.vps_target(), remote],
                                    check=False, timeout=self.publication_timeout)
            output = (verify.stdout or "") + (verify.stderr or "")
            if "generation-directory-exists" in output or verify.returncode == 3:
                raise PublicationError(
                    "a generation directory already exists on the VPS for this "
                    "session; refusing to replace it")
            if verify.returncode != 0 or "pkg-ready" not in output:
                raise PublicationError("remote package verification failed")
        finally:
            try:
                tar_path.unlink()
            except OSError:
                pass
        return final_dir

    def vps_switch_start(self, staging_dir: Path, session: str, generation: int,
                         *, previous_session: str = "") -> None:
        """Reconfigure the Live manager and the VOD proxy for this generation.

        ``previous_session`` (when it names a different, currently live session)
        makes the switch stop that session's own proven processes first: both
        generations share the loopback ports, so the candidate can only bind
        once the old one is gone. The script records it as the previous session
        and restores it if the candidate fails.
        """
        remote_dir = self.vps_publish_package(staging_dir, session)
        args = ["start", remote_dir, session,
                "--live-port", str(self.live_port), "--vod-port", str(self.vod_port),
                "--generation", str(int(generation))]
        if previous_session and previous_session != session:
            if not _SESSION_RE.fullmatch(previous_session):
                raise PublicationError("invalid previous session name")
            args += ["--supersede", previous_session]
        app_dir = self.vps_app_dir_effective()
        if app_dir:
            args += ["--app-dir", app_dir]
        if self.vps_upstream:
            args += ["--upstream", self.vps_upstream]
        completed = self._require_transport()._run(self._vps_argv(*args), check=False,
                                                   timeout=self.publication_timeout)
        output = (completed.stdout or "") + (completed.stderr or "")
        # "already_active" is the script's idempotent answer for a generation
        # whose services are already live and were proven earlier: a retry after
        # a partially failed publication must be able to continue.
        if completed.returncode != 0 or not (
                "switch ok" in output or "already_active" in output):
            # The switch script reports whether a superseded generation was
            # brought back. Never claim the previous generation was preserved
            # when its server-side services are in fact down.
            restored = None
            for line in output.splitlines():
                if line.startswith("switch failed previous_restored="):
                    for token in line.split():
                        if token.startswith("previous_restored="):
                            restored = token.split("=", 1)[1]
            if restored == "no":
                self._update_journal(previous_services_down=True)
                raise PublicationError(
                    "VPS generation switch failed and the previous generation's "
                    "services could not be restored (vps_previous_services_down)")
            raise PublicationError("VPS generation switch failed")
        self._update_journal(vps_generation_dir=remote_dir, previous_services_down=False)

    def vps_switch_stop(self, session: str) -> None:
        completed = self._require_transport()._run(
            self._vps_argv("stop", session), check=False, timeout=self.publication_timeout)
        return completed

    def rust_validate_package(self, staging_dir: Path) -> tuple[bool, str]:
        """Fast integrity + contract check; optionally the Rust structural proof."""
        ok, reason = self.staging_is_complete(staging_dir)
        if not ok:
            return False, reason
        try:
            generation, manifest_id = self.manifest_identity(staging_dir)
            validate_generation(staging_dir / "favorites_manifest.json",
                                staging_dir / "vod_allowlist.json", generation, manifest_id)
        except (OSError, ValueError, KeyError, TypeError):
            return False, "generation_contract_invalid"
        if self.rust_validate:
            completed = self._require_transport()._run(
                ["bash", str(ROOT / "scripts" / "validate_mixed_package.sh"), str(staging_dir)],
                check=False, timeout=self.publication_timeout)
            if completed.returncode != 0:
                return False, "rust_structural_validation_failed"
        return True, "ok"

    def rollback_package(self, session: str) -> None:
        transport = self._require_transport()
        package_dir = self._package_dir_on_tablet(session)
        command = self._script_command(package_dir, "rollback", package_dir)
        transport.root_shell(command, check=False)

    # ------------------------------------------------------------ publication
    def fresh_journal(self, fallback: dict | None = None) -> dict:
        """The journal as it is on disk, for a process that may outlive it.

        ``publish_pending`` is called from short-lived processes today, but the
        service object can live in a long-running one. Deciding a publication
        from an in-memory copy would be deciding from a memory of the journal.
        """
        state_file = getattr(self, "state_file", None)
        try:
            data = json.loads(Path(str(state_file)).read_text())
        except Exception:
            return dict(fallback or {})
        journal = data.get("publication")
        return dict(journal) if isinstance(journal, dict) else dict(fallback or {})

    def stale_publish_reason(self, journal: dict, tasks: list[dict] | None) -> str | None:
        """Why the journalled package must not be published as it is.

        ``None`` means it is safe to publish. The package has to match the Live
        export the services read now, and the inventory it was prepared for:
        whatever cannot be established blocks instead of proceeding.
        """
        staging_dir = Path(str(journal.get("staging_dir") or ""))
        try:
            # Imported here: this module is also imported by the dashboard path,
            # which must not pay for the manager module at import time.
            from run_live_favorites_manager import manifest_matches_export
            manifest = json.loads((staging_dir / "favorites_manifest.json").read_text())
            manifest_matches_export(manifest, self.export_config_path)
        except Exception as exc:
            logger.warning("Publication blocked: the package does not match the "
                           "current export (%s)", exc)
            return "stale_for_export"
        if not isinstance(tasks, list):
            status = self.fetch_vod_status()
            tasks = status.get("tasks") if isinstance(status, dict) else None
        if not isinstance(tasks, list):
            logger.warning("Publication blocked: the inventory could not be read")
            return "inventory_unavailable"
        # The on-disk journal wins over the in-memory copy; a field missing on
        # disk but present in memory is not invented either.
        disk_journal = self.fresh_journal(journal)
        expected = disk_journal.get("inventory_hash") or journal.get("inventory_hash")
        staging_dir = Path(str(disk_journal.get("staging_dir")
                               or journal.get("staging_dir") or ""))
        # A journal without a fingerprint cannot prove the package matches the
        # inventory it will be activated against: that is blocked, not assumed.
        if not expected:
            logger.warning("Publication blocked: the journal carries no inventory hash")
            return "stale_for_inventory"
        if self.compute_inventory_hash(tasks) != expected:
            logger.warning("Publication blocked: the inventory changed since the "
                           "package was prepared")
            return "stale_for_inventory"
        return None

    def _journal(self) -> dict:
        journal = self.state.get("publication")
        return dict(journal) if isinstance(journal, dict) else {}

    def _update_journal(self, **fields) -> dict:
        journal = self._journal()
        journal.update(fields)
        journal["updated_ts"] = self._now()
        self.state["publication"] = journal
        self._save_state()
        return journal

    @contextmanager
    def _publication_duration(self, stage: str):
        """Record elapsed time without letting telemetry alter publication."""
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed = round(max(0.0, time.monotonic() - started), 3)
            try:
                durations = dict(self._journal().get("stage_durations_s") or {})
                durations[stage] = elapsed
                self._update_journal(stage_durations_s=durations)
            except Exception:
                logger.warning("could not record publication stage duration")

    def promote_pending_generation(self, confirmation: dict | None = None) -> bool:
        """Mark the pending generation ACTIVE — only with tablet confirmation."""
        if not isinstance(confirmation, dict) or confirmation.get("verified") is not True:
            raise PublicationError("promotion requires a verified tablet confirmation")
        with self.state_lock():
            pending = self.state.get("pending_generation")
            if not pending:
                return False
            if not isinstance(confirmation.get("generation"), int) or int(
                    confirmation["generation"]) != int(pending):
                raise PublicationError("confirmation generation does not match pending")
            if confirmation.get("manifest_id") != self.state.get("pending_manifest_id"):
                raise PublicationError("confirmation manifest does not match pending")
            self.state["active_generation"] = pending
            self.state["active_inventory_hash"] = self.state.get("pending_inventory_hash")
            self.state["active_manifest_id"] = self.state.get("pending_manifest_id")
            self.state["active_session"] = self.state.get("pending_session")
            self.state["active_index"] = self.state.get("pending_index")
            self.state["pending_generation"] = None
            self.state["pending_staging_dir"] = None
            self.state["pending_inventory_hash"] = None
            self.state["pending_manifest_id"] = None
            self.state["pending_session"] = None
            self.state["pending_index"] = None
            journal = self._journal()
            journal.update({"phase": "active", "confirmed_ts": self._now(),
                            "updated_ts": self._now()})
            self.state["publication"] = journal
            self._save_state()
        logger.info("Generation %s confirmed by the tablet and promoted to ACTIVE.", pending)
        return True

    def publish_pending(self, *, manual: bool = False,
                        confirm_interrupt: bool = False,
                        tasks: list[dict] | None = None) -> dict:
        """Transfer, activate and confirm the pending generation (manual only)."""
        with self.state_lock():
            return self._publish_locked(manual=manual,
                                        confirm_interrupt=confirm_interrupt,
                                        tasks=tasks)

    def _publish_locked(self, *, manual: bool = False,
                        confirm_interrupt: bool = False,
                        tasks: list[dict] | None = None) -> dict:
        if self.transport is None:
            return {"state": "skipped", "reason": "no_tablet_transport"}
        journal = self._journal()
        if not journal:
            return {"state": "empty"}
        phase = journal.get("phase")
        if phase == "active":
            return {"state": "active", "generation": journal.get("generation")}
        if not manual:
            # Applying a generation reconnects the pendrive and therefore
            # interrupts playback. It is only performed on an explicit,
            # manual authorisation (the dashboard button or --publish).
            return {"state": "manual_required",
                    "reason": "manual_authorisation_required"}
        generation = int(journal.get("generation") or 0)
        manifest_id = str(journal.get("manifest_id") or "")
        session = str(journal.get("session") or "")
        staging_dir = Path(str(journal.get("staging_dir") or ""))
        if not generation or not manifest_id or not session:
            self._update_journal(phase="failed", last_error="incomplete_journal")
            return {"state": "failed", "reason": "incomplete_journal"}

        ok, reason = self.rust_validate_package(staging_dir)
        if not ok:
            self._update_journal(phase="incomplete", last_error=reason)
            return {"state": "incomplete", "reason": reason}

        local_generation, local_manifest = self.manifest_identity(staging_dir)
        if (local_generation, local_manifest) != (generation, manifest_id):
            self._update_journal(phase="failed", last_error="staging_manifest_mismatch")
            return {"state": "failed", "reason": "staging_manifest_mismatch"}

        # Recoverable path: a package may already be on the tablet from an
        # interrupted attempt. Confirm before doing anything else.
        if phase in ("transferred", "activating", "unconfirmed"):
            confirmation = self.verify_active_on_tablet(session, generation, manifest_id)
            if confirmation["verified"]:
                self.promote_pending_generation(confirmation)
                return {"state": "active", "recovered": True, "generation": generation}
            if phase == "unconfirmed":
                # An unconfirmed session may still be showing media to someone.
                # Never interrupt it merely because confirmation is unavailable.
                return {"state": "unconfirmed", "reason": "manual_reconciliation_required"}

        # A package may only be published for the export and the inventory it was
        # built for. After the Live export migrates, transferring the journalled
        # package would start the manager with the new export and a manifest built
        # for the old one: the manager refuses it and Live stays down. This runs
        # before anything touches the tablet, and every uncertainty blocks.
        stale = self.stale_publish_reason(journal, tasks)
        if stale:
            self._update_journal(phase="prepared", last_error=stale)
            return {"state": "prepared", "reason": stale, "generation": generation,
                    "interrupts_playback": False, "reconnected": False, "confirmed": None,
                    "message": PUBLISH_BLOCKED_MESSAGES.get(stale, "Publicação bloqueada")}

        # Recheck telemetry; an explicitly authorised manual request may
        # proceed even if playback is active or its state is unknown.
        playback, idle_reason = self.playback_state()
        if playback != "idle":
            if not self.may_interrupt_playback(idle_reason, manual=manual,
                                               confirm_interrupt=confirm_interrupt):
                self._update_journal(last_blocked_reason=idle_reason)
                return {"state": "blocked", "reason": idle_reason}
            self._update_journal(interrupted_legacy=idle_reason,
                                 interrupt_confirmed_ts=self._now())

        attempts = int(journal.get("attempts") or 0) + 1
        # Honesty about the evidence: ``active_vod`` / ``listeners`` / bitrate
        # come from the LEGACY modes and say nothing about the TV reading the
        # mixed pendrive. They are only ever used as a blocker. The journal
        # records that no mixed-reader proof exists, so nobody can mistake this
        # decision for a measured idle state.
        self._update_journal(phase="publishing", attempts=attempts, last_error=None,
                             started_ts=self._now(), idle_reason=idle_reason,
                             stage_durations_s={},
                             mixed_reader_proof="not_available",
                             tv_on=idle_reason == "idle_tv_on_no_playback")
        previous_session = str(self.state.get("active_session") or "")
        previous_generation = self.state.get("active_generation")
        previous_manifest_id = str(self.state.get("active_manifest_id") or "")
        guard_token = None
        handoff_token = None
        switched_vps = False
        activation_requested = False
        takeover_done = False
        takeover_uncertain = False
        keeper = None
        try:
            # 1. Exclusive guard: the server refuses any foreign playback or
            #    mode command from now until we release it.
            guard = self.acquire_guard(phase="publishing")
            guard_token = guard["token"]
            self._update_journal(guard_held=True, guard_acquired_ts=self._now())
            # The guard TTL is bounded (15-120 s) while a transfer and a
            # generation switch are not: renew in the background for the whole
            # guarded section and re-validate before/after every mutating step.
            keeper = GuardKeeper(self, guard_token).start()

            # 2. Transfer the package to the tablet. This does not touch the
            #    LUN and does not interrupt anyone.
            keeper.ensure("transfer")
            with self._publication_duration("tablet_transfer"):
                self.transfer_package(session, staging_dir)
            keeper.ensure("transfer completion")
            self._update_journal(phase="transferred")

            # 3. Playback authorisation, re-checked under the guard. Replacing the
            #    server-side consumers below makes the previous generation stop
            #    answering, so this is the first step that can interrupt a
            #    reader. The TV remote is outside software control: the manual
            #    click is what authorises this interruption and the UI says so.
            playback, reason = self.playback_state()
            if playback != "idle" and not self.may_interrupt_playback(
                    reason, manual=manual, confirm_interrupt=confirm_interrupt):
                keeper.stop()
                released = self._release_guard_safely(guard_token)
                self._update_journal(last_blocked_reason=reason, guard_held=not released)
                return {"state": "blocked", "reason": reason}
            if playback != "idle":
                self._update_journal(interrupted_legacy=reason)

            # 4. Server-side generation set FIRST: the candidate Live manager and
            #    VOD proxy of THIS generation must exist and serve it before any
            #    tablet change. On a cold start nothing was listening yet; on
            #    N->N+1 the previous session's own processes are superseded so
            #    the candidate can bind the shared ports, and the script
            #    restores them if this fails. The Live byte path is proven here
            #    only through non-destructive PROBE/range checks, because a real
            #    OPEN would advance the manager's generation/epoch counters and
            #    make the tablet's own client appear stale.
            with self._publication_duration("vps_switch"):
                self.vps_switch_start(staging_dir, session, generation,
                                      previous_session=previous_session)
            switched_vps = True
            keeper.ensure("generation switch completion")

            # 5. Now the tunnel has a live upstream of the right generation:
            #    bring the dedicated tunnels up and prove the real end-to-end
            #    path (Live handshake + authenticated VOD byte range) through
            #    them, with the same package and session the tablet will use.
            with self._publication_duration("tunnels"):
                self.ensure_tunnels(session)
            with self._publication_duration("preflight"):
                self.preflight_media(session)
            keeper.ensure("preflight completion")

            # 6. Re-check under the guard: playback could not have started
            #    through the server, but the TV itself may have been switched on
            #    or a video resumed. Abort and put the previous generation's
            #    services back.
            playback, reason = self.playback_state()
            if playback != "idle" and not self.may_interrupt_playback(
                    reason, manual=manual, confirm_interrupt=confirm_interrupt):
                keeper.stop()
                self._rollback_vps(guard_token=guard_token, session=session)
                self._update_journal(last_blocked_reason=reason)
                return {"state": "blocked", "reason": reason}
            keeper.ensure("activation decision")

            # 7. Tablet-side handoff lease, controlled takeover of the previous
            #    supervisor, then the supervised switch.
            handoff_token = hashlib.sha256(
                f"{session}:{generation}:{self._now()}".encode("utf-8")).hexdigest()[:32]
            self.handoff_lease(session, handoff_token, previous_session=previous_session)
            # The tablet lease is the single-writer token for the LUN swap and
            # expires on its own TTL. Renew it in lockstep with the server guard
            # so a long takeover can never run on an expired lease; a refused
            # renewal aborts before the LUN.
            keeper.extra = lambda: self.handoff_renew(session, handoff_token)
            keeper.ensure("tablet handoff lease")
            if previous_session and previous_session != session:
                # A transport failure or a missing output marker does not prove
                # that the tablet failed to stop the previous supervisor.
                takeover_uncertain = True
                try:
                    takeover_done = self.handoff_takeover(
                        session, handoff_token, previous_session)
                except (PreviousSupervisorNotUpgradable,
                        PreviousSupervisorTakeoverRefused):
                    # A refusal BEFORE any signal is a KNOWN state: nothing was
                    # switched, so it must not be reported as ambiguous.
                    takeover_uncertain = False
                    raise
                takeover_uncertain = False
                if not takeover_done:
                    # The tablet answered "not released": its previous
                    # supervisor still owns the mode lock, so nothing may be
                    # activated yet. This is a known state - nothing was
                    # switched - and the services are rolled back by the
                    # recovery path.
                    raise PublicationError(
                        "previous generation still owns the mode lock; the "
                        "scheduled switch was not performed")
                self._update_journal(previous_generation_stopped=takeover_done)
            keeper.ensure("tablet activation")
            self._update_journal(phase="activating", activated_ts=self._now())
            # Conservative by default: once the request may have reached the
            # tablet the outcome is unknown. Only a PROVEN non-start (the tablet
            # refused, or the supervised log never appeared) clears it, because
            # then undoing is safe and correct.
            activation_requested = True
            try:
                with self._publication_duration("activation_request"):
                    self.activate_package(session, handoff_token=handoff_token)
            except ActivationNotStarted:
                activation_requested = False
                raise
        except (PublicationError, TabletCommandError) as exc:
            if keeper is not None:
                keeper.stop()
            return self._recover_failed_publication(
                session=session, generation=generation, manifest_id=manifest_id,
                guard_token=guard_token, handoff_token=handoff_token,
                switched_vps=switched_vps, previous_session=previous_session,
                activation_requested=activation_requested, reason=str(exc),
                takeover_done=takeover_done, previous_generation=previous_generation,
                previous_manifest_id=previous_manifest_id,
                takeover_uncertain=takeover_uncertain)
        finally:
            if keeper is not None:
                keeper.stop()

        with self._publication_duration("confirmation"):
            confirmation = self._poll_confirmation(session, generation, manifest_id,
                                                  handoff_token=handoff_token)
        if not confirmation["verified"]:
            # Do not record an unconfirmed activation, and do not revert a
            # session that may still be showing media to someone.
            released = self._release_guard_safely(guard_token)
            if handoff_token:
                self.handoff_release(session, handoff_token)
            self._update_journal(phase="unconfirmed", last_error="activation_not_confirmed",
                                 guard_held=not released, previous_session=previous_session,
                                 previous_generation=previous_generation,
                                 manual_reconciliation_required=True)
            return {"state": "unconfirmed", "generation": generation}
        try:
            self.promote_pending_generation(confirmation)
        except PublicationError as exc:
            released = self._release_guard_safely(guard_token)
            if handoff_token:
                self.handoff_release(session, handoff_token)
            self._update_journal(phase="unconfirmed", last_error=str(exc),
                                 guard_held=not released)
            return {"state": "unconfirmed", "reason": str(exc)}
        if handoff_token:
            self.handoff_release(session, handoff_token)
        released = self._release_guard_safely(guard_token)
        self._update_journal(guard_held=not released, previous_session=previous_session,
                             previous_generation=previous_generation,
                             guard_released_ts=self._now())
        return {"state": "active", "generation": generation}

    def _rollback_vps(self, *, guard_token: str | None, session: str) -> None:
        """Put the previous generation's services back after a late abort.

        The tablet was never touched, so the server-side consumers must match
        the generation the TV is still showing. A rollback that cannot be
        confirmed is journalled as such instead of being hidden.
        """
        try:
            completed = self.vps_switch_stop(session)
            output = (completed.stdout or "") + (completed.stderr or "")
            failed = completed.returncode != 0 or "switch stop" not in output
        except (PublicationError, TabletCommandError, OSError) as exc:
            logger.error("VPS rollback failed: %s", type(exc).__name__)
            failed = True
        self._update_journal(previous_services_down=bool(failed))
        released = self._release_guard_safely(guard_token)
        if not released:
            self._update_journal(guard_held=True)

    def _release_guard_safely(self, token: str | None) -> bool:
        """Release the guard; return True only on an acknowledged release.

        A release that cannot be acknowledged is never recorded as a clean
        release: the lease is time-bounded, but the journal must not claim an
        ownership we do not have.
        """
        if not token:
            return True
        try:
            released = self.release_guard(token)
        except Exception:                              # never mask the real error
            logger.warning("mode guard release raised; ownership is unknown")
            return False
        if not released:
            logger.warning("mode guard release unacknowledged; it will expire")
        return bool(released)

    def _recover_failed_publication(self, *, session: str, generation: int,
                                    manifest_id: str, guard_token: str | None,
                                    handoff_token: str | None, switched_vps: bool,
                                    previous_session: str, activation_requested: bool,
                                    reason: str, takeover_done: bool = False,
                                    previous_generation: int | None = None,
                                    previous_manifest_id: str = "",
                                    takeover_uncertain: bool = False) -> dict:
        """Restore the last known-good set after a failure, without guessing.

        An RPC error does not prove that its remote effect did not happen, so
        this never re-switches blindly:

        * if the tablet already reports this exact generation active, promote
          it (finishing an activation is not a new interruption);
        * otherwise restore the previous generation's services — the state that
          was working before the click;
        * when the previous supervisor was already stopped by the takeover, try
          to bring the previous generation back through the same supervised
          path and record the real outcome instead of claiming success;
        * the tablet is never reverted blindly, and when the activation had
          already been requested the ambiguous outcome is recorded for a human
          instead of being reported as a plain failure.
        """
        if activation_requested:
            try:
                confirmation = self.verify_active_on_tablet(session, generation, manifest_id)
            except (PublicationError, TabletCommandError):
                confirmation = {"verified": False}
            if confirmation.get("verified"):
                try:
                    self.promote_pending_generation(confirmation)
                except PublicationError as exc:
                    reason = f"{reason}; promote_failed={exc}"
                else:
                    completed = hashlib.sha256(
                        f"{session}:{generation}:{self._now()}".encode("utf-8")).hexdigest()[:32]
                    self.handoff_release(session, handoff_token or completed)
                    released = self._release_guard_safely(guard_token)
                    self._update_journal(guard_held=not released,
                                         previous_session=previous_session)
                    return {"state": "active", "recovered": True, "generation": generation}
        if takeover_uncertain or activation_requested:
            # Either the takeover answer was lost or the tablet was already
            # asked to activate: in both cases the tablet may be serving the
            # NEW generation right now. Stopping the new services or forcing the
            # old ones back would break whichever list is live, so nothing
            # destructive happens here. Only the guard is released so the user
            # can keep watching; a human reconciles with real evidence.
            released = self._release_guard_safely(guard_token)
            if handoff_token:
                self.handoff_release(session, handoff_token)
            self._update_journal(
                phase="unconfirmed", last_error=reason,
                guard_held=not released, previous_session=previous_session,
                previous_restored=False, manual_reconciliation_required=True)
            return {"state": "unconfirmed", "reason": "manual_reconciliation_required",
                    "detail": reason, "generation": generation}
        if switched_vps:
            try:
                self.vps_switch_stop(session)
            except (PublicationError, TabletCommandError, OSError) as exc:
                logger.error("VPS rollback failed: %s", type(exc).__name__)
                reason = f"{reason}; vps_rollback_failed={type(exc).__name__}"
                self._update_journal(previous_services_down=True)
        if handoff_token:
            self.handoff_release(session, handoff_token)
        restore = {"restored": False, "reason": "not_needed"}
        if takeover_done and previous_session and not activation_requested:
            # The previous supervisor is gone and the new one never started:
            # bring back the generation that was working before the click.
            restore = self.restore_previous_generation(
                previous_session, int(previous_generation or 0), previous_manifest_id)
            if restore.get("restored"):
                self._update_journal(
                    phase="failed", last_error=reason, previous_restored=True,
                    previous_session=previous_session, guard_held=False,
                    manual_reconciliation_required=False)
                released = self._release_guard_safely(guard_token)
                if not released:
                    self._update_journal(guard_held=True)
                return {"state": "failed", "reason": reason, "generation": generation,
                        "previous_restored": True}
            reason = f"{reason}; previous_restore_failed={restore.get('reason')}"
        released = self._release_guard_safely(guard_token)
        ambiguous = bool(activation_requested or (takeover_done and not restore.get("restored")))
        self._update_journal(
            phase="unconfirmed" if ambiguous else "failed", last_error=reason,
            guard_held=not released, previous_session=previous_session,
            previous_restored=bool(restore.get("restored")),
            manual_reconciliation_required=ambiguous)
        if ambiguous:
            return {"state": "unconfirmed", "reason": "manual_reconciliation_required",
                    "detail": reason, "generation": generation}
        return {"state": "failed", "reason": reason, "generation": generation}

    def _poll_confirmation(self, session: str, generation: int,
                           manifest_id: str, *, handoff_token: str | None = None) -> dict:
        """Poll the tablet a bounded number of times for the active manifest.

        The attempt count is derived from the configured timeout and poll
        interval (never from the injected clock), so a frozen clock in tests
        cannot loop forever and a bug can never hang the daemon.
        """
        interval = max(self.poll_interval, 0.1)
        attempts = max(1, min(600, int(self.publication_timeout / interval) + 1))
        confirmation = {"verified": False}
        for _ in range(attempts):
            confirmation = self.verify_active_on_tablet(session, generation, manifest_id)
            if confirmation["verified"]:
                return confirmation
            time.sleep(self.poll_interval)
        return confirmation

    def resume_publication(self) -> dict:
        """Recover an interrupted attempt WITHOUT performing a new switch.

        Promotion of an already-switched, confirmed session does not interrupt
        anyone, so it is safe to finish automatically. Anything that would
        reconnect the pendrive requires a new manual authorisation.
        """
        with self.state_lock():
            journal = self._journal()
            if not journal:
                return {"state": "empty"}
            phase = journal.get("phase")
            if phase == "active":
                return {"state": "active", "generation": journal.get("generation")}
            if phase in ("transferred", "activating", "unconfirmed"):
                generation = int(journal.get("generation") or 0)
                manifest_id = str(journal.get("manifest_id") or "")
                session = str(journal.get("session") or "")
                try:
                    confirmation = self.verify_active_on_tablet(session, generation, manifest_id)
                except (PublicationError, TabletCommandError):
                    return {"state": "uncertain", "reason": "tablet_unreachable",
                            "message": "estado incerto: não foi possível consultar o tablet"}
                if confirmation["verified"]:
                    self.promote_pending_generation(confirmation)
                    return {"state": "active", "recovered": True, "generation": generation}
                if phase == "unconfirmed":
                    # Someone may be watching this session. Never revert it
                    # automatically; a human must decide.
                    return {"state": "unconfirmed",
                            "reason": "manual_reconciliation_required",
                            "message": "ativação não confirmada; confirme na TV antes de reverter"}
                return {"state": phase, "reason": "activation_pending",
                        "message": "reativação manual necessária"}
            return {"state": "manual_required", "reason": "manual_authorisation_required",
                    "message": "publicação pendente exige aplicação manual pelo botão"}

    # ------------------------------------------------------------------- loop
    def step(self, *, publish: bool = False) -> dict:
        """One check cycle: assess telemetry and prepare staging if needed.

        Preparing never touches the tablet. Applying a generation is a manual,
        guarded action (``apply_task``/``--publish``), because it reconnects the
        pendrive and therefore interrupts playback.
        """
        status = self.fetch_vod_status()
        tasks = status.get("tasks", []) if status else []
        server_status = self.fetch_server_status()
        active, reason = self.is_playback_active(server_status)
        prepared = None
        if tasks:
            try:
                prepared = self.check_and_prepare_generation(tasks)
            except StateLockedError as exc:
                logger.warning("state lock busy: %s", exc)
        result = {
            "tasks_count": len(tasks),
            "playback_active": active,
            "playback_reason": reason,
            "pending_generation": self.state.get("pending_generation"),
            "prepared": bool(prepared),
        }
        if publish:
            result["publication"] = self._publish_locked(manual=False)
        return result

    def run_daemon(self, poll_interval: float = 15.0, *, publish: bool = False):
        running = True

        def _handler(signum, frame):
            nonlocal running
            logger.info("Signal received, stopping daemon...")
            running = False

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

        logger.info("Starting mixed media generation daemon (interval=%ss)", poll_interval)
        if publish and self.transport is not None and self.state.get("publication"):
            try:
                logger.info("resume: %s", self.resume_publication())
            except (PublicationError, StateLockedError) as exc:
                logger.warning("resume deferred: %s", exc)
        while running:
            try:
                logger.debug("Step: %s", self.step(publish=publish))
            except Exception as exc:
                logger.error("Error in generation service loop: %s", exc)
            time.sleep(poll_interval)


def _canonical_manifest_bytes(manifest: dict) -> bytes:
    """Canonical manifest bytes, byte-identical to the Rust reader."""
    import struct

    encoded = bytearray(b"tv-live-favorites-manifest\0")

    def u32(value: int):
        encoded.extend(struct.pack("<I", value))

    def u64(value: int):
        encoded.extend(struct.pack("<Q", value))

    def string(value: str):
        data = (value or "").encode("utf-8")
        u32(len(data))
        encoded.extend(data)

    entries = sorted(
        manifest.get("entries", []),
        key=lambda item: (item["file_id"], item["namespace"], item["channel_id"],
                          item["folder"], item["filename"]))
    u32(int(manifest["schema_version"]))
    u32(int(manifest.get("layout_version", 1)))
    u64(int(manifest["generation"]))
    u32(len(entries))
    for item in entries:
        kind = item["kind"]
        u32(int(item["file_id"]))
        string(item["namespace"])
        string(item["channel_id"])
        encoded.append(1 if kind == "live" else 2)
        u32(len(item["folder"]))
        for component in item["folder"]:
            string(component)
        string(item["filename"])
        u64(int(item["logical_size"]))
        string(item.get("bootstrap_id", ""))
    return bytes(encoded)


def _build_transport(args) -> TabletTransport | None:
    if args.no_publish:
        return None
    return TabletTransport(
        ssh_host=args.ssh_host or None,
        adb_serial=args.adb_serial,
        base=args.tablet_base,
        dry_run=args.dry_run,
    )


def _mutating_action(args) -> bool:
    """True when this invocation can publish (and therefore needs the lock).

    Read-only proofs (``--verify``/``--status``/``--check-only``/
    ``--verify-inventory``) are deliberately excluded: they touch no state, and
    blocking the dashboard's status polling during a migration would only add
    noise.
    """
    for name in ("verify", "status", "check_only", "verify_inventory"):
        if getattr(args, name, False):
            return False
    for name in ("apply", "refresh", "publish", "resume", "daemon"):
        if getattr(args, name, False):
            return True
    # The default path (no flag) prepares and then publishes unless told not to.
    return not getattr(args, "no_publish", False)


def _acquire_publication_guard(args) -> dict | None:
    """Take the SHARED 'operation active' lock, or explain the refusal.

    Held for the whole process: this is what stops the migrator from restarting
    the bridge on top of a running publication, and what makes the migrator
    refuse instead of killing anything.
    """
    if not _mutating_action(args):
        return None
    if publication_lock is None:
        return {"ok": False, "state": "failed", "reason": "publication_lock_missing",
                "message": "Módulo de quiescência ausente; nada foi alterado.",
                "generation": None, "interrupts_playback": False,
                "detail": _LOCK_IMPORT_ERROR}
    lock_path = publication_lock.resolve_lock(
        explicit=os.environ.get(publication_lock.LOCK_ENV),
        state_file=getattr(args, "state_file", None))
    if lock_path is None:
        return {"ok": False, "state": "failed", "reason": "publication_lock_unresolved",
                "message": "Sem caminho de lock de publicação; nada foi alterado.",
                "generation": None, "interrupts_playback": False}
    try:
        guard = publication_lock.acquire(lock_path, shared=True, kind="publisher")
    except publication_lock.LockBusy as busy:
        logger.warning("Publication refused: migration window open (%s)", busy.holder)
        return {"ok": False, "state": "blocked", "reason": "migration_in_progress",
                "message": "Migração da grade de canais em andamento; nada foi "
                           "alterado. Tente de novo em instantes.",
                "generation": None, "interrupts_playback": False,
                "holder": busy.holder}
    except publication_lock.LockError as exc:
        return {"ok": False, "state": "failed", "reason": "publication_lock_unusable",
                "message": "Lock de publicação inutilizável; nada foi alterado.",
                "generation": None, "interrupts_playback": False,
                "detail": str(exc)}
    # Released on interpreter exit (and by the kernel if this process is killed).
    atexit.register(guard.release)
    return None


def _publisher_tablet_id() -> str:
    """Same identity derivation the server uses, so both address one operation."""
    for key in ("MIXED_TABLET_ID", "ADB_SERIAL", "TABLET_SERIAL"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return "tablet"


# An apply result maps onto an operation outcome. ``in_progress`` is deliberately
# absent: that operation belongs to another worker, so finishing it here would
# be stealing a result.
_OPERATION_OUTCOME = {
    "active": "succeeded",
    "already_current": "succeeded",
    "failed": "failed_safe",
    "incomplete": "failed_safe",
    "blocked": "blocked",
    # Ambiguous outcomes must be reconciled, never reported as a clean failure:
    # the device may have been touched and the truth is not known here.
    "unconfirmed": "needs_reconciliation",
    "unknown": "needs_reconciliation",
}


class _OperationTracker:
    """Keep the persistent operation in step with the real publication.

    The publisher inherits ``MIXED_OPERATIONS_DB`` from the bridge environment,
    so no new flag and no spool schema change is needed. The operation is found
    by tablet id because the store enforces at most one active operation per
    tablet — the same guarantee that makes the lookup unambiguous.

    Everything here is best-effort on purpose: observability must never be able
    to block a publication. A failure is logged and the work continues, which is
    also why the store itself never performs RPC.
    """

    def __init__(self, tablet_id: str):
        self.operation_id = None
        self._store = None
        path = os.environ.get("MIXED_OPERATIONS_DB", "").strip()
        if not path:
            return
        try:
            import mixed_operations
            self._store = mixed_operations.OperationStore(path)
        except Exception as exc:
            logger.warning("operation store unavailable (%s); publication continues",
                           type(exc).__name__)
            self._store = None
            return
        try:
            active = self._store.active_for_tablet(tablet_id)
        except Exception:
            active = None
        if active is not None:
            self.operation_id = active["operation_id"]

    @property
    def active(self) -> bool:
        return self._store is not None and self.operation_id is not None

    def phase(self, state: str, stage: str | None = None,
              message: str | None = None) -> None:
        if not self.active:
            return
        try:
            self._store.set_phase(self.operation_id, state=state, stage=stage,
                                  user_message=message)
        except Exception as exc:
            logger.warning("could not record operation phase %s (%s)",
                           state, type(exc).__name__)

    def finish(self, state: str, message: str, **kw) -> None:
        if not self.active:
            return
        try:
            self._store.finish(self.operation_id, state=state,
                               user_message=message, **kw)
        except Exception as exc:
            logger.warning("could not record operation result %s (%s)",
                           state, type(exc).__name__)


def _operation_outcome_for(state: str) -> str:
    """Map an apply result onto an operation outcome, failing closed.

    An unrecognised state is the ambiguous case by definition: the device may
    have been touched and this code cannot say what happened. Returning the last
    active state would hold the tablet slot forever, so it becomes
    ``needs_reconciliation`` — the caller must look at the device, not guess.
    """
    outcome = _OPERATION_OUTCOME.get(state)
    if outcome is None:
        logger.warning("unrecognised apply state %r; marking for reconciliation",
                       state)
        return "needs_reconciliation"
    return outcome


def _apply_command(args) -> int:
    """Manual application entry point used by the dashboard button."""
    refusal = _acquire_publication_guard(args)
    if refusal is not None:
        if args.json:
            print(json.dumps(refusal, indent=2))
        else:
            print(refusal.get("message") or refusal.get("reason"))
        return 3
    service = None
    try:
        service = MixedGenerationService(
            server_url=args.server_url, pin=args.pin,
            export_config_path=args.export_config, staging_base=args.staging_base,
            state_file=args.state_file, binaries_dir=args.binaries_dir,
            private_dir=args.private_dir, transport=_build_transport(args),
            live_port=args.live_port, vod_port=args.vod_port,
            status_max_age=args.status_max_age,
            publication_timeout=args.publication_timeout,
            guard_owner=args.guard_owner, guard_ttl=args.guard_ttl,
            vps_switch_script=args.vps_switch_script,
            vps_switch_root=args.vps_switch_root, vps_upstream=args.vps_upstream,
            vps_app_dir=args.vps_app_dir,
            volume_bytes=OPERATIONAL_VOLUME_BYTES,
            vps_package_root=args.vps_package_root or None,
            vps_switch_script_remote=args.vps_switch_script_remote or None,
            rust_validate=args.rust_validate,
        )
    except (PublicationError, ValueError) as exc:
        print(json.dumps({"state": "failed", "reason": "service_unavailable",
                          "message": str(exc), "generation": None,
                          "interrupts_playback": False}))
        return 2
    if args.verify_inventory:
        result = service.confirm_inventory()
        print(json.dumps(result, indent=2))
        return 0 if result.get("confirmed") is True else 1
    if args.verify:
        result = service.confirm_task(str(args.task_id))
        print(json.dumps(result, indent=2))
        return 0 if result.get("confirmed") is True else 1
    if args.status:
        print(json.dumps(service.apply_status(args.task_id), indent=2))
        return 0
    if args.refresh:
        result = service.refresh_inventory(manual=True,
                                           confirm_interrupt=args.confirm_interrupt)
        print(json.dumps(result, indent=2))
        return 0 if result.get("state") in ("active", "already_current", "in_progress") else 1
    if args.background:
        detached = [sys.executable, str(Path(__file__).resolve()), "--apply",
                    "--task-id", str(args.task_id), "--json"]
        if args.confirm_interrupt:
            detached.append("--confirm-interrupt")
        for flag, value in (("--state-file", args.state_file),
                            ("--staging-base", args.staging_base),
                            ("--export-config", args.export_config),
                            ("--binaries-dir", args.binaries_dir),
                            ("--adb-serial", args.adb_serial),
                            ("--tablet-base", args.tablet_base),
                            ("--live-port", args.live_port),
                            ("--vod-port", args.vod_port)):
            detached += [flag, str(value)]
        if args.private_dir:
            detached += ["--private-dir", str(args.private_dir)]
        if args.ssh_host:
            detached += ["--ssh-host", args.ssh_host]
        if args.pin:
            detached += ["--pin", args.pin]
        log_path = Path(args.state_file).with_suffix(".apply.log")
        with log_path.open("ab") as log:
            subprocess.Popen(detached, stdout=log, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        print(json.dumps({"state": "started", "generation": None,
                          "interrupts_playback": False,
                          "message": "⏳ Atualizando a lista do pendrive…",
                          "reason": None}))
        return 0
    # The endpoint already persisted the operation; adopt it so the panel sees
    # real phases instead of a row frozen in `preparing` forever.
    tracker = _OperationTracker(_publisher_tablet_id())
    tracker.phase("preparing", "publication", "Preparando a publicação")
    try:
        result = service.apply_task(str(args.task_id),
                                    confirm_interrupt=args.confirm_interrupt)
    except StateLockedError:
        # Another publisher already owns the state: report idempotently instead
        # of failing (a double click must never look like a broken update).
        result = {"state": "in_progress", "generation": None,
                  "interrupts_playback": True, "reason": "state_locked",
                  "message": "⏳ Atualização da lista já está em andamento"}
    state = str(result.get("state") or "unknown")
    outcome = _operation_outcome_for(state)
    tracker.finish(outcome,
                   str(result.get("message") or state),
                   error_code=result.get("reason"),
                   reconnected=bool(result.get("reconnected")))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result.get("message") or result.get("state"))
    return 0 if result.get("state") in ("active", "already_current", "in_progress") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", default="http://127.0.0.1:8080")
    parser.add_argument("--pin", default=os.environ.get("TV_PIN", ""))
    parser.add_argument("--export-config", type=Path, default=SCRATCH / "grade-1080p/export-revisao-sol.json")
    parser.add_argument("--vod-json", type=Path, default=None,
                        help="Optional local snapshot JSON instead of querying server")
    parser.add_argument("--staging-base", type=Path, default=SCRATCH / "mixed-staging")
    parser.add_argument("--state-file", type=Path, default=SCRATCH / "mixed_generation_state.json")
    parser.add_argument("--binaries-dir", type=Path, default=DEFAULT_BINARIES_DIR)
    parser.add_argument("--private-dir", type=Path, default=None,
                        help="Directory holding transport.token and chisel.auth (never committed)")
    parser.add_argument("--adb-serial", default="127.0.0.1:25555")
    parser.add_argument("--ssh-host", default="",
                        help="Optional ssh hop that owns the adb connection")
    parser.add_argument("--tablet-base", default="/data/local/tmp")
    parser.add_argument("--live-port", type=int, default=18190)
    parser.add_argument("--vod-port", type=int, default=18191)
    parser.add_argument("--status-max-age", type=float, default=60.0)
    parser.add_argument("--publication-timeout", type=float, default=120.0)
    parser.add_argument("--poll-interval", type=float, default=15.0)
    parser.add_argument("--guard-owner", default=os.environ.get("MIXED_GUARD_OWNER", "mixed-publisher"))
    parser.add_argument("--guard-ttl", type=int, default=int(os.environ.get("MIXED_GUARD_TTL", "60")))
    parser.add_argument("--vps-switch-script", type=Path, default=None)
    parser.add_argument("--vps-switch-root", default="")
    parser.add_argument("--vps-upstream", default="")
    parser.add_argument("--vps-app-dir", default="")
    parser.add_argument("--vps-package-root", default="")
    parser.add_argument("--vps-switch-script-remote", default="")
    parser.add_argument("--rust-validate", action="store_true",
                        help="Also run the Rust structural validation before switching")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--apply", action="store_true",
                        help="Manually apply the generation containing --task-id")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--background", action="store_true",
                        help="Detach the application and return immediately")
    parser.add_argument("--json", action="store_true",
                        help="Print machine-readable JSON (used by the dashboard)")
    parser.add_argument("--publish", action="store_true",
                        help="Publish the pending generation now (manual authorisation)")
    parser.add_argument("--confirm-interrupt", action="store_true",
                        help="Manual confirmation that a legacy playback signal may be "
                             "interrupted (never honoured without --apply/--publish)")
    parser.add_argument("--resume", action="store_true", help="Recover an interrupted publication")
    parser.add_argument("--verify", action="store_true",
                        help="Read-only proof that the tablet serves this task")
    parser.add_argument("--verify-inventory", action="store_true",
                        help="Read-only proof that the whole current inventory is active")
    parser.add_argument("--refresh", action="store_true",
                        help="Manual publish of the CURRENT inventory (no task anchor)")
    parser.add_argument("--status", action="store_true", help="Print journalled state")
    parser.add_argument("--daemon", action="store_true", help="Run as continuous background monitor")
    parser.add_argument("--no-publish", action="store_true",
                        help="Assemble and fingerprint only; never touch the tablet")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log tablet commands instead of executing them")
    args = parser.parse_args()

    if args.apply or args.refresh or args.verify_inventory:
        return _apply_command(args)

    refusal = _acquire_publication_guard(args)
    if refusal is not None:
        print(json.dumps(refusal, indent=2))
        return 3

    service = None
    try:
        service = MixedGenerationService(
            server_url=args.server_url,
            pin=args.pin,
            export_config_path=args.export_config,
            staging_base=args.staging_base,
            state_file=args.state_file,
            binaries_dir=args.binaries_dir,
            private_dir=args.private_dir,
            transport=_build_transport(args),
            live_port=args.live_port,
            vod_port=args.vod_port,
            status_max_age=args.status_max_age,
            publication_timeout=args.publication_timeout,
            guard_owner=args.guard_owner,
            guard_ttl=args.guard_ttl,
            vps_switch_script=args.vps_switch_script,
            vps_switch_root=args.vps_switch_root or None,
            vps_upstream=args.vps_upstream or None,
            vps_app_dir=args.vps_app_dir or None,
            rust_validate=args.rust_validate,
        )
    except (PublicationError, ValueError) as exc:
        print(f"[mixed] refusing to start: {exc}", file=sys.stderr)
        return 2

    if args.verify:
        print(json.dumps(service.confirm_task(str(args.task_id)), indent=2))
        return 0
    if args.status:
        print(json.dumps({"state": service.state,
                          "apply": service.apply_status(args.task_id or None),
                          "playback": service.is_playback_active()}, indent=2))
        return 0

    if args.resume:
        print(json.dumps(service.resume_publication(), indent=2))
        return 0

    if args.publish:
        status = service.fetch_vod_status()
        tasks = status.get("tasks") if isinstance(status, dict) else None
        print(json.dumps(service.publish_pending(
            manual=True, confirm_interrupt=args.confirm_interrupt,
            tasks=tasks if isinstance(tasks, list) else None), indent=2))
        return 0

    if args.daemon:
        # Preparing never touches the tablet: applying a generation is manual
        # (the dashboard button) because it reconnects the pendrive.
        service.run_daemon(poll_interval=args.poll_interval, publish=False)
        return 0

    if args.vod_json and args.vod_json.is_file():
        tasks = json.loads(args.vod_json.read_text())
    else:
        status = service.fetch_vod_status()
        tasks = status.get("tasks", []) if status else []

    logger.info("Checking %s VOD tasks...", len(tasks))
    active, reason = service.is_playback_active()
    logger.info("Playback active: %s (%s)", active, reason)

    if not tasks:
        logger.warning("No VOD tasks found.")
        return 0

    if args.check_only:
        print(json.dumps({
            "inventory_hash": service.compute_inventory_hash(tasks),
            "playback_active": active,
            "playback_reason": reason,
            "state": service.state,
        }, indent=2))
        return 0

    res = service.check_and_prepare_generation(tasks)
    if res:
        print(json.dumps(res, indent=2))
    if not args.no_publish:
        print(json.dumps(service.publish_pending(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
