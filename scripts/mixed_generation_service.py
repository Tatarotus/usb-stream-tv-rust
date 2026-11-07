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
import socket
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
# A process incarnation is `<pid>-<starttime>`: the pid alone is reused by the
# kernel, so the start time is what makes one process lifetime distinguishable
# from the next. Nothing else is accepted as an identity.
# The full identity token the tablet reports and compares:
# boot|instance|incarnation|manifest_id|package_digest - five non-empty
# components, no spaces. It is validated as a whole because the tablet refuses
# anything that is not complete, so a fragment must never be forwarded as if it
# were one.
_IDENTITY_TOKEN_RE = re.compile(r"[0-9A-Za-z_.:-]{1,64}(\|[0-9A-Za-z_.:-]{1,128}){4}")
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
        # adb shell joins its arguments into a remote shell command. Protect
        # the complete su -c argument across that first parse, so quotes and
        # pipes intended for the privileged shell reach it unchanged.
        return self.adb("shell", "su", "-c", _quote(command), check=check, timeout=timeout)

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
        # Alinhado ao runtime real (imagem de 256 GiB medida no tablet). O default
        # anterior de 128 GiB fazia o caminho _apply_command recusar a grade ativa
        # (40 canais Live ~149 GiB) com "capacity exceeded", que parecia falta de
        # disco no host - era o default do construtor contradizendo o runtime.
        volume_bytes: int = OPERATIONAL_VOLUME_BYTES,
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

    def describe_supervisor(self, package_dir: str):
        """Read the running supervisor's identity. Read-only.

        Returns None when the tablet did not answer in the expected protocol, so
        a caller can tell "no identity" from "identity says none" instead of
        trusting a half-built object. It never signals a process and never
        touches the LUN.
        """
        transport = self._require_transport()
        try:
            completed = transport.root_shell(
                self._script_command(package_dir, "describe", package_dir),
                check=False)
        except (PublicationError, TabletCommandError):
            return None
        return SupervisorIdentity.parse(
            (completed.stdout or "") + (completed.stderr or ""))

    # ------------------------------------------------- supervisor observation
    def _lock_pid_path(self) -> str:
        return self._require_transport().base + "/mode_switch.lock/pid"

    def _read_lock_owner(self) -> str | None:
        """The pid currently registered as the mode-lock owner, or None.

        The lock is the tablet's protected registry: it is the only place that
        says which process is actually supervising, and it is written by that
        process itself. Reading it is discovery, never proof — the proof is the
        owner's own ``describe`` answer.
        """
        try:
            completed = self._require_transport().root_shell(
                f"cat {_quote(self._lock_pid_path())}", check=False)
        except (PublicationError, TabletCommandError):
            return None
        owner = (completed.stdout or "").strip()
        return owner if owner.isdigit() else None

    def _cmdline_of(self, pid: int) -> str | None:
        try:
            completed = self._require_transport().root_shell(
                f"tr '\\000' '\\n' < /proc/{int(pid)}/cmdline", check=False)
        except (PublicationError, TabletCommandError):
            return None
        return completed.stdout or ""

    def observe_active_supervisor(self) -> dict:
        """Identify the supervisor that is ACTUALLY running. Read-only.

        Discovery runs through the protected registry (the mode lock), because
        the recorded session name is a location and a generation recovered into
        another folder keeps the same identity elsewhere. The location is then
        used only to reach the script of the *running* revision, whose own
        ``describe`` answer is the proof.

        Returns a dict with ``identity`` (or None) plus the discovered ``path``,
        ``session`` and ``lock_owner``. ``answered`` says whether a supervisor
        was found at all; every failure carries a ``reason`` so a caller can
        refuse specifically instead of guessing.
        """
        result = {"answered": False, "reason": None, "identity": None,
                  "path": None, "session": None, "lock_owner": None}
        if self.transport is None:
            result["reason"] = "no_tablet_transport"
            return result
        owner = self._read_lock_owner()
        if owner is None:
            # No live registration: there is no supervisor to identify. That is
            # not a failure, and it is not proof that one is absent either —
            # the caller sees `answered=False`.
            result["reason"] = "no_lock_owner_registered"
            return result
        result["lock_owner"] = owner
        cmdline = self._cmdline_of(int(owner))
        if cmdline is None:
            result["reason"] = "lock_owner_cmdline_unreadable"
            return result
        prefix = self._require_transport().base + "/live-favorites/"
        found = None
        for token in cmdline.splitlines():
            token = token.strip()
            if not token.startswith(prefix) or not token.endswith(
                    "/activate_live_favorites_tablet.sh"):
                continue
            session = token[len(prefix):-len("/activate_live_favorites_tablet.sh")]
            if _SESSION_RE.fullmatch(session):
                found = (session, token[:-len("/activate_live_favorites_tablet.sh")])
                break
        if found is None:
            # A lock owner whose own command line does not name a known session
            # script is not a supervisor this publisher can identify. Refusing
            # here is what keeps a foreign or older process from being signalled
            # on the strength of a directory name.
            result["reason"] = "lock_owner_is_not_a_known_supervisor"
            return result
        session, path = found
        identity = self.describe_supervisor(path)
        if identity is None:
            result["reason"] = "supervisor_did_not_answer describe"
            result["session"], result["path"] = session, path
            return result
        if not identity.incarnation.startswith(owner + "-"):
            # The registry and the answer disagree about WHICH process is
            # supervising, so neither is trustworthy. Never reconcile on this.
            result["reason"] = "supervisor_incarnation_disagrees_with_lock_owner"
            result["session"], result["path"] = session, path
            return result
        result.update({"answered": True, "identity": identity,
                       "session": session, "path": path})
        return result

    def revalidate_supervisor(self, observed) -> bool:
        """Re-read the identity under the guard, immediately before a mutation.

        Between the moment a caller decides to take over a generation and the
        moment it signals anything, the tablet can reboot, a pid can be
        recycled, or another publisher can win the race. The identity must still
        be the same incarnation, otherwise the caller proceeds from a fact that
        has already expired.
        """
        if not isinstance(observed, SupervisorIdentity):
            return False
        current = self.observe_active_supervisor()
        if not current.get("answered"):
            return False
        return observed.same_incarnation(current.get("identity"))

    def reconcile_location(self, observation: dict, *, generation=None,
                           manifest_id=None) -> dict:
        """Compare-and-swap the RECORDED location onto the observed one.

        Required by §4: a generation recovered into an alternative folder is
        accepted only with full proof of the supervisor AND the artifacts, and
        the observed location is reconciled in the active record before any
        handoff is negotiated. A folder that merely contains the same manifest
        is never adopted: identity (incarnation + lock ownership) and the
        recorded generation/manifest must all agree first.

        The write is a compare-and-swap: the recorded session must still be the
        one that was read when the swap was prepared, otherwise the record
        changed under us and nothing is written.
        """
        identity = observation.get("identity") if isinstance(observation, dict) else None
        session = observation.get("session") if isinstance(observation, dict) else None
        if not isinstance(identity, SupervisorIdentity) or not session:
            return {"reconciled": False, "reason": "no_observed_supervisor"}
        if not identity.lock_owned:
            return {"reconciled": False, "reason": "observed_supervisor_does_not_own_lock"}
        if identity.incarnation == "none":
            return {"reconciled": False, "reason": "observed_incarnation_unproven"}
        recorded_session = str(self.state.get("active_session") or "")
        recorded_generation = self.state.get("active_generation")
        recorded_manifest = str(self.state.get("active_manifest_id") or "")
        if not recorded_session:
            return {"reconciled": False, "reason": "no_recorded_session"}
        if generation is not None and recorded_generation != generation:
            return {"reconciled": False, "reason": "observed_generation_differs"}
        if manifest_id and identity.manifest_id != manifest_id:
            # Only the manifest the record already trusts is accepted; a folder
            # holding a DIFFERENT manifest is a different generation.
            return {"reconciled": False, "reason": "observed_manifest_differs"}
        if session == recorded_session:
            return {"reconciled": True, "reason": "location_already_reconciled",
                    "session": session}
        try:
            with self.state_lock():
                # CAS: EVERY field the proof was based on must be unchanged
                # since it was read above, not just the session. Reading the
                # generation and manifest outside the lock and re-checking only
                # the path would let a publication promoted in between be
                # attributed to the location we just adopted.
                if str(self.state.get("active_session") or "") != recorded_session:
                    return {"reconciled": False, "reason": "record_changed_concurrently"}
                if self.state.get("active_generation") != recorded_generation:
                    return {"reconciled": False, "reason": "record_changed_concurrently"}
                if str(self.state.get("active_manifest_id") or "") != recorded_manifest:
                    return {"reconciled": False, "reason": "record_changed_concurrently"}
                updates = {"active_session": session,
                           "session_reconciled_from": recorded_session,
                           "session_reconciled_at": self._now(),
                           "session_reconciled_incarnation": identity.incarnation}
                # Top-level state, exactly like the promotion path writes it:
                # `active_session` is read back from `self.state[...]` by every
                # confirmation and activation helper, so recording it inside the
                # publication journal would silently change nothing.
                self.state.update(updates)
                journal = self._journal()
                journal.update({"session_reconciled_from": recorded_session,
                                "session_reconciled_incarnation": identity.incarnation,
                                "updated_ts": self._now()})
                self.state["publication"] = journal
                self._save_state()
        except StateLockedError:
            return {"reconciled": False, "reason": "record_locked_by_another_publisher"}
        logger.warning("reconciled the recorded location of %s: %s -> %s",
                       identity.incarnation, recorded_session, session)
        return {"reconciled": True, "reason": "location_reconciled",
                "session": session, "from": recorded_session}

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
                     *, previous_session: str | None = None,
                     observed_incarnation: str | None = None) -> None:
        """Ask the tablet to publish our exclusive handoff lease.

        ``previous_session`` names the recorded generation a controlled handover
        would replace. ``observed_incarnation`` is what the caller actually saw
        with ``describe``; the tablet compares it against the live lock owner's
        own ``<pid>-<starttime>``.

        A path is a location, not an identity. If a live owner holds the lock and
        no observed incarnation is proven, the tablet refuses: authorising a
        divergent owner because a directory name appears in its command line is
        exactly the defect this parameter removes.
        """
        transport = self._require_transport()
        package_dir = self._package_dir_on_tablet(session)
        args = ["handoff-lease", package_dir, token, str(self.guard_ttl)]
        if previous_session and not _SESSION_RE.fullmatch(previous_session):
            raise PublicationError("invalid previous session name")
        if observed_incarnation:
            # The tablet reads the SESSION from $5 and the identity TOKEN from $6.
            # Exactly one of each is sent: appending the session in both branches
            # shifted the token onto the session field, and every handoff from a
            # live previous supervisor was refused for "no identity".
            if not _IDENTITY_TOKEN_RE.fullmatch(observed_incarnation):
                raise PublicationError("invalid observed identity token")
            args.append(previous_session or "")
            args.append(observed_incarnation)
        elif previous_session:
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

    def previous_supervisor_capability(self, previous_session: str,
                                       *, identity=None) -> tuple[bool, str]:
        """Can the RUNNING supervisor of ``previous_session`` take a handoff?

        The RUNNING supervisor's own ``describe`` announcement is the proof.
        Reading the *file* is not: a later revision can replace the script while
        the old process still supervises, which would make the file claim a
        protocol the running process does not implement.

        A revision that does not answer ``describe`` in the handoff protocol is
        refused, never signalled on the assumption that it might cope: killing a
        supervisor that cannot acknowledge is what strands the tablet in legacy
        mode.
        """
        if previous_session and not _SESSION_RE.fullmatch(previous_session):
            return False, "invalid_previous_session"
        if self.transport is None:
            return False, "no_tablet_transport"
        observed = identity
        observation_session = None
        if observed is None:
            observation = self.observe_active_supervisor()
            if not observation.get("answered"):
                reason = observation.get("reason") or "unidentified"
                if reason in ("no_lock_owner_registered",
                              "lock_owner_is_not_a_known_supervisor"):
                    # No live supervisor registered: nothing to take over, so a
                    # switch is free. This is the only case that skips the
                    # protocol proof, and it skips it because there is no
                    # process to signal.
                    return True, "no_previous_supervisor_running"
                return False, "previous_supervisor_capability_unreadable"
            observed = observation.get("identity")
            observation_session = observation.get("session")
        if not isinstance(observed, SupervisorIdentity):
            return False, "previous_supervisor_capability_unreadable"
        if previous_session and observation_session \
                and observation_session != previous_session:
            # The lock is held by a supervisor of ANOTHER session. It is not the
            # one this handoff would replace, so there is nothing of ours to
            # take over — but this is a location fact, never an authorisation to
            # signal it. A caller that expected the recorded session to be the
            # running one must reconcile the location first (see
            # ``reconcile_location``); signalling is only ever negotiated by
            # identity, inside ``handoff_takeover``.
            return True, "no_previous_supervisor_running"
        if observed.protocol == self.HANDOFF_PROTOCOL_ANNOUNCEMENT:
            return True, "ok"
        return False, "previous_supervisor_not_handoff_capable"

    def handoff_takeover(self, session: str, token: str,
                         previous_session: str, *,
                         observed=None) -> bool:
        """Stop the previous supervisor under our lease, then free the lock.

        The takeover is bound to the instance the caller OBSERVED: the tablet
        compares the lock owner's own incarnation against the observed one
        immediately before the signal, so a delayed, retried or replayed command
        cannot reach a process the caller never identified.

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
        identity = observed if isinstance(observed, SupervisorIdentity) else None
        if identity is None:
            # No instance was handed over. Observe NOW instead of signalling on
            # the strength of a directory name: a lock owner that cannot be
            # identified is exactly the case a signal must not reach.
            observation = self.observe_active_supervisor()
            if observation.get("answered"):
                identity = observation.get("identity")
            elif observation.get("reason") not in (
                    "no_lock_owner_registered",
                    "lock_owner_is_not_a_known_supervisor"):
                raise PreviousSupervisorNotUpgradable(
                    "no supervisor instance could be observed before the "
                    f"takeover ({observation.get('reason')})")
        capable, reason = self.previous_supervisor_capability(previous_session,
                                                              identity=identity)
        if not capable:
            raise PreviousSupervisorNotUpgradable(
                f"previous supervisor cannot accept a handoff ({reason})")
        # Revalidation under the guard, immediately before the mutation: the
        # tablet re-reads the lock owner itself, but the host must not even ask
        # if its own observation already expired (reboot, recycled pid, another
        # publisher winning the race between the decision and the signal).
        if identity is not None and not self.revalidate_supervisor(identity):
            raise PreviousSupervisorNotUpgradable(
                "the observed supervisor instance changed before the takeover")
        package_dir = self._package_dir_on_tablet(session)
        takeover_args = ["handoff-takeover", package_dir, token, previous_session]
        if identity is not None:
            # Binding the command to the observed instance: the tablet refuses a
            # command whose expectation no longer matches the live lock owner,
            # and it compares the FULL tuple (boot, instance, incarnation,
            # manifest, package digest) - a bare <pid>-<starttime> is refused as
            # unproven, which used to make every takeover impossible.
            if not identity.identity_is_complete():
                raise PreviousSupervisorNotUpgradable(
                    "the observed supervisor identity is incomplete; refusing "
                    "to signal on a fragment")
            takeover_args.append(identity.identity_token)
        command = self._script_command(package_dir, *takeover_args)
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
            #
            # Before anything is negotiated, identify the supervisor that is
            # ACTUALLY running, by the protected registry (the mode lock) and by
            # its own `describe` answer. The recorded session is a location; the
            # incarnation is the identity. A generation recovered into another
            # folder keeps its identity there, which is why the recorded
            # location is reconciled here instead of being trusted.
            observation = self.observe_active_supervisor()
            observed_identity = (observation.get("identity")
                                 if observation.get("answered") else None)
            observed_incarnation = (
                observed_identity.identity_token
                if isinstance(observed_identity, SupervisorIdentity)
                and observed_identity.identity_is_complete() else None)
            # Reconciliation and the identity veto apply to a HANDOFF. With no
            # recorded session there is no location to swap and no supervisor to
            # take over, so the tablet's own lease stays the only gate: it refuses
            # a live lock owner whose incarnation the caller did not prove.
            if previous_session and observation.get("answered"):
                reconciled = self.reconcile_location(
                    observation, generation=previous_generation,
                    manifest_id=previous_manifest_id or None)
                if not reconciled.get("reconciled"):
                    # The device and the record disagree about WHERE the running
                    # supervisor is. Neither may be assumed: nothing is
                    # signalled, the LUN is not touched, and the server-side
                    # services go back to the generation the TV is showing.
                    keeper.stop()
                    self._rollback_vps(guard_token=guard_token, session=session)
                    self._update_journal(
                        phase="needs_reconciliation",
                        last_error="session_location_not_reconciled",
                        manual_reconciliation_required=True)
                    return {"state": "needs_reconciliation",
                            "generation": generation,
                            "reason": "session_location_not_reconciled",
                            "interrupts_playback": False, "reconnected": False,
                            "message": "A TV usa uma sessão recuperada que não pôde "
                                       "ser reconciliada. A lista nova não foi "
                                       "aplicada; nada foi alterado na TV."}
                previous_session = str(reconciled.get("session") or previous_session)
            elif previous_session and observation.get("reason") in (
                    "lock_owner_cmdline_unreadable",
                    "supervisor_did_not_answer describe",
                    "supervisor_incarnation_disagrees_with_lock_owner"):
                # A live lock owner that nobody can identify is conflicting data,
                # not a free device: refusing here is what keeps a blind takeover
                # from signalling a process it never proved.
                keeper.stop()
                self._rollback_vps(guard_token=guard_token, session=session)
                self._update_journal(
                    phase="needs_reconciliation",
                    last_error="session_identity_conflicting",
                    manual_reconciliation_required=True)
                return {"state": "needs_reconciliation", "generation": generation,
                        "reason": "session_identity_conflicting",
                        "interrupts_playback": False, "reconnected": False,
                        "message": "O supervisor ativo não pôde ser identificado. "
                                   "A lista nova não foi aplicada; nada foi "
                                   "alterado na TV."}
            handoff_token = hashlib.sha256(
                f"{session}:{generation}:{self._now()}".encode("utf-8")).hexdigest()[:32]
            self.handoff_lease(session, handoff_token, previous_session=previous_session,
                               observed_incarnation=observed_incarnation)
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
                        session, handoff_token, previous_session,
                        observed=observed_identity)
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


# Field boundaries in the tablet's ``describe`` line. A value may contain
# spaces (``protocol=handoff.ack 1``, a package path with spaces), so the split
# is on `` <name>=`` and never on bare whitespace.
_IDENTITY_FIELD_RE = re.compile(r"(?:^|\s)([a-z_]+)=")


class SupervisorIdentity:
    """Identity of the supervisor ACTUALLY running on the tablet.

    This is an *incarnation*, not a location. ``supervisor`` is
    ``<pid>-<starttime>`` as reported by the tablet's own ``proc_incarnation``: a
    pid alone is reused by the kernel, so the start time is what distinguishes
    one process lifetime from the next. A generation recovered into an
    alternative directory supervises the same content from elsewhere, so
    correlating by path is exactly what refused publication 19 — this object
    makes the incarnation comparable instead.

    Every field is what the tablet reported. A missing field stays ``none`` and
    is never filled in by guessing.
    """

    __slots__ = ("protocol", "boot_id", "generation", "manifest_id",
                 "package_digest", "runtime_digest", "supervisor", "candidate",
                 "identity", "instance_id",
                 "lun", "lock_owned", "path")

    FIELDS = __slots__

    def __init__(self, fields: dict):
        for name in self.__slots__:
            setattr(self, name, str(fields.get(name, "none")))
        self.lock_owned = self.lock_owned == "1"

    @classmethod
    def parse(cls, output: str):
        """Parse the single ``describe ...`` line, or return None.

        Fields are split on `` <name>=`` boundaries rather than on whitespace: a
        value may legitimately contain spaces (the protocol announcement is
        ``handoff.ack 1``, and a package path may contain spaces). Splitting on
        whitespace would silently truncate both.

        Only an answer carrying at least the protocol and the incarnation is
        accepted; a truncated or unrelated reply yields None rather than a
        half-built identity a caller might trust.
        """
        for line in (output or "").splitlines():
            line = line.strip()
            if not line.startswith("describe "):
                continue
            body = line[len("describe "):]
            matches = list(_IDENTITY_FIELD_RE.finditer(body))
            if not matches:
                return None
            fields = {}
            for index, match in enumerate(matches):
                start = match.end()
                end = (matches[index + 1].start()
                       if index + 1 < len(matches) else len(body))
                fields[match.group(1)] = body[start:end].strip()
            if "protocol" not in fields or "supervisor" not in fields:
                return None
            return cls(fields)
        return None

    @property
    def incarnation(self) -> str:
        return self.supervisor

    @property
    def identity_token(self) -> str:
        """The token the tablet compares under the guard.

        Preferred source is the ``identity=`` field the running supervisor
        reports, because it is exactly what the tablet will recompute. When an
        older reply lacks it, the token is assembled from the individual fields
        in the same order; if any component is a placeholder the result is NOT
        complete and the caller must refuse instead of sending a fragment.
        """
        if isinstance(self.identity, str) and self.identity not in ("", "none"):
            return self.identity
        return "|".join([self.boot_id, self.instance_id, self.supervisor,
                         self.manifest_id, self.package_digest])

    def identity_is_complete(self) -> bool:
        token = self.identity_token
        if not _IDENTITY_TOKEN_RE.fullmatch(token):
            return False
        return "none" not in token.split("|")

    def same_incarnation(self, other: "SupervisorIdentity | None") -> bool:
        """True only when both name the SAME running process.

        ``none`` never matches: an unproven incarnation is not evidence of
        sameness, and treating two unknowns as equal is how a stale marker gets
        accepted.
        """
        return (isinstance(other, SupervisorIdentity)
                and self.incarnation != "none"
                and self.incarnation == other.incarnation)

    def __repr__(self) -> str:
        return ("SupervisorIdentity(protocol=%s generation=%s manifest=%s "
                "incarnation=%s lock_owned=%s)" % (
                    self.protocol, self.generation, self.manifest_id,
                    self.incarnation, self.lock_owned))


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

    Everything in ``phase``/``finish`` is best-effort on purpose: notification
    failure must never block a publication. Coordination is the opposite — see
    :meth:`claim`, which fails closed.
    """

    def __init__(self, tablet_id: str, task_id: str | None = None,
                 revision: str | int | None = None,
                 executor: str | None = None,
                 operation_id: str | None = None):
        self.tablet_id = tablet_id
        # A taskless publication (inventory refresh) legitimately has NO task id:
        # it must still be able to adopt ITS OWN operation, so "no task" is a
        # value to compare, not a reason to refuse.
        self.task_id = str(task_id) if task_id else None
        self.revision = str(revision) if revision not in (None, "") else None
        # The durable operation the caller created. When present it is the
        # PRIMARY correlation: the row is read by id and proved to be this task
        # and this revision, instead of adopting whatever happens to be active
        # for the tablet (which proves only that SOME operation holds the slot).
        self.operation_id = str(operation_id) if operation_id else None
        # The execution identity is this PROCESS: host + <pid>-<starttime>. A pid
        # is recycled, so the start time is what makes one worker run
        # distinguishable from the next, and the CAS in the store binds the
        # operation to it instead of assuming "the worker that ran".
        self.executor = executor or _local_execution_identity()
        self._store = None
        self._effect_started = False
        self._terminal_state = None
        self.adopted_from_dead = False
        self._configured = bool(os.environ.get("MIXED_OPERATIONS_DB", "").strip())

    def claim(self) -> str:
        """Establish ownership BEFORE the interruptive effect. Fails closed.

        Three outcomes, and the distinction is the whole point:

        ``legacy``   no store configured, so coordination is not required and
                     existing deployments stay byte-identical;
        ``owned``    this publication holds a recorded operation for THIS task;
        ``blocked``  coordination was required and could not be proven. Acting
                     on the device with no recorded owner is worse than not
                     acting, so the caller aborts before any effect.

        Correlation is ``(tablet_id, task_id, revision)`` **proven by a CAS that
        records this execution**, never by tablet alone: the slot is unique per
        tablet, so adopting whatever happens to be active would attach this run
        to an operation meant for a different task or revision.
        """
        if not self._configured:
            return "legacy"
        try:
            import mixed_operations
            self._store = mixed_operations.OperationStore(
                os.environ["MIXED_OPERATIONS_DB"].strip())
        except Exception as exc:
            logger.error("coordination store configured but unusable (%s); "
                         "refusing to publish", type(exc).__name__)
            self._store = None
            return "blocked"
        if self.operation_id:
            return self._claim_by_id()
        try:
            active = self._store.active_for_tablet(self.tablet_id)
        except Exception as exc:
            logger.error("could not read the active operation (%s); refusing "
                         "to publish", type(exc).__name__)
            return "blocked"
        if active is not None:
            match = self._intention_matches(active)
            if match != "ok":
                logger.error("tablet %s is publishing task %r revision %r while "
                             "this run is task %r revision %r (%s); refusing to "
                             "attach to another intention",
                             self.tablet_id, active.get("task_id"),
                             active.get("intent_revision"), self.task_id,
                             self.revision, match)
                return "blocked"
            self.operation_id = active["operation_id"]
            # Proving the association, not assuming it: the row must carry this
            # task and (when both sides stated one) this revision, and the claim
            # records WHICH execution owns it. A row already owned by another
            # execution is refused rather than adopted.
            try:
                self._store.claim_execution(
                    self.operation_id, task_id=self.task_id,
                    revision=self.revision, executor=self.executor)
            except Exception as exc:
                logger.error("operation %s does not belong to this execution "
                             "(%s: %s); refusing to publish",
                             self.operation_id, type(exc).__name__, exc)
                return "blocked"
            return "owned"
        # Nothing active: record this publication's intent here, so a durable
        # owner exists before any effect rather than after it.
        try:
            self.operation_id, _created = self._store.begin(
                tablet_id=self.tablet_id, principal="publisher",
                intention={"task_id": self.task_id, "operation": "apply",
                           "revision": self.revision},
                desired_generation=(int(self.revision)
                                    if (self.revision or "").isdigit() else None),
                user_message="Publicação iniciada pelo publicador")
            self._store.claim_execution(
                self.operation_id, task_id=self.task_id,
                revision=self.revision, executor=self.executor)
        except Exception as exc:
            logger.error("could not record the operation before publishing "
                         "(%s); refusing to publish", type(exc).__name__)
            return "blocked"
        return "owned"

    def _intention_matches(self, row: dict) -> str:
        """Does this row describe THIS run's intention?

        Both sides are normalised, so a taskless refresh (no task id anywhere)
        matches instead of being refused for lacking an id it never had. A
        mismatch names the first field that differs, because "blocked" with no
        reason is not investigable.
        """
        mine_task = self.task_id or None
        row_task = row.get("task_id") or None
        if row_task != mine_task:
            return "task_differs"
        row_revision = row.get("intent_revision")
        if self.revision is not None and row_revision is not None \
                and str(row_revision) != str(self.revision):
            return "revision_differs"
        return "ok"

    @staticmethod
    def _executor_is_dead(token: str | None) -> bool:
        """Can we PROVE the recorded execution no longer exists?

        ``executor`` is ``host:<pid>-<starttime>``. Proof is only possible for
        our own host: a pid that is gone, or one that was recycled (the start
        time differs). Another host's pid says nothing here, so it stays
        unproven and the caller must not steal.

        This is what makes a crash survivable: an OOM/SIGKILL/reboot leaves the
        row active with a dead executor, and the next worker either adopts it
        (proof of death) or refuses. Without it the tablet's slot stayed held
        for ever - the same symptom B1 had, through another door.
        """
        if not token:
            return False
        host, _, rest = str(token).partition(":")
        if not host or host != socket.gethostname()[:32]:
            return False
        pid_text = rest.split("-", 1)[0]
        if not pid_text.isdigit():
            return False
        pid = int(pid_text)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return True                      # the process is gone
        fields = stat.rsplit(")", 1)[-1].split()
        start = fields[19] if len(fields) > 19 else ""
        recorded_start = rest.split("-", 1)[1] if "-" in rest else ""
        # A recycled pid keeps the number and changes the start time.
        return bool(recorded_start) and start != recorded_start

    def _claim_by_id(self) -> str:
        """Adopt the operation the caller created, after proving it is OURS.

        Read by id, then check the intention and finally the execution CAS. A
        terminal row means the work this click asked for is already decided:
        republishing it would be a second activation behind the same id.
        """
        try:
            row = self._store.get(self.operation_id)
        except Exception as exc:
            logger.error("could not read operation %s (%s); refusing to publish",
                         self.operation_id, type(exc).__name__)
            return "blocked"
        if row.get("tablet_id") != self.tablet_id:
            logger.error("operation %s belongs to another tablet; refusing",
                         self.operation_id)
            return "blocked"
        match = self._intention_matches(row)
        if match != "ok":
            logger.error("operation %s does not match this run (%s); refusing",
                         self.operation_id, match)
            return "blocked"
        if row.get("state") in _TERMINAL_OPERATION_STATES:
            self._terminal_state = row.get("state")
            return "already_terminal"
        try:
            self._store.claim_execution(
                self.operation_id, task_id=self.task_id,
                revision=self.revision, executor=self.executor)
        except Exception as exc:
            recorded = row.get("executor")
            if not self._executor_is_dead(recorded):
                logger.error("operation %s does not belong to this execution "
                             "(%s: %s) and its owner is not provably dead; "
                             "refusing to publish",
                             self.operation_id, type(exc).__name__, exc)
                return "blocked"
            # The recorded execution is GONE: take the operation over instead of
            # leaving the tablet's slot held until someone edits SQLite.
            logger.warning("operation %s was owned by a dead execution (%s); "
                           "adopting it", self.operation_id, recorded)
            try:
                self._store.claim_execution(
                    self.operation_id, task_id=self.task_id,
                    revision=self.revision, executor=self.executor,
                    steal_dead=True)
            except Exception as steal_exc:
                logger.error("could not adopt operation %s from a dead "
                             "execution (%s); refusing to publish",
                             self.operation_id, type(steal_exc).__name__)
                return "blocked"
            self.adopted_from_dead = True
        return "owned"

    def mark_effect_started(self) -> None:
        """Record that an interruptive effect may now have happened.

        From this point on a coordination failure is no longer a clean failure:
        the device may have been touched, so the outcome is unknown and must be
        reconciled instead of reported as ``failed_safe``.
        """
        self._effect_started = True

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

    def finish(self, state: str, message: str, **kw) -> str:
        """Record the result. Returns how far it could be recorded.

        ``recorded``            the row now carries this result;
        ``unrecorded_after_effect`` the write failed AND an interruptive effect
                                may already have happened. The caller must not
                                claim a safe failure: the device has to be
                                reconciled, and the caller says so.
        ``unrecorded``          the write failed before any effect, which is a
                                clean failure (nothing was touched).

        Notification failure never blocks a publication; coordination failure
        after a possible effect must change what the operator is told.
        """
        if not self.active:
            return "not_tracked"
        try:
            self._store.finish(self.operation_id, state=state,
                               user_message=message, **kw)
            return "recorded"
        except Exception as exc:
            logger.warning("could not record operation result %s (%s)",
                           state, type(exc).__name__)
        if not self._effect_started:
            return "unrecorded"
        # The result could not be written although the device may have been
        # touched. Writing "needs_reconciliation" is a second, independent
        # attempt: if it also fails, the caller still must not pretend the
        # publication failed safely.
        try:
            self._store.finish(self.operation_id, state="needs_reconciliation",
                               user_message="O resultado não pôde ser registrado "
                                            "depois de a TV possivelmente ter "
                                            "sido alterada; reconciliar.")
            return "recorded_needs_reconciliation"
        except Exception as exc:
            logger.error("operation result AND reconciliation marker could not "
                         "be recorded (%s); the caller must report an unknown "
                         "outcome", type(exc).__name__)
            return "unrecorded_after_effect"


# States in which an operation's work is already decided. Reached only through
# adoption BY ID: a duplicate click must report the result, never republish it.
_TERMINAL_OPERATION_STATES = frozenset({"succeeded", "already_current",
                                        "failed_safe", "blocked", "superseded"})


def _local_execution_identity() -> str:
    """Identity of THIS worker run: host + ``<pid>-<starttime>``.

    The same primitive the tablet uses for its supervisor: a pid alone is
    recycled by the kernel, so the start time is what distinguishes one run from
    the next. It is deliberately short and free of secrets so it can be stored
    and shown; it identifies a run, never a user.
    """
    pid = os.getpid()
    start = "unknown"
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        rest = stat.rsplit(")", 1)[-1].split()
        start = rest[19] if len(rest) > 19 else "unknown"
    except (OSError, IndexError, ValueError):
        pass
    return f"{socket.gethostname()[:32]}:{pid}-{start}"


# Reasons the publisher reports as "unknown" WITHOUT having touched the device:
# the catalog could not be read, so nothing was prepared and nothing was
# activated. Reporting those as `needs_reconciliation` would hold the tablet's
# single slot for an effect that never happened - the operator would be told to
# reconcile a TV nobody touched. They are clean failures with a visible message.
_PRE_EFFECT_UNKNOWN_REASONS = frozenset({"inventory_unavailable"})


def _operation_outcome_for(state: str, reason: str | None = None) -> str:
    """Map an apply result onto an operation outcome, failing closed.

    An unrecognised state is the ambiguous case by definition: the device may
    have been touched and this code cannot say what happened. Returning the last
    active state would hold the tablet slot forever, so it becomes
    ``needs_reconciliation`` — the caller must look at the device, not guess.
    """
    if state == "unknown" and reason in _PRE_EFFECT_UNKNOWN_REASONS:
        return "failed_safe"
    outcome = _OPERATION_OUTCOME.get(state)
    if outcome is None:
        logger.warning("unrecognised apply state %r; marking for reconciliation",
                       state)
        return "needs_reconciliation"
    return outcome


def _tracked_publication(service, args, *, task_id, run):
    """Claim, run and record a publication — for BOTH apply and refresh.

    The refresh used to return before this point, so the operation the endpoint
    created was never adopted and never finished: the row stayed in `preparing`
    forever, holding the tablet's single slot and hiding the problem (that state
    is active but not an "unresolved" one). The coordination is identical for a
    task anchor and for an inventory refresh, so it lives in one place.
    """
    tracker = _OperationTracker(
        _publisher_tablet_id(), task_id,
        revision=service.state.get("pending_generation"),
        operation_id=getattr(args, "operation_id", None))
    decision = tracker.claim()
    if decision == "already_terminal":
        terminal = {"state": tracker._terminal_state or "succeeded",
                    "generation": None, "interrupts_playback": False,
                    "reconnected": False, "duplicate": True,
                    "operation_id": tracker.operation_id,
                    "reason": "operation_already_terminal",
                    "message": "Esta atualização já foi concluída; nada foi "
                               "alterado na TV."}
        print(json.dumps(terminal, indent=2) if args.json
              else terminal["message"])
        return 0
    if decision == "blocked":
        payload = {"state": "blocked", "generation": None,
                   "interrupts_playback": False, "reconnected": False,
                   "reason": "coordination_unavailable",
                   "message": "Não foi possível registrar a operação antes de "
                              "publicar; nada foi alterado na TV."}
        print(json.dumps(payload, indent=2) if args.json
              else payload["message"])
        return 3
    tracker.phase("preparing", "publication", "Preparando a publicação")
    # From here on the device may be touched, so a coordination failure stops
    # being a clean failure (see ``_OperationTracker.finish``).
    tracker.mark_effect_started()
    try:
        result = run()
    except StateLockedError:
        # Another publisher already owns the state: report idempotently instead
        # of failing (a double click must never look like a broken update).
        result = {"state": "in_progress", "generation": None,
                  "interrupts_playback": True, "reason": "state_locked",
                  "message": "⏳ Atualização da lista já está em andamento"}
    state = str(result.get("state") or "unknown")
    recorded = tracker.finish(_operation_outcome_for(state, result.get("reason")),
                              str(result.get("message") or state),
                              error_code=result.get("reason"),
                              reconnected=bool(result.get("reconnected")))
    if recorded == "unrecorded_after_effect":
        # The result could not be persisted although the TV may already have been
        # altered. Claiming a clean failure here would be a fabrication, so the
        # caller is told the outcome is unknown and must be reconciled.
        result = {"state": "needs_reconciliation", "generation": None,
                  "interrupts_playback": True, "reconnected": False,
                  "reason": "coordination_unrecorded_after_effect",
                  "message": "A alteração pode ter ocorrido, mas o resultado não "
                             "pôde ser registrado. Confira o estado da TV antes de "
                             "tentar novamente."}
    print(json.dumps(result, indent=2) if args.json
          else result.get("message") or result.get("state"))
    return 0 if result.get("state") in ("active", "already_current",
                                        "in_progress") else 1


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
        # Same tracked path as `apply`: a refresh is a publication, so its
        # operation must be adopted and finished, not left holding the slot.
        return _tracked_publication(
            service, args, task_id="",
            run=lambda: service.refresh_inventory(
                manual=True, confirm_interrupt=args.confirm_interrupt))
    if args.background:
        detached = [sys.executable, str(Path(__file__).resolve()), "--apply",
                    "--task-id", str(args.task_id), "--json"]
        if getattr(args, "operation_id", ""):
            # The child is a DIFFERENT process: without this it would fall back to
            # "whatever is active for the tablet", losing the proof that it is
            # publishing the operation the endpoint created.
            detached += ["--operation-id", str(args.operation_id)]
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
    # Coordination before any effect: if the store is configured but ownership
    # cannot be recorded, refuse. Acting on the device with no durable owner is
    # worse than not acting.
    return _tracked_publication(
        service, args, task_id=str(args.task_id),
        run=lambda: service.apply_task(str(args.task_id),
                                       confirm_interrupt=args.confirm_interrupt))




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
    parser.add_argument("--operation-id", default="",
                        help="durable operation to adopt (set by the endpoint; "
                             "proves this task/revision instead of guessing)")
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
