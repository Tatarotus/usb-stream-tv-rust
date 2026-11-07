"""Tests for the mixed-media generation service: strict telemetry, trusted
fingerprints, single-writer state and real, recoverable publication.

Publication is exercised against a fake device driven through the real
``TabletTransport`` command construction: the test asserts the exact commands
the tablet receives and simulates the device responses, so the state machine
(transfer -> preflight -> activate -> confirm -> promote) is validated without
mocking any internal function.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import build_mixed_media_package as pkg_builder  # noqa: E402
import mixed_generation_service as mgs  # noqa: E402

SESSION = "mixed-gen2-aaaaaaaa"


def idle_status(**overrides):
    """A complete, fresh, provably idle payload."""
    status = {
        "active_vod": None,
        "tv_on": False,
        "tv_usb_state": "DISCONNECTED",
        "in_standby": True,
        "listeners": 0,
        "stream_bitrate_kbps": 0.0,
        "tablet_online": True,
        "server_time": 1000.0,
    }
    status.update(overrides)
    return status


def write_manifest(staging: Path, generation: int,
                  extra_vod: tuple[tuple[str, int, str], ...] = (),
                  *, include_default_vod: bool = True) -> str:
    entries = [
        {
            "file_id": pkg_builder.stable_file_id("live", "206080"),
            "channel_id": "206080",
            "namespace": "live",
            "kind": "live",
            "folder": ["Abertos"],
            "filename": "SBT.trp",
            "logical_size": 4_000_000_000,
            # The real bootstrap rule: publication now proves the staged manifest
            # against the Live export, exactly like the manager does.
            "bootstrap_id": "bootstrap-%08x" % pkg_builder.stable_file_id("live", "206080"),
        },
    ]
    if include_default_vod:
        entries.append({
            "file_id": pkg_builder.stable_file_id("vod", "a1b2c3d4"),
            "channel_id": "a1b2c3d4",
            "namespace": "vod",
            "kind": "vod",
            "folder": ["Filmes"],
            "filename": "Film.mp4",
            "logical_size": 4 * 1024 * 1024,
            "bootstrap_id": "",
            "content_revision": "md5:" + "a" * 32,
        })
    for task_id, size, md5 in extra_vod:
        entries.append({
            "file_id": pkg_builder.stable_file_id("vod", task_id),
            "channel_id": task_id,
            "namespace": "vod",
            "kind": "vod",
            "folder": ["Filmes"],
            "filename": f"{task_id}.mp4",
            "logical_size": size,
            "bootstrap_id": "",
            "content_revision": "md5:" + md5,
        })
    raw, sorted_entries = pkg_builder.canonical_manifest_bytes(entries, generation=generation)
    manifest_id = hashlib.sha256(raw).hexdigest()
    (staging / "favorites_manifest.json").write_text(json.dumps({
        "schema_version": 1, "layout_version": 1, "generation": generation,
        "manifest_id": manifest_id, "entries": sorted_entries,
    }, indent=2) + "\n")
    # The generation contract requires the allowlist to agree with the manifest
    # exactly, so both are produced from the same entries.
    tasks = [{"task_id": e["channel_id"], "size": e["logical_size"],
              "revision": e["content_revision"]}
             for e in sorted_entries if e["kind"] == "vod"]
    (staging / "vod_allowlist.json").write_text(json.dumps({"tasks": tasks}, indent=2) + "\n")
    return manifest_id


def write_sums(staging: Path) -> None:
    names = list(pkg_builder.ARTIFACTS) + list(mgs.REQUIRED_RUNTIME)
    lines = []
    for name in sorted(set(names)):
        digest = hashlib.sha256((staging / name).read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}")
    (staging / "SHA256SUMS").write_text("\n".join(lines) + "\n")


SUPERVISOR_BODY = b"#!/system/xbin/busybox sh\n# handoff.ack acceptance record\n"
LEGACY_SUPERVISOR_BODY = b"#!/system/xbin/busybox sh\n# older revision: no handoff protocol\n"


def write_runtime_files(staging: Path, *, legacy_script: bool = False) -> None:
    """Write the runtime artifacts a package ships.

    ``activate_live_favorites_tablet.sh`` carries the handoff acceptance
    protocol, which is exactly what the capability gate looks for; a package
    built by an older revision has no marker.
    """
    body = LEGACY_SUPERVISOR_BODY if legacy_script else SUPERVISOR_BODY
    for name in mgs.REQUIRED_RUNTIME:
        if name == "activate_live_favorites_tablet.sh":
            (staging / name).write_bytes(body)
        else:
            (staging / name).write_bytes(f"runtime:{name}\n".encode())


def make_complete_package(base: Path, generation: int = 2, session: str = SESSION,
                          *, legacy_script: bool = False) -> tuple[Path, str]:
    staging = base / session
    staging.mkdir(parents=True)
    for name in pkg_builder.ARTIFACTS:
        (staging / name).write_bytes(f"artifact:{name}\n".encode())
    write_runtime_files(staging, legacy_script=legacy_script)
    manifest_id = write_manifest(staging, generation)
    (staging / "vod_allowlist.json").write_text(json.dumps({"tasks": [{
        "task_id": "a1b2c3d4", "size": 4 * 1024 * 1024,
        "revision": "md5:" + "a" * 32,
    }]}))
    write_sums(staging)
    return staging, manifest_id


class FakeGuard:
    """Emulates the server-side exclusive mode guard."""

    def __init__(self, *, busy: bool = False, lose_after: int | None = None,
                 unreachable: bool = False, release_ok: bool = True,
                 lose_when=None):
        self.token = "f" * 32
        self.busy = busy
        self.unreachable = unreachable
        self.lose_after = lose_after
        # Predicate evaluated on every renewal: once true the lease is lost for
        # good, which is how a real expiry/mismatch behaves.
        self.lose_when = lose_when
        self.release_ok = release_ok
        self.renew_count = 0
        self.held = False
        self.calls: list[str] = []

    def __call__(self, action, *, token=None, ttl=None, phase=None):
        self.calls.append(action)
        if self.unreachable:
            raise mgs.PublicationError("mode guard unreachable: URLError")
        if action == "acquire":
            if self.busy:
                return {"ok": False, "busy": True,
                        "holder": {"owner": "other", "phase": "publishing"}}
            self.held = True
            return {"ok": True, "token": self.token, "expires_in": ttl or 60,
                    "owner": "mixed-publisher", "phase": phase or "publishing"}
        if action == "renew":
            self.renew_count += 1
            if self.lose_when is not None and self.lose_when():
                return {"ok": False, "expired": True}
            if self.lose_after is not None and self.renew_count > self.lose_after:
                return {"ok": False, "expired": True}
            return {"ok": True, "expires_in": 60}
        if action == "release":
            if token != self.token:
                return {"ok": False}
            if not self.release_ok:
                return {"ok": False, "unacknowledged": True}
            self.held = False
            return {"ok": True}
        return {"held": self.held,
                "owner": "mixed-publisher" if self.held else None,
                "phase": "publishing" if self.held else None, "expires_in": 60}


TASK_READY = {"id": "a1b2c3d4", "status": "ready", "file_size": 4 * 1024 * 1024,
              "title": "Film", "remote_md5": "a" * 32}


class FakeDevice:
    """Simulates the tablet behind the real adb command surface."""

    def __init__(self, *, preflight_ok: bool = True, confirm: bool = True,
                 fail_push: bool = False, already_active: bool = False,
                 refuse_lease: bool = False, refuse_takeover: bool = False,
                 vps_start_ok: bool = True, unreachable: bool = False,
                 remote_generation_exists: bool = False,
                 vps_restore_failed: bool = False,
                 refuse_renew: bool = False, tunnel_ok: bool = True,
                 takeover_lost_answer: bool = False,
                 inherit_old_script: bool = False):
        self.files = {}
        self.commands = []
        self.active = already_active
        self.preflight_ok = preflight_ok
        self.confirm = confirm
        self.fail_push = fail_push
        self.refuse_lease = refuse_lease
        self.refuse_takeover = refuse_takeover
        self.vps_start_ok = vps_start_ok
        self.vps_restore_failed = vps_restore_failed
        self.refuse_renew = refuse_renew
        self.tunnel_ok = tunnel_ok
        self.takeover_lost_answer = takeover_lost_answer
        # True models a session published by an OLDER revision: its script has
        # no handoff acceptance protocol.
        self.inherit_old_script = inherit_old_script
        # A live mode lock belongs to some supervisor of the active session.
        self.lock_owner = True
        self.lock_session = SESSION
        self.unreachable = unreachable
        self.preflight_calls = 0
        self.start_calls = 0
        self.rollback_calls = 0
        self.takeovers: list[str] = []
        self.handoff_leases: list[str] = []
        self.lease_renewals: list[str] = []
        self.vps_starts: list[str] = []
        self.vps_stops: list[str] = []
        # Every accepted switch invocation (argv tail), for order/flag proofs.
        self.sessions_started: list[list[str]] = []
        # Simulated remote (VPS) filesystem for the package promotion step.
        self.remote_generation_exists = remote_generation_exists
        self.remote_files: dict[str, bytes] = {}

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _local_to_device(path: str) -> str:
        return path.rstrip("/")

    # The script a CURRENT package ships. The marker is what the handoff
    # capability gate looks for; a session published by an older revision has
    # no marker (see ``inherit_old_script``).
    SUPERVISOR_BODY = b"#!/system/xbin/busybox sh\n# handoff.ack acceptance record\n"
    LEGACY_BODY = b"#!/system/xbin/busybox sh\n# (older revision, no handoff protocol)\n"

    MANIFEST_ID = "b9d04e0a" * 8

    def supervisor_body(self) -> bytes:
        return self.LEGACY_BODY if self.inherit_old_script else self.SUPERVISOR_BODY

    def _read_device(self, path: str) -> bytes | None:
        path = self._local_to_device(path)
        stored = self.files.get(path)
        if stored is not None:
            return stored
        if path == "/data/local/tmp/mode_switch.lock/pid":
            # The lock exists while a supervisor of the armed session runs.
            if self.inherit_old_script or self.lock_owner:
                return b"4242\n"
            return None
        if path.endswith("/activation/handoff.protocol"):
            # Written by the RUNNING supervisor when it understands the
            # handoff protocol; an older revision never writes it.
            if self.inherit_old_script:
                return None
            return b"handoff.ack 1\n"
        if path.endswith("/activate_live_favorites_tablet.sh"):
            return self.supervisor_body()
        return None

    def __call__(self, argv, timeout):
        self.commands.append(list(argv))
        if self.unreachable:
            raise OSError("device unreachable")
        if argv and argv[0] in ("ssh", "scp"):
            return self._remote(argv)
        # The local generation switch is invoked through `sudo -n -E sh ...` so
        # it can supersede services owned by another user. Recognize both: the
        # bare form keeps older expectations readable, the prefixed form proves
        # the privilege fix is really in the argv.
        if argv and (argv[0] == "sh" or (argv[0] == "sudo" and "sh" in argv)):
            return self._vps(argv)
        if len(argv) > 6 and argv[3] == "push":
            source, target = argv[5], argv[6]
            if self.fail_push:
                return CompletedProcess(argv, 1, "", "push failed")
            src = Path(source[:-2]) if source.endswith("/.") else Path(source)
            for item in src.iterdir():
                if item.is_file():
                    self.files[self._local_to_device(target) + "/" + item.name] = item.read_bytes()
            return CompletedProcess(argv, 0, "", "")
        if len(argv) >= 2 and argv[-2] == "-c":
            return self._shell(argv[-1], argv)
        return CompletedProcess(argv, 0, "", "")

    def _shell(self, command: str, argv):
        import shlex
        tokens = shlex.split(command)
        if tokens and tokens[0] == "cat" and len(tokens) == 2:
            data = self._read_device(tokens[1])
            if data is None:
                return CompletedProcess(argv, 1, "", "cat: no such file")
            return CompletedProcess(argv, 0, data.decode("utf-8", "replace"), "")
        if "/proc/" in command and "/cmdline" in command:
            pid = command.split("/proc/")[1].split("/")[0]
            return CompletedProcess(
                argv, 0,
                "busybox\nsh\n/data/local/tmp/live-favorites"
                f"/{self.lock_session}/activate_live_favorites_tablet.sh\nstart\n", "")
        if "describe" in tokens and any(
                t.endswith("/activate_live_favorites_tablet.sh") for t in tokens):
            # The RUNNING revision announces its own identity. An older revision
            # has no such action at all, which is what the "not handoff capable"
            # refusal is based on: the file on disk is never the evidence.
            index = tokens.index("describe")
            path = tokens[index + 1] if len(tokens) > index + 1 else ""
            protocol = "none" if self.inherit_old_script else "handoff.ack 1"
            locked = 1 if (self.inherit_old_script or self.lock_owner) else 0
            # The real action reads the generation and manifest out of the
            # package it is describing, so the fake must too: reporting "none"
            # would make every reconciliation look like a foreign generation.
            generation = manifest_id = "none"
            raw = self._read_device(path + "/favorites_manifest.json")
            if raw:
                import json as _json
                try:
                    data = _json.loads(raw.decode("utf-8", "replace"))
                    generation = str(data.get("generation", "none"))
                    manifest_id = str(data.get("manifest_id", "none"))
                except ValueError:
                    pass
            # A REAL supervisor reports every identity component and the token it
            # computes from them; the host now refuses a fragment, so the fake
            # must describe itself exactly like the tablet script does. A device
            # without a package manifest cannot be part of a real handoff at all
            # (the package is validated before activation), so the fake reports
            # the manifest id of the generation it is serving.
            if str(manifest_id) == "none":
                manifest_id = FakeDevice.MANIFEST_ID
            token = "|".join(["fakeboot", "a" * 32, "4242-999", str(manifest_id),
                              "c" * 32])
            return CompletedProcess(
                argv, 0,
                f"describe protocol={protocol} boot_id=fakeboot "
                f"generation={generation} manifest_id={manifest_id} "
                f"package_digest={'c' * 32} runtime_digest={'d' * 32} "
                f"supervisor=4242-999 candidate=4242-999 lun=none "
                f"lock_owned={locked} instance_id={'a' * 32} "
                f"identity={token} path={path}\n", "")
        if "sha256sum" in command:
            package = None
            if "cd" in tokens:
                package = tokens[tokens.index("cd") + 1]
            return CompletedProcess(argv, self._verify_sums(package), "", "")
        if "tunnel-check" in tokens:
            if not self.tunnel_ok:
                # Exit status 0 with no success marker: on this tablet adb/su
                # masks failures, so this is the realistic "silent failure".
                return CompletedProcess(argv, 0, "", "")
            return CompletedProcess(
                argv, 0, "[favorites] live (18190) and vod (18191) reachable "
                         "through loopback\n", "")
        if "tunnel-start" in tokens:
            return CompletedProcess(
                argv, 0, "[favorites] dedicated dual loopback tunnels tested "
                         "(live=18190 vod=18191); watchdog Chisel preserved\n", "")
        if "preflight" in tokens:
            self.preflight_calls += 1
            if not self.preflight_ok:
                return CompletedProcess(
                    argv, 0, "", "[favorites] media live tunnel on port 18190 not "
                                 "reachable (preflight failed)\n")
            return CompletedProcess(
                argv, 0, "[favorites] preflight ok (live=18190 vod=18191; real byte "
                         "range verified through the tunnel)\n", "")
        if "handoff-lease" in tokens:
            index = tokens.index("handoff-lease")
            token = tokens[index + 2]
            if self.refuse_lease:
                return CompletedProcess(argv, 1, "", "lease refused")
            self.handoff_leases.append(token)
            return CompletedProcess(argv, 0, f"handoff-lease token={token} expires_in=60\n", "")
        if "handoff-release" in tokens:
            return CompletedProcess(argv, 0, "handoff-release released=1\n", "")
        if "handoff-renew" in tokens:
            index = tokens.index("handoff-renew")
            token = tokens[index + 2]
            ttl = tokens[index + 3]
            if self.refuse_lease or self.refuse_renew:
                return CompletedProcess(argv, 1, "", "lease gone\n")
            self.lease_renewals.append(token)
            return CompletedProcess(
                argv, 0, f"handoff-renew token={token} expires_in={ttl}\n", "")
        if "handoff-takeover" in tokens:
            index = tokens.index("handoff-takeover")
            previous = tokens[index + 3] if len(tokens) > index + 3 else ""
            if self.refuse_takeover:
                # A refusal before any signal: known state, explicit marker.
                return CompletedProcess(
                    argv, 0, "takeover released=0 reason=lock_foreign\n", "")
            if self.takeover_lost_answer:
                # The real tablet: the script died with a message on stderr, but
                # ``adb shell su -c`` still reports exit status 0. The publisher
                # must not discard that message nor treat it as "nothing
                # happened".
                self.takeovers.append(previous)
                return CompletedProcess(
                    argv, 0, "",
                    "[favorites] takeover timeout; previous supervisor did not stop\n")
            self.takeovers.append(previous)
            return CompletedProcess(
                argv, 0, "takeover released=1 acked=1 owner=4242 previous=%s\n" % previous, "")
        if "verify-active" in tokens:
            index = tokens.index("verify-active")
            expected_gen, expected_mid = tokens[index + 2], tokens[index + 3]
            package = tokens[index + 1]
            manifest = self._read_device(package + "/favorites_manifest.json")
            verified = 0
            generation, manifest_id = "none", "none"
            if manifest:
                data = json.loads(manifest)
                generation, manifest_id = str(data["generation"]), data["manifest_id"]
                if (self.active and self.confirm and generation == expected_gen
                        and manifest_id == expected_mid):
                    verified = 1
            output = (f"verify verified={verified} generation={generation} "
                      f"manifest_id={manifest_id} lock_owned=1 lease=ours\n")
            return CompletedProcess(argv, 0, output, "")
        if "echo started" in command:
            # The publisher proves the detached launcher really began.
            return CompletedProcess(argv, 0, "started\n", "")
        if "start-detached" in tokens:
            # The device detaches its own ``start``; a pid line is its proof.
            self.start_calls += 1
            self.active = True
            return CompletedProcess(
                argv, 0, "start-detached pid=4242 log=/data/local/tmp/x/activation/start.log\n", "")
        if "start" in tokens:
            self.start_calls += 1
            self.active = True
            return CompletedProcess(argv, 0, "", "")
        if "rollback" in tokens:
            self.rollback_calls += 1
            self.active = False
            return CompletedProcess(argv, 0, "", "")
        return CompletedProcess(argv, 0, "", "")

    def _vps_failure(self, argv):
        restored = "no" if self.vps_restore_failed else "none"
        return CompletedProcess(
            argv, 1, f"switch failed previous_restored={restored} reason=probe\n", "")

    def _vps(self, argv):
        tokens = list(argv)
        # Locate the action after the script path, so both `sh SCRIPT start ...`
        # and `sudo -n -E sh SCRIPT start ...` parse identically.
        script_index = next((i for i, token in enumerate(tokens)
                             if token.endswith("switch_mixed_generation_vps.sh")), None)
        base = (script_index + 1) if script_index is not None else 2
        action = tokens[base] if len(tokens) > base else ""
        if action == "start":
            if not self.vps_start_ok:
                return self._vps_failure(argv)
            self.vps_starts.append(tokens[base + 2] if len(tokens) > base + 2 else "")
            self.sessions_started.append(list(tokens[base:]))
            return CompletedProcess(
                argv, 0,
                f"switch ok generation=2 session={tokens[base + 2] if len(tokens) > base + 2 else ''}\n", "")
        if action == "stop":
            self.vps_stops.append(tokens[base + 1] if len(tokens) > base + 1 else "")
            return CompletedProcess(argv, 0, "switch stop restored=none\n", "")
        return CompletedProcess(argv, 0, "", "")

    def _remote(self, argv):
        """Emulate the ssh/scp surface: tablet commands, package promotion, switch."""
        import shlex
        if argv[0] == "scp":
            source = argv[-2] if len(argv) >= 2 else ""
            self.remote_files[argv[-1]] = str(source).encode()
            return CompletedProcess(argv, 0, "", "")
        command = argv[-1] if argv else ""
        parts = shlex.split(command)
        if parts[:1] == ["adb"]:
            inner = parts[6] if len(parts) > 6 else ""
            return self._shell(inner, argv)
        if "pkg-ready" in command:
            if self.remote_generation_exists:
                return CompletedProcess(argv, 3, "generation-directory-exists\n", "")
            self.remote_generation_exists = True
            return CompletedProcess(argv, 0, "pkg-ready\n", "")
        if "switch_mixed_generation_vps.sh" in command:
            if " stop " in command or "'stop'" in command:
                return CompletedProcess(
                    argv, 0,
                    "switch stop session=remote restored=none\n", "")
            if not self.vps_start_ok:
                return self._vps_failure(argv)
            session = ""
            for token in parts:
                if token.startswith("mixed-gen"):
                    session = token
                    break
            self.vps_starts.append(session)
            self.sessions_started.append(parts)
            return CompletedProcess(
                argv, 0, f"switch ok generation=2 session={session}\n", "")
        if "sha256sum -c" in command:
            return CompletedProcess(argv, 0, "OK\n", "")
        return CompletedProcess(argv, 0, "", "")

    def _verify_sums(self, package: str | None) -> int:
        if not package:
            return 1
        sums = self._read_device(package + "/SHA256SUMS")
        if not sums:
            return 1
        for line in sums.decode().splitlines():
            digest, name = line.split("  ", 1)
            data = self._read_device(package + "/" + name)
            if data is None or hashlib.sha256(data).hexdigest() != digest:
                return 1
        return 0


class MixedGenerationServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="test-mgs-"))
        self.state_file = self.tmp_dir / "state.json"
        self.export_config_path = self.tmp_dir / "export.json"
        self.staging_base = self.tmp_dir / "staging"
        self.staging_base.mkdir()
        self.clock = [1000.0]

        self.export_data = {
            "schema_version": 1,
            "generation": 1,
            "identities": [
                {
                    "identity": "sbt",
                    "status": "approved",
                    "category": "Abertos",
                    "filename": "SBT.trp",
                    "chosen": {"channel_id": "206080"},
                }
            ],
        }
        self.export_config_path.write_text(json.dumps(self.export_data))

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def service(self, **kwargs):
        params = dict(
            server_url="http://127.0.0.1:8080",
            export_config_path=self.export_config_path,
            staging_base=self.staging_base,
            state_file=self.state_file,
            now=lambda: self.clock[0],
            poll_interval=0.0,
            publication_timeout=0.0,
        )
        params.update(kwargs)
        return mgs.MixedGenerationService(**params)

    # ------------------------------------------------------------- fingerprint
    def test_compute_inventory_hash_is_order_independent(self):
        service = self.service()
        task_a = {"id": "11111111", "status": "ready", "file_size": 100000, "title": "A", "display_name": "A.mp4"}
        task_b = {"id": "22222222", "status": "ready", "file_size": 200000, "title": "B", "display_name": "B.mp4"}
        task_not_ready = {"id": "33333333", "status": "downloading", "file_size": 300000}

        h1 = service.compute_inventory_hash([task_a, task_b, task_not_ready])
        h2 = service.compute_inventory_hash([task_b, task_a])
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)
        # A running download must not change the fingerprint.
        self.assertEqual(h1, service.compute_inventory_hash([task_a, task_b, task_not_ready]))

    def test_compute_inventory_hash_detects_metadata_and_revision_changes(self):
        service = self.service()
        base_task = {
            "id": "11111111", "status": "ready", "file_size": 100000,
            "title": "Title 1", "display_name": "Ep1.mp4", "series_id": "100",
            "series_title": "Series One", "storage": "gdrive",
            "remote_path": "11111111.mp4",
            "remote_md5": "d41d8cd98f00b204e9800998ecf8427e",
        }
        h_base = service.compute_inventory_hash([base_task])

        for changed, label in [
            (dict(base_task, title="Title Renamed"), "title"),
            (dict(base_task, display_name="Ep1_HD.mp4"), "display_name"),
            (dict(base_task, series_id="200"), "series_id"),
            (dict(base_task, series_title="Series Two"), "series_title"),
            (dict(base_task, season_num=3), "season_num"),
            (dict(base_task, episode_num=7), "episode_num"),
            (dict(base_task, storage="local"), "storage"),
            (dict(base_task, remote_path="11111111.mp4", remote_md5="098f6bcd4621d373cade4e832627b4f6"), "revision"),
            (dict(base_task, content_revision="md5:" + "c" * 32), "explicit revision"),
        ]:
            with self.subTest(changed=label):
                self.assertNotEqual(h_base, service.compute_inventory_hash([changed]))

        altered_export = {
            "schema_version": 1, "generation": 1,
            "identities": [
                {"identity": "record", "status": "approved", "category": "Abertos",
                 "filename": "Record.trp", "chosen": {"channel_id": "214640"}}
            ]
        }
        self.assertNotEqual(h_base, service.compute_inventory_hash([base_task], altered_export))

    # --------------------------------------------------------------- telemetry
    def test_is_playback_active_is_strict_and_fail_closed(self):
        service = self.service()

        for empty in (None, {}, []):
            with self.subTest(empty=empty):
                active, reason = service.is_playback_active(empty)
                self.assertTrue(active)
                self.assertEqual(reason, "server_unreachable_fail_closed")

        # The reviewer's exact case: a partial payload must never look idle.
        active, reason = service.is_playback_active({"active_vod": None})
        self.assertTrue(active)
        self.assertTrue(reason.startswith("missing_telemetry:"))

        for field in mgs.ESSENTIAL_STATUS_TYPES:
            payload = idle_status()
            payload.pop(field)
            with self.subTest(missing=field):
                active, reason = service.is_playback_active(payload)
                self.assertTrue(active)
                self.assertEqual(reason, f"missing_telemetry:{field}")

        for field, bad in [("listeners", "0"), ("tv_on", "no"), ("in_standby", None),
                           ("server_time", "now"), ("tv_usb_state", 7),
                           ("active_vod", 5), ("stream_bitrate_kbps", "fast"),
                           ("tablet_online", "yes")]:
            with self.subTest(invalid=field):
                active, reason = service.is_playback_active(idle_status(**{field: bad}))
                self.assertTrue(active)
                self.assertEqual(reason, f"invalid_telemetry:{field}")

        # Stale or skewed telemetry is not evidence of an idle TV.
        active, reason = service.is_playback_active(idle_status(server_time=900.0))
        self.assertTrue(active)
        self.assertEqual(reason, "stale_status_fail_closed")
        active, reason = service.is_playback_active(idle_status(server_time=1100.0))
        self.assertTrue(active)
        self.assertEqual(reason, "skewed_status_fail_closed")

        # Non-finite, negative or unparsable numbers must never look idle.
        for bad in (float("nan"), float("inf"), -1.0):
            with self.subTest(server_time=bad):
                active, _ = service.is_playback_active(idle_status(server_time=bad))
                self.assertTrue(active)
        for field, value in (("listeners", -1), ("stream_bitrate_kbps", -5.0)):
            with self.subTest(field=field):
                active, reason = service.is_playback_active(idle_status(**{field: value}))
                self.assertTrue(active)
                self.assertEqual(reason, "invalid_numeric_telemetry_fail_closed")

        # An empty-but-present VOD identity is telemetry, not idleness.
        active, reason = service.is_playback_active(idle_status(active_vod=""))
        self.assertTrue(active)
        self.assertIn("active_vod", reason)

        # Any measured bitrate blocks, even with zero counted listeners.
        active, reason = service.is_playback_active(
            idle_status(listeners=0, stream_bitrate_kbps=12.0))
        self.assertTrue(active)
        self.assertIn("active_live_stream", reason)

        active, reason = service.is_playback_active(idle_status(tablet_online=False))
        self.assertTrue(active)
        self.assertEqual(reason, "tablet_telemetry_offline_fail_closed")

        active, reason = service.is_playback_active(idle_status(active_vod="1234abcd"))
        self.assertTrue(active)
        self.assertIn("active_vod", reason)

        active, reason = service.is_playback_active(idle_status(listeners=1, stream_bitrate_kbps=3800.0))
        self.assertTrue(active)
        self.assertIn("active_live_stream", reason)

        # A powered TV with the pendrive mounted is the NORMAL state for the
        # manual button: the user stops the video, taps "NA TV" and browses the
        # folders. It is idle (with an explicit reconnect warning on the UI) as
        # long as no measurable reproduction signal is present.
        active, reason = service.is_playback_active(idle_status(tv_on=True))
        self.assertFalse(active)
        self.assertEqual(reason, "idle_tv_on_no_playback")
        self.assertEqual(service.playback_state(idle_status(tv_on=True))[0], "idle")

        active, reason = service.is_playback_active(idle_status(tv_usb_state="CONFIGURED"))
        self.assertFalse(active)
        self.assertEqual(reason, "idle_tv_on_no_playback")
        self.assertEqual(
            service.playback_state(idle_status(tv_usb_state="CONFIGURED"))[0], "idle")

        # Unknown USB states are still fail-closed and never authorise a switch,
        # even when the TV is powered on.
        active, reason = service.is_playback_active(idle_status(tv_usb_state="WEIRD"))
        self.assertTrue(active)
        self.assertIn("unknown_usb_state", reason)
        self.assertEqual(
            service.playback_state(idle_status(tv_usb_state="WEIRD"))[0], "unknown")
        active, reason = service.is_playback_active(
            idle_status(tv_on=True, tv_usb_state="CONNECTED"))
        self.assertTrue(active)
        self.assertIn("unknown_usb_state", reason)
        self.assertEqual(service.playback_state(
            idle_status(tv_on=True, tv_usb_state="CONNECTED"))[0], "unknown")

        # Measurable reproduction still blocks even with the TV powered on.
        active, reason = service.is_playback_active(
            idle_status(tv_on=True, active_vod="1234abcd"))
        self.assertTrue(active)
        self.assertIn("active_vod", reason)
        self.assertEqual(service.playback_state(
            idle_status(tv_on=True, listeners=1, stream_bitrate_kbps=3800.0))[0], "playing")

        # Only a complete, fresh, idle payload is idle.
        active, reason = service.is_playback_active(idle_status())
        self.assertFalse(active)
        self.assertEqual(reason, "idle_tv_off")
        self.assertEqual(service.playback_state(idle_status())[0], "idle")

    # ------------------------------------------------------------ preparation
    def test_reconstruction_with_existing_staging_directory(self):
        service = self.service()
        task = {
            "id": "11111111", "status": "ready", "file_size": 100000,
            "title": "Episode 1", "display_name": "Ep1.mp4",
        }
        h = service.compute_inventory_hash([task])
        pre_existing = self.staging_base / f"mixed-gen2-{h[:8]}"
        pre_existing.mkdir(parents=True)
        (pre_existing / "stale_file.bin").write_text("dummy")

        with mock.patch("build_mixed_media_package.build_package") as mock_build:
            mock_build.return_value = {
                "manifest_id": "abc12345", "total_files": 2, "free_bytes": 1000000,
                "hashes": {name: "0" * 64 for name in mgs.REQUIRED_RUNTIME},
            }
            res = service.check_and_prepare_generation([task])
            self.assertIsNotNone(res)
            self.assertEqual(service.state["pending_generation"], 2)
            self.assertEqual(service.state["pending_session"], f"mixed-gen2-{h[:8]}")
            # Clean reconstruction: no stale bytes survive.
            self.assertFalse((pre_existing / "stale_file.bin").exists())

    def test_prepare_leaves_an_unchanged_pending_generation_alone(self):
        """The timer must not disturb a pending generation it did not change."""
        service = self.service()
        task = [{"id": "11111111", "status": "ready", "file_size": 100000, "title": "Ep 1"}]
        with mock.patch("build_mixed_media_package.build_package") as mock_build:
            mock_build.return_value = {
                "manifest_id": "abc12345", "total_files": 2, "free_bytes": 1000000,
                "hashes": {name: "0" * 64 for name in mgs.REQUIRED_RUNTIME},
            }
            service.check_and_prepare_generation(task)
            pending = (service.state["pending_generation"], service.state["pending_session"],
                       service.state["pending_inventory_hash"])
            self.assertEqual(mock_build.call_count, 1)
            # A second run over the same inventory is a no-op: the pending
            # generation is kept, not rebuilt and not cleared.
            service.check_and_prepare_generation(list(task))
            self.assertEqual((service.state["pending_generation"],
                              service.state["pending_session"],
                              service.state["pending_inventory_hash"]), pending)
            self.assertEqual(mock_build.call_count, 1)
            # A new ready VOD does change it, and then it is rebuilt.
            service.check_and_prepare_generation(
                task + [{"id": "22222222", "status": "ready", "file_size": 200000, "title": "Ep 2"}])
            self.assertEqual(mock_build.call_count, 2)
            self.assertNotEqual(service.state["pending_inventory_hash"], pending[2])

    def test_export_migration_rebuilds_the_pending_generation(self):
        """Switching the Live export must rebuild, never publish the stale one.

        The Live configuration is part of the inventory fingerprint, so a pending
        generation prepared for the old export cannot be published once the export
        changes: the next preparation produces a different session.
        """
        service = self.service()
        task = [{"id": "11111111", "status": "ready", "file_size": 100000, "title": "Ep 1"}]
        with mock.patch("build_mixed_media_package.build_package") as mock_build:
            mock_build.return_value = {
                "manifest_id": "abc12345", "total_files": 2, "free_bytes": 1000000,
                "hashes": {name: "0" * 64 for name in mgs.REQUIRED_RUNTIME},
            }
            service.state["active_generation"] = 1
            service._save_state()
            service.check_and_prepare_generation(task)
            old_hash = service.state["pending_inventory_hash"]
            old_session = service.state["pending_session"]

            # The migration writes the TV export (nested under TV/).
            self.export_config_path.write_text(json.dumps({
                "schema_version": 1, "generation": 2, "live_root": "TV",
                "identities": [{
                    "identity": "sbt", "status": "approved", "category": "Abertos",
                    "filename": "SBT 1080 RJ",
                    "chosen": {"channel_id": "26823618"},
                }],
            }))
            self.assertIsNotNone(service.check_and_prepare_generation(list(task)))
            self.assertNotEqual(service.state["pending_inventory_hash"], old_hash)
            self.assertNotEqual(service.state["pending_session"], old_session)
            self.assertEqual(service.state["pending_generation"], 2)
            self.assertIn(service.state["pending_inventory_hash"][:8],
                          service.state["pending_session"])
            # The package handed to the builder must be the migrated layout.
            self.assertEqual(mock_build.call_count, 2)
            self.assertEqual(mock_build.call_args.kwargs["export_config"]["live_root"], "TV")
            built_dir = mock_build.call_args.kwargs["staging_dir"]
            self.assertEqual(built_dir.name, service.state["pending_session"])

    def test_a_new_ready_vod_enters_the_next_generation(self):
        """A VOD that finishes downloading after the last publication is included."""
        service = self.service()
        first = [{"id": "11111111", "status": "ready", "file_size": 100000, "title": "Ep 1"}]
        with mock.patch("build_mixed_media_package.build_package") as mock_build:
            mock_build.return_value = {
                "manifest_id": "abc12345", "total_files": 2, "free_bytes": 1000000,
                "hashes": {name: "0" * 64 for name in mgs.REQUIRED_RUNTIME},
            }
            service.check_and_prepare_generation(first)
            service.promote_pending_generation({"verified": True, "generation": 2,
                                                "manifest_id": "abc12345"})
            self.assertEqual(service.state["active_generation"], 2)
            # The download finishes; a running one must not matter.
            downloaded = {"id": "22222222", "status": "ready", "file_size": 200000,
                          "title": "Ep 2"}
            running = {"id": "33333333", "status": "downloading", "file_size": 300000}
            self.assertIsNotNone(
                service.check_and_prepare_generation(first + [downloaded, running]))
            self.assertEqual(service.state["pending_generation"], 3)
            self.assertNotEqual(service.state["pending_inventory_hash"],
                                service.state["active_inventory_hash"])

    def test_build_failure_recovers_state_and_cleans_staging(self):
        service = self.service()
        service.state["active_generation"] = 1
        service._save_state()

        task = [{"id": "11111111", "status": "ready", "file_size": 100000, "title": "Ep 1"}]
        with mock.patch("build_mixed_media_package.build_package", side_effect=RuntimeError("Disk full")):
            res = service.check_and_prepare_generation(task)
            self.assertIsNone(res)
            self.assertIsNone(service.state.get("pending_generation"))
            self.assertEqual(service.state["active_generation"], 1)
        self.assertEqual(list(self.staging_base.iterdir()), [])

    def test_incomplete_package_is_journalled_as_incomplete(self):
        service = self.service()
        task = [{"id": "11111111", "status": "ready", "file_size": 100000, "title": "Ep 1"}]
        with mock.patch("build_mixed_media_package.build_package") as mock_build:
            mock_build.return_value = {
                "manifest_id": "abc12345", "total_files": 2, "free_bytes": 1000000,
                "hashes": {},
            }
            service.check_and_prepare_generation(task)
        journal = service.state["publication"]
        self.assertEqual(journal["phase"], "incomplete")
        self.assertIn("fuse_ntfs", journal["missing"])

    # ------------------------------------------------------- integrity checks
    def test_staging_integrity_gate(self):
        service = self.service()
        staging, _ = make_complete_package(self.tmp_dir)
        self.assertEqual(service.staging_is_complete(staging), (True, "ok"))

        # Missing / empty / malformed sums must always fail.
        (staging / "SHA256SUMS").unlink()
        self.assertEqual(service.staging_is_complete(staging)[1], "sha256sums_missing")
        (staging / "SHA256SUMS").write_text("")
        self.assertEqual(service.staging_is_complete(staging)[1], "sha256sums_empty")
        (staging / "SHA256SUMS").write_text("not a sums file\n")
        self.assertTrue(service.staging_is_complete(staging)[1].startswith("sha256sums_invalid"))

        write_sums(staging)
        # Tampered artifact bytes, and a listed file that vanished.
        (staging / "favorites_layout.json").write_text("tampered")
        self.assertTrue(service.staging_is_complete(staging)[1].startswith("hash_mismatch"))
        write_sums(staging)
        (staging / "fuse_ntfs").unlink()
        self.assertTrue(service.staging_is_complete(staging)[1].startswith("file_missing"))
        (staging / "fuse_ntfs").write_bytes(f"runtime:fuse_ntfs\n".encode())
        write_sums(staging)
        (staging / "transport.token").unlink()
        self.assertTrue(service.staging_is_complete(staging)[1].startswith("file_missing"))

        # A staging directory that never existed.
        self.assertEqual(service.staging_is_complete(self.tmp_dir / "nope")[1],
                         "staging_dir_missing")

    def test_manifest_identity_must_match_canonical_bytes(self):
        service = self.service()
        staging, manifest_id = make_complete_package(self.tmp_dir)
        self.assertEqual(service.manifest_identity(staging), (2, manifest_id))
        manifest = json.loads((staging / "favorites_manifest.json").read_text())
        manifest["manifest_id"] = "0" * 64
        (staging / "favorites_manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(mgs.PublicationError):
            service.manifest_identity(staging)

    # -------------------------------------------------------------- promotion
    def test_promotion_requires_matching_verified_confirmation(self):
        service = self.service()
        service.state.update({
            "pending_generation": 3, "pending_inventory_hash": "hash3",
            "pending_manifest_id": "man3", "pending_staging_dir": "/tmp/gen3",
            "pending_session": "mixed-gen3-12345678",
        })
        with self.assertRaises(mgs.PublicationError):
            service.promote_pending_generation()
        with self.assertRaises(mgs.PublicationError):
            service.promote_pending_generation({"verified": False, "generation": 3, "manifest_id": "man3"})
        with self.assertRaises(mgs.PublicationError):
            service.promote_pending_generation({"verified": True, "generation": 2, "manifest_id": "man3"})
        with self.assertRaises(mgs.PublicationError):
            service.promote_pending_generation({"verified": True, "generation": 3, "manifest_id": "other"})
        self.assertEqual(service.state["active_generation"], 1)

        self.assertTrue(service.promote_pending_generation(
            {"verified": True, "generation": 3, "manifest_id": "man3"}))
        self.assertEqual(service.state["active_generation"], 3)
        self.assertEqual(service.state["active_inventory_hash"], "hash3")
        self.assertEqual(service.state["active_session"], "mixed-gen3-12345678")
        self.assertIsNone(service.state["pending_generation"])
        self.assertEqual(service.state["publication"]["phase"], "active")

        # Reload from disk to verify the atomic save really persisted it.
        reloaded = json.loads(self.state_file.read_text())
        self.assertEqual(reloaded["active_generation"], 3)
        self.assertEqual(reloaded["active_manifest_id"], "man3")

    # ------------------------------------------------------------ publication
    def _journal(self, staging, manifest_id, session=SESSION, phase="prepared", generation=2):
        state = {
            "pending_generation": generation,
            "pending_staging_dir": str(staging),
            "pending_inventory_hash": "inv-hash",
            "pending_manifest_id": manifest_id,
            "pending_session": session,
            "publication": {
                "generation": generation,
                "manifest_id": manifest_id,
                "inventory_hash": "inv-hash",
                "staging_dir": str(staging),
                "session": session,
                "phase": phase,
                "attempts": 0,
                "last_error": None,
            },
        }
        return state

    def _arm(self, service, staging, manifest_id, *, session=None, phase="prepared",
             generation=2, tasks=(TASK_READY,)):
        """Pre-arm a *consistent* pending generation (hash matches the tasks).

        Publication only proceeds when the pending inventory hash equals the
        current one, so a hand-written journal must carry the real fingerprint;
        otherwise the service would (correctly) rebuild a new generation.
        """
        state = {
            "pending_generation": generation,
            "pending_staging_dir": str(staging),
            "pending_inventory_hash": service.compute_inventory_hash(list(tasks)),
            "pending_manifest_id": manifest_id,
            "pending_session": session or SESSION,
            "pending_index": service.generation_index(staging),
            "publication": {
                "generation": generation,
                "manifest_id": manifest_id,
                "inventory_hash": service.compute_inventory_hash(list(tasks)),
                "staging_dir": str(staging),
                "session": session or SESSION,
                "phase": phase,
                "attempts": 0,
                "last_error": None,
            },
        }
        service.state.update(state)
        service._save_state()
        return state

    def test_manual_authorisation_is_required_to_publish(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        result = service.publish_pending()          # no manual authorisation
        self.assertEqual(result["state"], "manual_required")
        self.assertEqual(device.commands, [])
        self.assertEqual(service.state["active_generation"], 1)

    def test_the_daemon_cycle_never_switches_the_lun(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()
        service.fetch_vod_status = lambda: {"tasks": [TASK_READY]}
        service.check_and_prepare_generation = lambda tasks, export=None: None

        result = service.step(publish=True)
        self.assertEqual(result["publication"]["state"], "manual_required")
        # A daemon cycle prepares and observes; it never reconnects the pendrive.
        self.assertEqual(device.commands, [])
        self.assertEqual(device.start_calls, 0)

    def test_journal_states_that_no_mixed_reader_proof_exists(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        self._arm(service, staging, manifest_id)
        # Normal manual-button state: TV on with the pendrive mounted.
        service.fetch_server_status = lambda: idle_status(tv_usb_state="CONFIGURED",
                                                         tv_on=True)

        self.assertEqual(service.apply_task("a1b2c3d4", tasks=[TASK_READY])["state"],
                         "active")
        journal = service.state["publication"]
        self.assertEqual(journal["idle_reason"], "idle_tv_on_no_playback")
        self.assertEqual(journal["mixed_reader_proof"], "not_available")
        self.assertTrue(journal["tv_on"])

    def test_concurrent_apply_publishes_at_most_once(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        results = []

        def worker():
            try:
                results.append(service.apply_task("a1b2c3d4", tasks=[TASK_READY]))
            except mgs.StateLockedError:
                results.append({"state": "state_locked"})

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(results), 2)
        self.assertLessEqual(device.start_calls, 1)
        active = [r for r in results if r.get("state") == "active"]
        self.assertLessEqual(len(active), 1)
        others = [r for r in results if r.get("state") != "active"]
        self.assertTrue(others)
        self.assertIn(others[0]["state"],
                      ("state_locked", "already_current", "in_progress", "blocked"))

    def test_apply_manual_cycle_activates_and_promotes(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        guard = FakeGuard()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = guard
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "active")
        self.assertEqual(service.state["active_generation"], 2)
        self.assertIn("a1b2c3d4", service.state["active_index"])
        self.assertEqual(device.start_calls, 1)
        self.assertTrue(device.active)
        self.assertTrue(device.handoff_leases)
        self.assertFalse(guard.held)                   # guard released at the end
        flat = [" ".join(c) for c in device.commands]
        self.assertTrue(any("push --sync" in c for c in flat))
        self.assertTrue(any("'handoff-lease'" in c for c in flat))
        self.assertTrue(any("'start-detached'" in c for c in flat))
        # The local generation switch must run privileged: the running
        # generation's services belong to root, so an unprivileged switch cannot
        # supersede them and the shared ports stay bound. `-E` keeps
        # SWITCH_ROOT/ENV_FILE/EXPORT_CONFIG_FILE across sudo's env reset.
        switch_calls = [c for c in flat if "switch_mixed_generation_vps.sh" in c and " start " in c]
        self.assertTrue(switch_calls, "no local switch invocation")
        self.assertTrue(all(c.startswith("sudo -n -E sh ") for c in switch_calls), switch_calls)
        # The activation must carry the handoff token as its last argument.
        start_cmd = [c for c in flat if "'start-detached'" in c][0]
        self.assertIn(device.handoff_leases[-1], start_cmd)
        self.assertTrue(any("'rollback'" not in c for c in flat))
        self.assertEqual(device.rollback_calls, 0)
        durations = service.state["publication"]["stage_durations_s"]
        self.assertEqual(set(durations), {"tablet_push", "tablet_hash_verification",
                         "tablet_transfer", "vps_switch", "tunnels", "preflight",
                         "activation_request", "confirmation"})
        self.assertTrue(all(isinstance(value, float) and value >= 0 for value in durations.values()))

    def test_duration_journaling_cannot_hide_publication_errors(self):
        service = self.service()
        with mock.patch.object(service, "_update_journal", side_effect=OSError("disk")):
            with self.assertRaisesRegex(mgs.PublicationError, "original"):
                with service._publication_duration("test"):
                    raise mgs.PublicationError("original")

    def test_apply_already_current_does_not_reconnect_the_usb(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice(already_active=True)
        for item in staging.iterdir():
            device.files[f"/data/local/tmp/live-favorites/{SESSION}/{item.name}"] = item.read_bytes()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.state.update({
            "active_generation": 2, "active_manifest_id": manifest_id,
            "active_session": SESSION, "active_index": service.generation_index(staging),
            "active_inventory_hash": service.compute_inventory_hash([TASK_READY]),
        })
        service._save_state()
        service.fetch_server_status = lambda: idle_status()

        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "already_current")
        self.assertTrue(result["confirmed"])
        self.assertFalse(result["interrupts_playback"])
        self.assertIn("Lista atualizada", result["message"])
        # Only the confirmation probe ran; nothing was transferred or switched.
        self.assertEqual(device.start_calls, 0)
        flat = [" ".join(c) for c in device.commands]
        self.assertFalse(any("push --sync" in c for c in flat))
        self.assertFalse(any(" start " in c for c in flat))

    def test_apply_existing_task_does_not_treat_deleted_episode_as_current(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice(already_active=True)
        service = self.service(transport=mgs.TabletTransport(runner=device))
        deleted = {"id": "b2c3d4e5", "status": "ready", "file_size": 1024,
                   "title": "Deleted", "remote_md5": "b" * 32}
        service.state.update({
            "active_generation": 2, "active_manifest_id": manifest_id,
            "active_session": SESSION, "active_index": service.generation_index(staging),
            "active_inventory_hash": service.compute_inventory_hash([TASK_READY, deleted]),
        })
        service._save_state()
        service.fetch_server_status = lambda: idle_status(active_vod="d0df9dbf")
        # Existing video is still in the old generation but the removed item
        # changes the fingerprint, so an active TV must block publication.
        with mock.patch.object(service, "confirm_task", side_effect=AssertionError("stale inventory")):
            result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(device.start_calls, 0)

    def test_missing_inventory_fingerprint_never_claims_already_current(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        service = self.service(transport=mgs.TabletTransport(runner=FakeDevice(already_active=True)))
        service.state.update({"active_generation": 2, "active_manifest_id": manifest_id,
                              "active_session": SESSION,
                              "active_index": service.generation_index(staging),
                              "active_inventory_hash": None})
        service._save_state()
        service.fetch_server_status = lambda: idle_status(active_vod="d0df9dbf")
        self.assertEqual(service.apply_task("a1b2c3d4", tasks=[TASK_READY])["state"], "blocked")

    def test_apply_is_blocked_while_playback_is_active(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        guard = FakeGuard()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = guard
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status(active_vod="d0df9dbf")

        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "blocked")
        self.assertTrue(result["interrupts_playback"])
        self.assertIn("interromper", result["message"])
        self.assertEqual(guard.calls, [])                     # guard never touched
        self.assertEqual(device.commands, [])
        self.assertEqual(service.state["active_generation"], 1)

    def test_repeated_click_is_idempotent(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        journal = self._journal(staging, manifest_id)
        journal["publication"].update({"phase": "publishing", "requested_task": "a1b2c3d4"})
        service.state.update(journal)
        service._save_state()
        service.fetch_server_status = lambda: idle_status()

        first = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(first["state"], "in_progress")
        self.assertEqual(device.commands, [])
        other = service.apply_task("ffffffff", tasks=[TASK_READY])
        self.assertEqual(other["state"], "blocked")
        self.assertEqual(other["reason"], "another_task_in_progress")

    def test_playback_starting_during_preflight_blocks_activation(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        guard = FakeGuard()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = guard
        self._arm(service, staging, manifest_id)
        calls = {"n": 0}

        def status():
            # Idle until the end-to-end preflight has actually run, then someone
            # starts watching: the abort must happen after the services were
            # switched, and must put the previous generation back.
            calls["n"] += 1
            if device.preflight_calls == 0:
                return idle_status()
            return idle_status(active_vod="1234abcd")

        service.fetch_server_status = status
        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(device.start_calls, 0)
        self.assertFalse(guard.held)                    # guard must be released
        self.assertEqual(service.state["active_generation"], 1)
        self.assertGreaterEqual(device.preflight_calls, 1)
        self.assertIn("preflight", service.state["publication"]["stage_durations_s"])
        self.assertNotIn("activation_request", service.state["publication"]["stage_durations_s"])
        # The services were already switched to the new generation before the
        # preflight, so aborting here must put the previous generation back.
        self.assertEqual(device.vps_starts, [SESSION])
        self.assertEqual(device.vps_stops, [SESSION])
        self.assertFalse(service.state["publication"]["previous_services_down"])

    def test_guard_busy_blocks_without_touching_the_tablet(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        guard = FakeGuard(busy=True)
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = guard
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "failed")
        self.assertIn("guard", result["reason"])
        self.assertEqual(device.start_calls, 0)
        self.assertEqual([c for c in device.commands if "push" in " ".join(c)], [])
        self.assertEqual(service.state["active_generation"], 1)

    def test_guard_loss_before_activation_aborts_and_restores_services(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        guard = FakeGuard(lose_after=1)      # the renew after the transfer fails
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = guard
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "failed")
        self.assertIn("guard", result["reason"])
        self.assertEqual(device.start_calls, 0)
        self.assertFalse(guard.held)
        self.assertIn("previous_session", service._journal())
        self.assertEqual(service.state["active_generation"], 1)

    def test_vps_switch_failure_aborts_before_the_tablet_switch(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice(vps_start_ok=False)
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "failed")
        self.assertIn("VPS", result["reason"])
        self.assertEqual(device.start_calls, 0)
        self.assertEqual(device.vps_stops, [])          # nothing was switched

    def test_tablet_lease_refusal_rolls_back_the_whole_set(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice(refuse_lease=True)
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(device.start_calls, 0)
        # The services were reconfigured first, so the whole set is restored.
        self.assertEqual(device.vps_stops, [SESSION])

    def test_unconfirmed_activation_is_not_reverted_or_promoted(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice(confirm=False)
        service = self.service(transport=mgs.TabletTransport(runner=device))
        guard = FakeGuard()
        service.guard_request = guard
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "unconfirmed")
        self.assertEqual(service.state["active_generation"], 1)
        self.assertIsNotNone(service.state["pending_generation"])
        self.assertEqual(service.state["publication"]["phase"], "unconfirmed")
        self.assertEqual(device.rollback_calls, 0)      # someone may be watching
        self.assertFalse(guard.held)
        # A second apply must not silently revert or re-switch: it reports that
        # a human has to reconcile the TV state.
        second = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertIn(second["state"], ("unconfirmed", "blocked"))
        self.assertEqual(device.rollback_calls, 0)

    def test_failed_preflight_aborts_before_activation(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice(preflight_ok=False)
        service = self.service(transport=mgs.TabletTransport(runner=device))
        guard = FakeGuard()
        service.guard_request = guard
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(device.start_calls, 0)
        # The generation services are started BEFORE the tunnels/preflight, so a
        # failed preflight must put the previous generation's set back.
        self.assertEqual(device.vps_starts, [SESSION])
        self.assertEqual(device.vps_stops, [SESSION])
        self.assertFalse(guard.held)
        self.assertEqual(service.state["active_generation"], 1)
        self.assertEqual(service.state["publication"]["phase"], "failed")
        self.assertGreaterEqual(device.preflight_calls, 1)

    def test_publish_refuses_a_package_built_for_another_export(self):
        """D9: migrating the Live export must never transfer the stale package.

        The journalled generation may still be internally consistent (its hash
        matches the inventory), but if its manifest belongs to the PREVIOUS export
        the tablet would receive a package the manager of the new export refuses,
        taking Live down. It has to be blocked before the tablet is touched.
        """
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()
        service.fetch_vod_status = lambda: {"tasks": [TASK_READY]}

        # The export migrates: same channel, now nested under TV/ with the .trp
        # naming the reviewed grid uses. The journalled manifest keeps the old
        # layout, so it can no longer describe the mounted set.
        self.export_config_path.write_text(json.dumps({
            "schema_version": 1, "generation": 2, "live_root": "TV",
            "identities": [{"identity": "sbt", "status": "approved",
                            "category": "Abertos", "filename": "SBT 1080 RJ",
                            "chosen": {"channel_id": "206080"}}]}))

        result = service.publish_pending(manual=True)
        self.assertEqual(result["state"], "prepared")
        self.assertEqual(result["reason"], "stale_for_export")
        self.assertFalse(result["reconnected"])
        self.assertEqual(device.commands, [])
        self.assertEqual(device.preflight_calls, 0)
        self.assertEqual(service.state["active_generation"], 1)
        self.assertEqual(service.state["publication"]["phase"], "prepared")
        self.assertEqual(service.state["publication"]["last_error"], "stale_for_export")

    def test_publish_refuses_a_package_when_the_inventory_changed(self):
        """A new ready VOD after preparation makes the package outdated too."""
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()
        new_task = {"id": "b2c3d4e5", "status": "ready", "file_size": 8 * 1024 * 1024,
                    "title": "Outro", "display_name": "Outro.mp4"}
        service.fetch_vod_status = lambda: {"tasks": [TASK_READY, new_task]}

        result = service.publish_pending(manual=True)
        self.assertEqual(result["state"], "prepared")
        self.assertEqual(result["reason"], "stale_for_inventory")
        self.assertEqual(device.commands, [])

    def test_publish_blocks_when_the_catalog_cannot_be_read(self):
        """Fail closed: an unreadable catalog never publishes silently."""
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()
        service.fetch_vod_status = lambda: None

        result = service.publish_pending(manual=True)
        self.assertEqual(result["state"], "prepared")
        self.assertEqual(result["reason"], "inventory_unavailable")
        self.assertEqual(device.commands, [])

    def test_publish_blocks_when_the_journal_has_no_fingerprint(self):
        """A journal without an inventory hash cannot prove the package is current."""
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        self._arm(service, staging, manifest_id)
        journal = service.state["publication"]
        journal.pop("inventory_hash", None)
        service._save_state()
        service.fetch_server_status = lambda: idle_status()
        service.fetch_vod_status = lambda: {"tasks": [TASK_READY]}

        result = service.publish_pending(manual=True)
        self.assertEqual(result["state"], "prepared")
        self.assertEqual(result["reason"], "stale_for_inventory")
        self.assertEqual(device.commands, [])

    def test_publish_reads_the_journal_from_disk_not_from_memory(self):
        """A long-lived process must not decide from a stale in-memory journal.

        ``publish_pending`` is usually called by a fresh process, but the service
        object may live on. The guard therefore reads the journal the peers
        committed, which is written atomically (temp + fsync + os.replace), so a
        read under the state lock never sees a torn file.
        """
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()
        service.fetch_vod_status = lambda: {"tasks": [TASK_READY]}

        # A peer (another process) commits a different journal to disk.
        on_disk = json.loads(self.state_file.read_text())
        on_disk["publication"]["inventory_hash"] = "0" * 64
        self.state_file.write_text(json.dumps(on_disk))

        self.assertEqual(service.state["publication"]["inventory_hash"],
                         service.compute_inventory_hash([TASK_READY]))
        result = service.publish_pending(manual=True)
        self.assertEqual(result["state"], "prepared")
        self.assertIn(result["reason"], ("stale_for_inventory", "stale_for_export"))
        self.assertEqual(device.commands, [])

    def test_publish_still_proceeds_for_a_matching_export(self):
        """The guard must not block the normal path."""
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()
        service.fetch_vod_status = lambda: {"tasks": [TASK_READY]}

        reason = service.stale_publish_reason(service._journal(), [TASK_READY])
        self.assertIsNone(reason)

    def test_incomplete_staging_is_never_published(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        (staging / "fuse_ntfs").unlink()
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        result = service.publish_pending(manual=True)
        self.assertEqual(result["state"], "incomplete")
        self.assertEqual(device.commands, [])

    def test_resume_promotes_a_confirmed_session_without_switching(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice(already_active=True)
        for item in staging.iterdir():
            device.files[f"/data/local/tmp/live-favorites/{SESSION}/{item.name}"] = item.read_bytes()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        self._arm(service, staging, manifest_id, phase="activating")
        service.fetch_server_status = lambda: idle_status()

        result = service.resume_publication()
        self.assertEqual(result["state"], "active")
        self.assertTrue(result["recovered"])
        self.assertEqual(service.state["active_generation"], 2)
        self.assertEqual(device.start_calls, 0)
        self.assertEqual(device.vps_starts, [])

    def test_resume_never_switches_and_never_reverts_an_unconfirmed_session(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice(confirm=False)
        service = self.service(transport=mgs.TabletTransport(runner=device))
        self._arm(service, staging, manifest_id, phase="unconfirmed")
        service.fetch_server_status = lambda: idle_status(active_vod="1234abcd")

        result = service.resume_publication()
        self.assertEqual(result["state"], "unconfirmed")
        self.assertEqual(result["reason"], "manual_reconciliation_required")
        self.assertEqual(device.rollback_calls, 0)
        self.assertEqual(device.start_calls, 0)

    def test_resume_reports_uncertain_when_the_tablet_is_unreachable(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice(unreachable=True)
        service = self.service(transport=mgs.TabletTransport(runner=device))
        self._arm(service, staging, manifest_id, phase="activating")
        result = service.resume_publication()
        self.assertEqual(result["state"], "uncertain")
        self.assertEqual(result["reason"], "tablet_unreachable")

    def test_publication_without_transport_is_skipped(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        service = self.service()
        self._arm(service, staging, manifest_id)
        self.assertEqual(service.publish_pending(manual=True),
                         {"state": "skipped", "reason": "no_tablet_transport"})

    def test_two_consecutive_generations(self):
        """N -> N+1: the second application uses a new session and package."""
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()

        # Generation N (2) already applied.
        staging2, manifest2 = make_complete_package(self.tmp_dir, generation=2,
                                                    session=service.session_name(2, "a" * 32))
        self._arm(service, staging2, manifest2, session=service.session_name(2, "a" * 32))
        self.assertEqual(service.apply_task("a1b2c3d4", tasks=[TASK_READY])["state"], "active")
        first_session = service.state["active_session"]

        # A new video arrives: a new generation must be prepared and applied.
        new_tasks = [TASK_READY, {"id": "b2c3d4e5", "status": "ready",
                                  "file_size": 5 * 1024 * 1024, "title": "New",
                                  "remote_md5": "b" * 32}]
        staging3 = self.tmp_dir / service.session_name(3, "c" * 32)
        staging3.mkdir()
        for name in pkg_builder.ARTIFACTS:
            (staging3 / name).write_bytes(f"artifact:{name}\n".encode())
        write_runtime_files(staging3)
        manifest3 = write_manifest(staging3, 3,
                                   extra_vod=(("b2c3d4e5", 5 * 1024 * 1024, "b" * 32),))
        write_sums(staging3)

        def fake_prepare(_tasks, _export=None):
            self._arm(service, staging3, manifest3, generation=3,
                      session=service.session_name(3, "c" * 32), tasks=tuple(new_tasks))
            return {"manifest_id": manifest3}

        service.check_and_prepare_generation = lambda tasks, export=None: fake_prepare(tasks)
        result = service.apply_task("b2c3d4e5", tasks=new_tasks)
        self.assertEqual(result["state"], "active")
        self.assertEqual(service.state["active_generation"], 3)
        self.assertNotEqual(service.state["active_session"], first_session)
        self.assertEqual(service.state["active_session"], service.session_name(3, "c" * 32))
        self.assertEqual(device.start_calls, 2)
        # Each generation reconfigures the Live manager + VOD proxy, so the
        # second application switched the services to generation 3's session.
        self.assertEqual(len(device.vps_starts), 2)
        self.assertEqual(device.vps_starts[-1], service.session_name(3, "c" * 32))

    def test_apply_status_reports_states_without_secrets(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice(already_active=True)
        for item in staging.iterdir():
            device.files[f"/data/local/tmp/live-favorites/{SESSION}/{item.name}"] = item.read_bytes()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.state.update({
            "active_generation": 2, "active_manifest_id": manifest_id,
            "active_session": SESSION, "active_index": service.generation_index(staging),
        })
        service._save_state()

        status = service.apply_status("a1b2c3d4")
        self.assertTrue(status["available"])
        self.assertTrue(status["selected_in_active"])
        self.assertEqual(status["generation"], 2)
        self.assertNotIn("token", json.dumps(status).lower())
        idle_status_result = service.apply_status("ffffffff")
        self.assertFalse(idle_status_result["selected_in_active"])

    def test_step_prepares_but_never_applies_by_default(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_vod_status = lambda: {"tasks": [TASK_READY]}
        service.fetch_server_status = lambda: idle_status()

        def fake_prepare(_tasks, _export=None):
            with service.state_lock():
                self._arm(service, staging, manifest_id)
            return {"manifest_id": manifest_id}

        service._prepare_locked = fake_prepare
        result = service.step()
        self.assertEqual(result["prepared"], True)
        self.assertNotIn("publication", result)
        self.assertEqual(device.start_calls, 0)
        self.assertEqual(service.state["active_generation"], 1)
        self.assertIsNotNone(service.state["pending_generation"])

    def test_activation_transport_error_is_ambiguous_and_restores_services(self):
        """An RPC error after requesting the activation proves nothing."""
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice(confirm=False)
        guard = FakeGuard()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = guard
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        def exploding_activate(_session, *, handoff_token=None):
            raise mgs.ActivationUncertain("adb died after start was requested")

        service.activate_package = exploding_activate
        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "unconfirmed")
        self.assertEqual(result["reason"], "manual_reconciliation_required")
        journal = service._journal()
        self.assertTrue(journal["manual_reconciliation_required"])
        self.assertEqual(journal["phase"], "unconfirmed")
        # The tablet may ALREADY be serving the new generation, so nothing
        # destructive happens: the new services are left running and no blind
        # restore of the old set is attempted.
        self.assertEqual(device.vps_stops, [])
        # No extra start is attempted either: the recovery refuses to guess.
        self.assertEqual(device.start_calls, 0)
        self.assertFalse(guard.held)
        self.assertEqual(service.state["active_generation"], 1)
        self.assertFalse(journal.get("previous_restored"))

    def test_proven_non_start_undoes_the_server_side_switch(self):
        """A proven non-start is not ambiguous: the services are rolled back."""
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice(confirm=False)
        guard = FakeGuard()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = guard
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        def never_started(_session, *, handoff_token=None):
            raise mgs.ActivationNotStarted("tablet did not start the session")

        service.activate_package = never_started
        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "failed", result)
        # Nothing was activated, so the previous set is restored for real.
        self.assertEqual(device.vps_stops, [SESSION])
        self.assertEqual(device.start_calls, 0)
        self.assertEqual(service.state["active_generation"], 1)
        self.assertFalse(guard.held)

    def test_activation_error_still_promotes_when_the_tablet_reports_it_active(self):
        """Finishing an activation that really happened is not an interruption."""
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice(confirm=True)
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        def late_failure(_session, *, handoff_token=None):
            device.active = True        # the detached start did take effect
            raise mgs.PublicationError("socket closed after the request")

        service.activate_package = late_failure
        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "active")
        self.assertEqual(service.state["active_generation"], 2)
        self.assertIn("a1b2c3d4", service.state["active_index"])
        self.assertEqual(device.vps_stops, [])      # never reverted a live session

    def test_unacknowledged_guard_release_is_never_recorded_as_released(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        guard = FakeGuard(release_ok=False)
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = guard
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "active")
        # The generation is live, but the lease ownership is not claimed clean.
        self.assertTrue(service._journal()["guard_held"])

    def test_concurrent_instances_cannot_mutate_state(self):
        first = self.service()
        second = self.service()
        with first.state_lock():
            with self.assertRaises(mgs.StateLockedError):
                second.check_and_prepare_generation(
                    [{"id": "11111111", "status": "ready", "file_size": 100, "title": "x"}])
            with self.assertRaises(mgs.StateLockedError):
                second.publish_pending(manual=True)
            with self.assertRaises(mgs.StateLockedError):
                second.apply_task("a1b2c3d4", tasks=[])
            with self.assertRaises(mgs.StateLockedError):
                second.promote_pending_generation(
                    {"verified": True, "generation": 1, "manifest_id": None})
        # After release the second instance works normally.
        self.assertIsInstance(second._journal(), dict)

    def test_state_save_is_atomic_and_leaves_no_temporary_files(self):
        service = self.service()
        service.state["pending_generation"] = 4
        service._save_state()
        leftovers = [p.name for p in self.tmp_dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        self.assertEqual(json.loads(self.state_file.read_text())["pending_generation"], 4)

    def test_corrupt_state_file_fails_closed(self):
        self.state_file.write_text("{not json")
        with self.assertRaises(mgs.PublicationError):
            self.service()
        self.state_file.write_text('["not", "an", "object"]')
        with self.assertRaises(mgs.PublicationError):
            self.service()

    def test_peer_commit_is_detected_before_mutation(self):
        first = self.service()
        first.state["pending_generation"] = 2
        first._save_state()
        second = self.service()          # loads the same journal
        self.assertEqual(second.state["pending_generation"], 2)

        # The first instance commits a newer state...
        first.state["pending_generation"] = 3
        first._save_state()

        # ...so the second must refresh instead of overwriting it.
        with second.state_lock():
            self.assertEqual(second.state["pending_generation"], 3)

    def test_conflicting_local_and_peer_state_aborts(self):
        first = self.service()
        first.state["pending_generation"] = 2
        first._save_state()
        second = self.service()
        second.state["pending_generation"] = 99   # local, unsaved
        first.state["pending_generation"] = 3
        first._save_state()
        with self.assertRaises(mgs.PublicationError):
            with second.state_lock():
                pass

    def test_threads_cannot_enter_the_state_lock_concurrently(self):
        service = self.service()
        entered = threading.Event()
        release = threading.Event()
        attempted = threading.Event()
        errors = []

        def holder():
            with service.state_lock():
                entered.set()
                release.wait(timeout=5)

        def intruder():
            entered.wait(timeout=5)
            try:
                with service.state_lock():
                    errors.append("acquired")
            except mgs.StateLockedError:
                errors.append("blocked")
            finally:
                attempted.set()

        threads = [threading.Thread(target=holder), threading.Thread(target=intruder)]
        for thread in threads:
            thread.start()
        self.assertTrue(attempted.wait(timeout=5))
        release.set()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(errors, ["blocked"])

    # --------------------------------------------------- read-only confirmation
    def _activate_generation(self, service, device, *, generation=2, suffix="a",
                             legacy_script=False):
        """Publish one generation for real through the fake command surface."""
        session = service.session_name(generation, suffix * 32)
        staging, manifest_id = make_complete_package(
            self.tmp_dir, generation=generation, session=session,
            legacy_script=legacy_script)
        self._arm(service, staging, manifest_id, generation=generation, session=session)
        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        return session, staging, manifest_id, result

    def _stage_inventory(self, service, *, inventory, generation=3, suffix="c",
                         live_only=False):
        """Arm a consistent package for an arbitrary inventory list."""
        session = service.session_name(generation, suffix * 32)
        staging = self.tmp_dir / session
        staging.mkdir()
        for name in pkg_builder.ARTIFACTS:
            (staging / name).write_bytes(f"artifact:{name}\n".encode())
        write_runtime_files(staging)
        if live_only:
            manifest_id = write_manifest(staging, generation, extra_vod=(),
                                         include_default_vod=False)
        else:
            extra = tuple((t["id"], int(t["file_size"]), str(t.get("remote_md5") or "c" * 32))
                          for t in inventory if str(t.get("id")) != "a1b2c3d4")
            manifest_id = write_manifest(staging, generation, extra_vod=extra)
        write_sums(staging)

        def fake_prepare(_tasks, _export=None):
            self._arm(service, staging, manifest_id, generation=generation,
                      session=session, tasks=tuple(inventory))
            return {"manifest_id": manifest_id}

        service.check_and_prepare_generation = lambda tasks, export=None: fake_prepare(tasks)
        return session, staging, manifest_id

    def _stage_next_generation(self, service, *, generation=3, suffix="c"):
        """Arm a second generation whose package only exists in staging."""
        session = service.session_name(generation, suffix * 32)
        new_tasks = [TASK_READY, {"id": "b2c3d4e5", "status": "ready",
                                  "file_size": 5 * 1024 * 1024, "title": "New",
                                  "remote_md5": "b" * 32}]
        staging = self.tmp_dir / session
        staging.mkdir()
        for name in pkg_builder.ARTIFACTS:
            (staging / name).write_bytes(f"artifact:{name}\n".encode())
        write_runtime_files(staging)
        manifest_id = write_manifest(
            staging, generation, extra_vod=(("b2c3d4e5", 5 * 1024 * 1024, "b" * 32),))
        write_sums(staging)

        def fake_prepare(_tasks, _export=None):
            self._arm(service, staging, manifest_id, generation=generation,
                      session=session, tasks=tuple(new_tasks))
            return {"manifest_id": manifest_id}

        service.check_and_prepare_generation = lambda tasks, export=None: fake_prepare(tasks)
        return session, staging, manifest_id, new_tasks

    def test_confirm_task_proves_generation_and_revision_without_mutating(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        _session, _staging, _manifest, result = self._activate_generation(service, device)
        self.assertEqual(result["state"], "active")

        start_calls = device.start_calls
        rollback_calls = device.rollback_calls
        pushes = [c for c in device.commands if "push" in " ".join(c)]
        confirmation = service.confirm_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertIs(confirmation["confirmed"], True, confirmation)
        # Read-only: it never re-connects the pendrive nor re-pushes the package.
        self.assertEqual(device.start_calls, start_calls)
        self.assertEqual(device.rollback_calls, rollback_calls)
        self.assertEqual([c for c in device.commands if "push" in " ".join(c)], pushes)

        # A different size/revision is NOT the published content.
        changed = dict(TASK_READY, file_size=TASK_READY["file_size"] + 1)
        self.assertIs(service.confirm_task("a1b2c3d4", tasks=[changed])["confirmed"], False)
        # The tablet answering another generation is not proof either.
        device.confirm = False
        self.assertIs(service.confirm_task("a1b2c3d4", tasks=[TASK_READY])["confirmed"], False)

    def test_confirm_task_is_unknown_without_tablet_or_inventory(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        self._activate_generation(service, device)
        # No inventory to compare revisions against: unknown, never success.
        self.assertIsNone(service.confirm_task("a1b2c3d4", tasks=None)["confirmed"])
        self.assertIsNone(
            service.confirm_task("a1b2c3d4", tasks=[{"id": "a1b2c3d4"}])["confirmed"])

        # No active generation recorded: there is nothing to confirm.
        service.state["active_session"] = None
        service._save_state()
        self.assertIs(service.confirm_task("a1b2c3d4", tasks=[TASK_READY])["confirmed"], False)

    # ------------------------------------------------------ N -> N+1 takeover
    def test_vps_package_is_transferred_and_reverified_before_the_switch(self):
        staging, _manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        transport = mgs.TabletTransport(ssh_host="oracle", runner=device)
        service = self.service(transport=transport)
        service.vps_package_root = "/var/lib/usb-stream-tv/generations"
        with mock.patch.object(mgs, "subprocess_run",
                               return_value=CompletedProcess([], 0, "", "")) as tar_run:
            remote = service.vps_publish_package(staging, SESSION)
        # The switch receives the remote, persistent generation path.
        self.assertEqual(remote, "/var/lib/usb-stream-tv/generations/" + SESSION)
        self.assertEqual(tar_run.call_args.args[0][0], "tar")
        joined = "\n".join(" ".join(command) for command in device.commands)
        self.assertIn("scp", joined)
        self.assertIn("sha256sum -c", joined)
        self.assertIn(remote, joined)
        # The local staging path is never handed to a remote command.
        self.assertNotIn(str(staging), joined)

    def test_vps_package_refuses_an_incomplete_generation_before_transfer(self):
        staging, _manifest_id = make_complete_package(self.tmp_dir)
        (staging / "fuse_ntfs").unlink()
        device = FakeDevice()
        transport = mgs.TabletTransport(ssh_host="oracle", runner=device)
        service = self.service(transport=transport)
        with self.assertRaises(mgs.PublicationError) as ctx:
            service.vps_publish_package(staging, SESSION)
        self.assertIn("incomplete", str(ctx.exception))
        self.assertEqual(device.commands, [])

    def test_vps_command_uses_remote_paths_and_never_an_empty_app_dir(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(
            ssh_host="oracle", runner=device))
        # No app dir configured: no ``cd ''`` prefix and no empty argument.
        argv = service._vps_argv("status", SESSION)
        self.assertEqual(argv[0], "ssh")
        self.assertNotIn("''", argv[-1])
        # A configured app dir is the only ``cd`` source, applied on the remote.
        service.vps_app_dir = "/opt/containers/apps/usb-stream-tv"
        argv = service._vps_argv("status", SESSION)
        self.assertIn("cd '/opt/containers/apps/usb-stream-tv' &&", argv[-1])
        self.assertIn("switch_mixed_generation_vps.sh", argv[-1])

    def test_vps_package_is_the_local_staging_dir_without_a_hop(self):
        staging, _manifest_id = make_complete_package(self.tmp_dir)
        service = self.service()
        self.assertEqual(service.vps_publish_package(staging, SESSION), str(staging))

    def test_vps_package_refuses_to_replace_an_existing_generation(self):
        staging, _manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice(remote_generation_exists=True)
        service = self.service(transport=mgs.TabletTransport(ssh_host="oracle",
                                                             runner=device))
        service.vps_package_root = "/var/lib/usb-stream-tv/generations"
        with mock.patch.object(mgs, "subprocess_run",
                               return_value=CompletedProcess([], 0, "", "")):
            with self.assertRaises(mgs.PublicationError) as ctx:
                service.vps_publish_package(staging, SESSION)
        self.assertIn("already exists", str(ctx.exception))
        joined = "\n".join(" ".join(command) for command in device.commands)
        # A retry never erases the directory of an existing generation.
        self.assertNotIn("rm -rf", joined)

    def test_generation_services_are_configured_before_the_tunnels(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        self.assertEqual(service.apply_task("a1b2c3d4", tasks=[TASK_READY])["state"],
                         "active")
        commands = [" ".join(c) for c in device.commands]
        switch_at = next(i for i, c in enumerate(commands) if "switch_mixed_generation_vps.sh" in c)
        tunnel_at = next(i for i, c in enumerate(commands) if "tunnel-check" in c)
        preflight_at = next(i for i, c in enumerate(commands) if "'preflight'" in c)
        start_at = next(i for i, c in enumerate(commands)
                        if "activate_live_favorites_tablet.sh" in c and "'start-detached'" in c)
        # Services -> tunnels -> preflight -> LUN switch. A cold start cannot
        # work in any other order: the tunnel probe needs something listening.
        self.assertLess(switch_at, tunnel_at)
        self.assertLess(tunnel_at, preflight_at)
        self.assertLess(preflight_at, start_at)
        # A cold start has no previous session, so no supersede is requested.
        self.assertNotIn("--supersede", commands[switch_at])

    def test_vps_switch_supersedes_the_recorded_previous_session(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        first_session, _staging, _manifest, result = self._activate_generation(service, device)
        self.assertEqual(result["state"], "active")

        _session3, _staging3, _manifest3, new_tasks = self._stage_next_generation(service)
        second = service.apply_task("b2c3d4e5", tasks=new_tasks)
        self.assertEqual(second["state"], "active")
        # The candidate had to take the shared ports over from the previous
        # generation, and the script was told exactly which session to supersede.
        self.assertIn("--supersede", device.sessions_started[-1])
        index = device.sessions_started[-1].index("--supersede")
        self.assertEqual(device.sessions_started[-1][index + 1], first_session)
        self.assertEqual(device.takeovers, [first_session])

    def test_vps_switch_retry_accepts_an_already_active_generation(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        staging, manifest_id = make_complete_package(self.tmp_dir)
        self._arm(service, staging, manifest_id)
        # First publication gets as far as the services, then fails later.
        self.assertEqual(service.apply_task("a1b2c3d4", tasks=[TASK_READY])["state"],
                         "active")
        # The services for this session are already live: a retry must treat the
        # script's idempotent answer as success instead of failing.
        service.vps_publish_package = lambda *a, **k: str(staging)
        original = device._vps

        def already_active(argv):
            tokens = list(argv)
            if tokens[2] == "start":
                return CompletedProcess(argv, 0, "already_active session=x\n", "")
            return original(argv)

        device._vps = already_active
        service.vps_switch_start(staging, SESSION, 2)
        self.assertTrue(service._journal().get("vps_generation_dir"))

    def test_vps_switch_failure_never_claims_the_previous_set_was_preserved(self):
        device = FakeDevice(vps_start_ok=False, vps_restore_failed=True)
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        staging, manifest_id = make_complete_package(self.tmp_dir)
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "failed")
        self.assertIn("not be restored", result["reason"])
        self.assertTrue(service.state["publication"]["previous_services_down"])
        self.assertEqual(device.start_calls, 0)

    def test_handoff_lease_is_acquired_and_renewed_by_its_owner(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        staging, manifest_id = make_complete_package(self.tmp_dir)
        self._arm(service, staging, manifest_id)

        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "active")
        self.assertEqual(len(device.handoff_leases), 1)
        token = device.handoff_leases[0]
        # The same owner renewed it before touching the LUN, so it can never
        # expire between the lease and the activation.
        self.assertIn(token, device.lease_renewals)
        self.assertGreaterEqual(device.lease_renewals.count(token), 1)

    def test_lease_renewal_failure_aborts_before_the_tablet_switch(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice(refuse_renew=True)
        service = self.service(transport=mgs.TabletTransport(runner=device))
        guard = FakeGuard()
        service.guard_request = guard
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "failed")
        self.assertIn("handoff lease", result["reason"])
        # Nothing touched the LUN, and the services were put back.
        self.assertEqual(device.start_calls, 0)
        self.assertEqual(device.takeovers, [])
        self.assertEqual(device.vps_stops, [SESSION])
        self.assertFalse(guard.held)

    def test_a_masked_exit_code_cannot_fake_a_tunnel(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        # tunnel-check/tunnel-start answer with exit status 0 but no success
        # marker, exactly like adb shell su -c on the real tablet.
        device = FakeDevice(tunnel_ok=False)
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "failed")
        self.assertIn("tunnel", result["reason"])
        self.assertEqual(device.start_calls, 0)

    def test_a_masked_exit_code_cannot_fake_the_preflight(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice(preflight_ok=False)
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status()

        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(result["state"], "failed")
        self.assertIn("preflight", result["reason"])
        self.assertEqual(device.start_calls, 0)
        self.assertEqual(device.vps_stops, [SESSION])

    def test_a_manual_click_may_confirm_a_legacy_playback_interruption(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        self._arm(service, staging, manifest_id)
        # The tablet is in the legacy VOD mode: a real signal, but it does not
        # prove the TV is reading the mixed pendrive.
        service.fetch_server_status = lambda: idle_status(active_vod="ab27600d")

        blocked = service.apply_task("a1b2c3d4", tasks=[TASK_READY])
        self.assertEqual(blocked["state"], "blocked")
        self.assertTrue(blocked["interruptible"])
        self.assertEqual(device.start_calls, 0)

        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY],
                                    confirm_interrupt=True)
        self.assertEqual(result["state"], "active")
        self.assertEqual(device.start_calls, 1)
        # The interruption is never silent: it is journalled with its reason.
        self.assertEqual(service.state["publication"]["interrupted_legacy"],
                         "active_vod_selected:ab27600d")

    def test_unknown_telemetry_requires_an_authorised_manual_request(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        self._arm(service, staging, manifest_id)
        for status in (None, idle_status(server_time=900.0), idle_status(tablet_online=False),
                       idle_status(tv_usb_state="WEIRD")):
            with self.subTest(status=status):
                service.fetch_server_status = (lambda: None) if status is None else \
                    (lambda: status)
                result = service.apply_task("a1b2c3d4", tasks=[TASK_READY],
                                            confirm_interrupt=False)
                self.assertEqual(result["state"], "blocked")
                self.assertTrue(result["interruptible"])
        self.assertEqual(device.start_calls, 0)
        self.assertEqual(device.commands, [])
        service.fetch_server_status = lambda: idle_status(tv_usb_state="WEIRD")
        result = service.apply_task("a1b2c3d4", tasks=[TASK_READY],
                                    confirm_interrupt=True)
        self.assertEqual(result["state"], "active")
        self.assertEqual(device.start_calls, 1)

    def test_unknown_override_is_manual_and_limited_to_known_reasons(self):
        service = self.service()
        for reason in ("server_unreachable_fail_closed", "stale_status_fail_closed",
                       "tablet_telemetry_offline_fail_closed", "unknown_usb_state_fail_closed:WEIRD"):
            with self.subTest(reason=reason):
                self.assertTrue(service.may_interrupt_playback(reason, manual=True, confirm_interrupt=True))
                self.assertFalse(service.may_interrupt_playback(reason, manual=False, confirm_interrupt=True))
                self.assertFalse(service.may_interrupt_playback(reason, manual=True, confirm_interrupt=False))
        self.assertFalse(service.may_interrupt_playback("unexpected_reason", manual=True, confirm_interrupt=True))

    def test_the_automatic_path_can_never_confirm_an_interruption(self):
        staging, manifest_id = make_complete_package(self.tmp_dir)
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        self._arm(service, staging, manifest_id)
        service.fetch_server_status = lambda: idle_status(active_vod="ab27600d")
        self.assertFalse(service.may_interrupt_playback(
            "active_vod_selected:ab27600d", manual=False, confirm_interrupt=True))
        result = service.publish_pending(manual=False, confirm_interrupt=True)
        self.assertEqual(result["state"], "manual_required")
        self.assertEqual(device.commands, [])

    def test_handoff_takeover_refusal_keeps_the_previous_generation(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        first_session, _staging, _manifest, result = self._activate_generation(service, device)
        self.assertEqual(result["state"], "active")

        device.refuse_takeover = True
        _session3, _staging3, _manifest3, new_tasks = self._stage_next_generation(service)
        second = service.apply_task("b2c3d4e5", tasks=new_tasks)
        # A refusal before any signal is a KNOWN state: the previous supervisor
        # still owns the lock, so the publication aborts and the previous
        # generation is genuinely untouched.
        self.assertEqual(second["state"], "failed", second)
        self.assertIn("refused the takeover", second["reason"])
        self.assertEqual(device.takeovers, [])
        self.assertEqual(device.start_calls, 1)
        self.assertEqual(service.state["active_generation"], 2)
        self.assertEqual(service.state["active_session"], first_session)
        self.assertFalse(service.state["publication"]["previous_restored"])

    def test_guard_lost_right_after_takeover_restores_the_previous_generation(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        guard = FakeGuard()
        service.guard_request = guard
        service.fetch_server_status = lambda: idle_status()
        first_session, _staging, _manifest, result = self._activate_generation(service, device)
        self.assertEqual(result["state"], "active")

        # The guard is lost for good right after the takeover and right before
        # the new LUN switch: it must abort without activating, and restore the
        # generation whose supervisor was already stopped.
        guard.lose_when = lambda: bool(device.takeovers)
        session3, _staging3, _manifest3, new_tasks = self._stage_next_generation(service)
        second = service.apply_task("b2c3d4e5", tasks=new_tasks)
        self.assertEqual(second["state"], "failed", second)
        self.assertTrue(second["detail"].get("previous_restored"), second)
        # The previous supervisor was taken over and then rebuilt from its own
        # package; the services were rolled back to the previous session.
        self.assertEqual(device.takeovers, [first_session])
        self.assertIn(session3, device.vps_stops)
        self.assertEqual(service.state["active_generation"], 2)
        self.assertEqual(service.state["active_session"], first_session)

    def test_lost_takeover_answer_never_claims_previous_is_intact(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        first_session, _staging, _manifest, result = self._activate_generation(service, device)
        self.assertEqual(result["state"], "active")
        session3, _staging3, _manifest3, new_tasks = self._stage_next_generation(service)

        def lost_answer(*_args, **_kwargs):
            # The previous supervisor stopped, but the transport lost the
            # acknowledgement. This must never be reported as clean failure.
            device.active = False
            raise mgs.PublicationError("tablet gave no takeover answer")

        with mock.patch.object(service, "handoff_takeover", side_effect=lost_answer):
            result = service.apply_task("b2c3d4e5", tasks=new_tasks)
        self.assertEqual(result["state"], "unconfirmed", result)
        self.assertEqual(service.state["publication"]["phase"], "unconfirmed")
        self.assertTrue(service.state["publication"]["manual_reconciliation_required"])
        self.assertFalse(service.state["publication"]["previous_restored"])
        self.assertEqual(device.start_calls, 1, "must not blindly reactivate either generation")
        self.assertNotIn(session3, device.vps_stops)
        self.assertEqual(service.state["active_session"], first_session)


    def test_a_masked_takeover_failure_keeps_the_tablet_diagnostic(self):
        """adb/su masks the exit status, so the stderr text IS the diagnosis."""
        device = FakeDevice(takeover_lost_answer=True)
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        _session, _staging, _manifest, result = self._activate_generation(service, device)
        self.assertEqual(result["state"], "active")
        _session3, _staging3, _manifest3, new_tasks = self._stage_next_generation(service)

        second = service.apply_task("b2c3d4e5", tasks=new_tasks)
        # The tablet's own reason survives instead of a generic message.
        detail = str(second.get("detail") or second)
        self.assertIn("takeover timeout", detail)
        self.assertNotIn("no takeover answer\n", detail)
        # And the ambiguous outcome is recorded, never a clean failure.
        self.assertEqual(second["state"], "unconfirmed", second)
        self.assertEqual(service.state["publication"]["phase"], "unconfirmed")
        self.assertTrue(service.state["publication"]["manual_reconciliation_required"])
        self.assertFalse(service.state["publication"]["previous_restored"])

    def test_takeover_answer_text_never_leaks_a_token(self):
        token = "a" * 32
        text = f"[favorites] takeover refused for {token}; handoff lease is not ours"
        clean = mgs.sanitize_tablet_output(text)
        self.assertNotIn(token, clean)
        self.assertIn("<redacted>", clean)
        self.assertIn("handoff lease is not ours", clean)

    def test_a_supervisor_without_the_protocol_is_never_signalled(self):
        """The exact production failure: killing a supervisor that cannot ack.

        Such a session restores the legacy VOD mode on TERM and records nothing,
        so the switch must refuse BEFORE signalling anything.
        """
        old = FakeDevice(inherit_old_script=True)
        service = self.service(transport=mgs.TabletTransport(runner=old))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        # The published session ships the script of the revision that built it,
        # and its supervisor is the live lock owner.
        old.lock_session = service.session_name(2, "a" * 32)
        _s, _st, _m, result = self._activate_generation(service, old,
                                                        legacy_script=True)
        self.assertEqual(result["state"], "active")
        _s3, _st3, _m3, new_tasks = self._stage_next_generation(service)

        second = service.apply_task("b2c3d4e5", tasks=new_tasks)
        # A supervisor that cannot acknowledge is refused BEFORE any signal, so
        # this is a KNOWN failure - never reported as ambiguous, and never as if
        # something might have happened.
        self.assertEqual(second["state"], "failed", second)
        self.assertNotEqual(service.state["publication"]["phase"], "unconfirmed")
        # Nothing was signalled and nothing was touched.
        self.assertEqual(old.takeovers, [])
        self.assertEqual(old.start_calls, 1)
        self.assertEqual(service.state["active_generation"], 2)
        self.assertIn("cannot accept a handoff", json.dumps(second))

    def test_takeover_grammar_rejects_contradictions_and_missing_proof(self):
        def fake_with(answer):
            device = FakeDevice()
            # The real transport answers the discovery and capability commands;
            # only the takeover marker itself is scripted here.
            real = mgs.TabletTransport(runner=device)

            class Fake(mgs.TabletTransport):
                def root_shell(self, command, check=True, timeout=None):
                    if "handoff-takeover" not in command:
                        return real.root_shell(command, check=check, timeout=timeout)

                    class R:
                        returncode = 0
                        stdout = answer
                        stderr = ""
                    return R()

            return Fake(runner=device)

        service = mgs.MixedGenerationService.__new__(mgs.MixedGenerationService)
        service._package_dir_on_tablet = lambda s: "/data/local/tmp/live-favorites/" + s
        # Contradictory lines, a missing ack/already_free, or a doubled field are
        # never success and never a clean refusal.
        for answer in (
                "takeover released=1\ntakeover released=0\n",
                "takeover released=1\n",
                "takeover released=1 released=0\n",
                "takeover released=1 acked=0\n",
                "noise\ntakeover released=1\n"):
            with self.subTest(answer=answer):
                service.transport = fake_with(answer)
                with self.assertRaises((mgs.TakeoverUncertain,
                                        mgs.PreviousSupervisorTakeoverRefused)):
                    service.handoff_takeover("sess-x", "b" * 32, "sess-prev")
        # The two documented success shapes are accepted.
        for answer in ("takeover released=1 acked=1 owner=1 previous=p\n",
                       "takeover released=1 already_free=1 previous=p\n"):
            with self.subTest(answer=answer):
                service.transport = fake_with(answer)
                self.assertTrue(service.handoff_takeover("sess-x", "b" * 32, "sess-prev"))

    def test_previous_supervisor_capability_reports_both_answers(self):
        capable = FakeDevice()
        # The proof is the RUNNING supervisor's own announcement, so the lock must
        # be held by the session being asked about. A file on disk is not
        # evidence: a later revision can replace it while the old process still
        # supervises.
        capable.lock_session = "sess-a"
        service = self.service(transport=mgs.TabletTransport(runner=capable))
        self.assertEqual(service.previous_supervisor_capability("sess-a"), (True, "ok"))

        old = FakeDevice(inherit_old_script=True)
        old.lock_session = "sess-a"
        service = self.service(transport=mgs.TabletTransport(runner=old))
        ok, reason = service.previous_supervisor_capability("sess-a")
        self.assertFalse(ok)
        self.assertEqual(reason, "previous_supervisor_not_handoff_capable")

        # A lock owned by somebody else is not this session's supervisor: there
        # is nothing to take over, so the switch is free.
        old.lock_session = "other-session"
        self.assertEqual(service.previous_supervisor_capability("sess-a"),
                         (True, "no_previous_supervisor_running"))

        # No lock at all likewise means nothing to signal.
        free = FakeDevice(inherit_old_script=True)
        free.lock_owner = False
        service = self.service(transport=mgs.TabletTransport(runner=free))
        self.assertEqual(service.previous_supervisor_capability("sess-a"),
                         (True, "no_previous_supervisor_running"))

        # No transport and an unsafe name are both refusals, never a pass.
        service = self.service()
        self.assertEqual(service.previous_supervisor_capability("sess-a"),
                         (False, "no_tablet_transport"))
        self.assertEqual(service.previous_supervisor_capability("../etc"),
                         (False, "invalid_previous_session"))

    def test_handoff_takeover_reads_the_marker_not_the_exit_status(self):
        """A refusal is only believed when the marker says so."""
        def fake_with(answer, *, capable=True, on_stderr=False, announce=None):
            # The running supervisor's own ``describe`` answer is the capability
            # proof now, so an older revision is modelled by the device itself
            # (``inherit_old_script``) instead of by intercepting a file read.
            capable_now = capable if announce is None else announce
            device = FakeDevice(inherit_old_script=not capable_now)
            real = mgs.TabletTransport(runner=device)

            class Fake(mgs.TabletTransport):
                def root_shell(self, command, check=True, timeout=None):
                    if "handoff-takeover" not in command:
                        return real.root_shell(command, check=check, timeout=timeout)

                    class R:
                        returncode = 0
                        stdout = "" if on_stderr else answer
                        stderr = answer if on_stderr else ""
                    return R()

            return Fake(runner=device)

        service = mgs.MixedGenerationService.__new__(mgs.MixedGenerationService)
        service._package_dir_on_tablet = lambda s: "/data/local/tmp/live-favorites/" + s
        service.transport = fake_with("takeover released=0 reason=lock_foreign\n",
                                      announce=True)
        with self.assertRaises(mgs.PreviousSupervisorTakeoverRefused):
            service.handoff_takeover("sess-x", "b" * 32, "sess-prev")

        # A marker that only appears on stderr is not an answer: the producer's
        # contract is one line on stdout, and rc is masked on this tablet.
        service.transport = fake_with("takeover released=1\n", on_stderr=True)
        with self.assertRaises(mgs.TakeoverUncertain):
            service.handoff_takeover("sess-x", "b" * 32, "sess-prev")

        # Lookalike values and contradictory answers are never success.
        for lookalike in ("takeover released=10\n", "takeover released=1\n"
                          "takeover released=0\n"):
            with self.subTest(answer=lookalike):
                service.transport = fake_with(lookalike)
                with self.assertRaises(mgs.TakeoverUncertain):
                    service.handoff_takeover("sess-x", "b" * 32, "sess-prev")

        service.transport = fake_with(
            "takeover released=unknown reason=no_ack\n")
        with self.assertRaises(mgs.TakeoverUncertain) as ctx:
            service.handoff_takeover("sess-x", "b" * 32, "sess-prev")
        self.assertIn("no_ack", str(ctx.exception))

        # A supervisor that is RUNNING but cannot acknowledge is refused BEFORE
        # any signal. The lock owner must be the previous session for that.
        device = FakeDevice(inherit_old_script=True)
        device.lock_session = "sess-prev"

        # Delegate to the fake device through the real transport: the refusal
        # must come from the running revision's own announcement, and the file on
        # disk must NOT be consulted (a later revision can replace it while the
        # old process still supervises).
        real = mgs.TabletTransport(runner=device)

        service.transport = real
        with self.assertRaises(mgs.PreviousSupervisorNotUpgradable):
            service.handoff_takeover("sess-x", "b" * 32, "sess-prev")

    def test_refresh_publishes_the_inventory_without_any_ready_vod(self):
        """The last VOD deleted leaves nothing to anchor: the list still updates."""
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        # First generation is published normally (one ready VOD).
        _s, _st, _m, first = self._activate_generation(service, device)
        self.assertEqual(first["state"], "active")

        # Now every ready VOD is gone: there is nothing left to anchor on.
        service.fetch_vod_status = lambda: {"tasks": []}
        self._stage_inventory(service, inventory=[], generation=3,
                              suffix="d", live_only=True)
        result = service.refresh_inventory(tasks=[])
        self.assertIn(result["state"], ("active", "already_current"), result)
        # The registry entry is keyed by the inventory, so the dashboard can
        # follow a refresh that has no video identity.
        self.assertEqual(service.state["apply"]["task_id"], mgs.REFRESH_KEY)

    def test_refresh_with_an_unchanged_inventory_proves_the_list(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        _s, _st, _m, first = self._activate_generation(service, device)
        self.assertEqual(first["state"], "active")
        before = device.start_calls

        # Same inventory, tablet still serving that generation: nothing to do.
        result = service.refresh_inventory(tasks=[TASK_READY])
        self.assertEqual(result["state"], "already_current", result)
        self.assertIs(result["confirmed"], True)
        self.assertEqual(device.start_calls, before)

    def test_refresh_never_claims_success_when_the_tablet_is_unreachable(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        self._activate_generation(service, device)
        device.unreachable = True

        result = service.refresh_inventory(tasks=[TASK_READY])
        self.assertEqual(result["state"], "unknown", result)
        self.assertIsNot(result.get("confirmed"), True)

    def test_confirm_inventory_detects_a_deleted_entry_in_the_tablet_list(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        service.fetch_vod_status = lambda: {"tasks": [TASK_READY]}
        session, _staging, _manifest_id, first = self._activate_generation(service, device)
        self.assertEqual(first["state"], "active")
        self.assertIs(service.confirm_inventory()["confirmed"], True)

        # The tablet still offers a task the recorded generation does not
        # contain (exactly what a stale list looks like after a deletion): that
        # is a mismatch, never a success.
        package = f"/data/local/tmp/live-favorites/{session}"
        allowlist = json.loads(device.files[package + "/vod_allowlist.json"].decode())
        allowlist["tasks"].append({"task_id": "deadbeef", "size": 10,
                                   "revision": "md5:" + "0" * 32})
        device.files[package + "/vod_allowlist.json"] = json.dumps(allowlist).encode()
        result = service.confirm_inventory()
        self.assertIs(result["confirmed"], False)
        self.assertEqual(result["reason"], "tablet_list_differs_from_generation")
        self.assertEqual(result["extra_count"], 1)

        # And a tablet that lost an entry the generation recorded is a mismatch
        # in the other direction.
        allowlist["tasks"] = [t for t in allowlist["tasks"] if t["task_id"] != "deadbeef"]
        allowlist["tasks"] = allowlist["tasks"][1:]
        device.files[package + "/vod_allowlist.json"] = json.dumps(allowlist).encode()
        result = service.confirm_inventory()
        self.assertIs(result["confirmed"], False)
        self.assertEqual(result["missing_count"], 1)

    def test_confirm_inventory_fails_closed_for_deleted_or_unreadable_catalog(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        self._activate_generation(service, device)
        mismatch = service.confirm_inventory(tasks=[])
        self.assertIs(mismatch['confirmed'], False)
        self.assertEqual(mismatch['reason'], 'inventory_changed')
        service.fetch_vod_status = lambda: None
        unknown = service.confirm_inventory()
        self.assertIsNone(unknown['confirmed'])
        self.assertEqual(unknown['reason'], 'inventory_unavailable')


class EndToEndInventoryFlowTests(unittest.TestCase):
    """The two flows the user actually performs, end to end.

    These drive the real service over the fake command surface: inventory
    change -> refresh -> handoff -> activation -> confirmation, plus the
    already-current and ambiguity branches.
    """

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="test-e2e-"))
        self.state_file = self.tmp_dir / "state.json"
        self.export_config_path = self.tmp_dir / "export.json"
        self.staging_base = self.tmp_dir / "staging"
        self.staging_base.mkdir()
        self.clock = [1000.0]
        self.export_config_path.write_text(json.dumps({
            "schema_version": 1, "generation": 1,
            "identities": [{"identity": "sbt", "status": "approved",
                            "category": "Abertos", "filename": "SBT.trp",
                            "chosen": {"channel_id": "206080"}}]}))

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def service(self, **kwargs):
        params = dict(
            server_url="http://127.0.0.1:8080",
            export_config_path=self.export_config_path,
            staging_base=self.staging_base,
            state_file=self.state_file,
            now=lambda: self.clock[0],
            poll_interval=0.0,
            publication_timeout=0.0,
        )
        params.update(kwargs)
        return mgs.MixedGenerationService(**params)

    # -- helpers (same shapes the other suites use) --------------------------
    def _arm(self, service, staging, manifest_id, *, session=SESSION, phase="prepared",
             generation=2, tasks=(TASK_READY,)):
        state = service.state
        state.update({
            "pending_generation": generation,
            "pending_staging_dir": str(staging),
            "pending_inventory_hash": service.compute_inventory_hash(list(tasks)),
            "pending_manifest_id": manifest_id,
            "pending_session": session,
            "pending_index": service.generation_index(staging),
        })
        state["publication"] = {
            "generation": generation, "manifest_id": manifest_id,
            "inventory_hash": service.compute_inventory_hash(list(tasks)),
            "staging_dir": str(staging), "session": session, "phase": phase,
        }
        service._save_state()

    def _publish_first(self, service, device, *, tasks=(TASK_READY,)):
        session = service.session_name(2, "a" * 32)
        staging, manifest = make_complete_package(self.tmp_dir, generation=2,
                                                  session=session)
        self._arm(service, staging, manifest, session=session, tasks=tasks)
        return service.apply_task("a1b2c3d4", tasks=list(tasks))

    def _stage_for(self, service, *, inventory, generation=3, suffix="c",
                   live_only=False):
        session = service.session_name(generation, suffix * 32)
        staging = self.tmp_dir / session
        staging.mkdir()
        for name in pkg_builder.ARTIFACTS:
            (staging / name).write_bytes(f"artifact:{name}\n".encode())
        write_runtime_files(staging)
        if live_only:
            manifest_id = write_manifest(staging, generation, extra_vod=(),
                                         include_default_vod=False)
        else:
            extra = tuple((t["id"], int(t["file_size"]), str(t.get("remote_md5") or "c" * 32))
                          for t in inventory if str(t.get("id")) != "a1b2c3d4")
            manifest_id = write_manifest(staging, generation, extra_vod=extra,
                                         include_default_vod=any(
                                             str(t.get("id")) == "a1b2c3d4"
                                             for t in inventory))
        write_sums(staging)

        def fake_prepare(_tasks, _export=None):
            self._arm(service, staging, manifest_id, generation=generation,
                      session=session, tasks=tuple(inventory))
            return {"manifest_id": manifest_id}

        service.check_and_prepare_generation = lambda tasks, export=None: fake_prepare(tasks)
        return session, staging, manifest_id

    # -- A: deletion ---------------------------------------------------------
    def test_deletion_then_button_removes_the_item_and_reports_the_reconnect(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        first = self._publish_first(service, device)
        self.assertEqual(first["state"], "active")
        self.assertTrue(first["interrupts_playback"],
                        "publishing really reconnected the pendrive")

        # The user deletes the only ready VOD: the inventory is now empty.
        self._stage_for(service, inventory=[], generation=3, suffix="d", live_only=True)
        result = service.refresh_inventory(tasks=[])
        self.assertIn(result["state"], ("active", "already_current"), result)
        self.assertTrue(result["interrupts_playback"],
                        "a refresh that switched generations reconnected the pendrive")
        self.assertIs(result["reconnected"], True)
        # The new generation really is the active one, and the deleted item is
        # not part of it.
        self.assertNotIn("a1b2c3d4", service.state.get("active_index") or {})

    def test_deleting_with_other_videos_left_still_rebuilds_the_list(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        other = {"id": "b2c3d4e5", "status": "ready", "file_size": 5 * 1024 * 1024,
                 "title": "Outro", "remote_md5": "b" * 32}
        self._publish_first(service, device, tasks=(TASK_READY, other))

        # One video is deleted; another remains. The remaining one must NOT be
        # used as "proof" that the whole list is current.
        remaining = [other]
        self._stage_for(service, inventory=remaining)
        result = service.refresh_inventory(tasks=remaining)
        self.assertIn(result["state"], ("active", "already_current"), result)
        self.assertNotIn("a1b2c3d4", service.state.get("active_index") or {})
        self.assertIn("b2c3d4e5", service.state.get("active_index") or {})

    # -- B: new episode ------------------------------------------------------
    def test_new_episode_is_prepared_without_touching_the_tv_then_applied(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        first = self._publish_first(service, device)
        self.assertEqual(first["state"], "active")
        starts_before = device.start_calls

        # A download finishes: the daemon cycle only prepares (no publish).
        new_tasks = (TASK_READY,
                     {"id": "b2c3d4e5", "status": "ready", "file_size": 5 * 1024 * 1024,
                      "title": "Episodio novo", "remote_md5": "b" * 32})
        self._stage_for(service, inventory=new_tasks)
        prepared = service.check_and_prepare_generation(list(new_tasks))
        self.assertIsNotNone(prepared)
        self.assertEqual(device.start_calls, starts_before,
                         "preparing must never reconnect the TV")
        self.assertEqual(service.state["active_generation"], 2,
                         "preparing must not change the active generation")

        # The user taps NA TV: the prepared generation is applied.
        result = service.refresh_inventory(tasks=list(new_tasks))
        self.assertEqual(result["state"], "active", result)
        self.assertTrue(result["interrupts_playback"])
        self.assertIn("b2c3d4e5", service.state.get("active_index") or {})
        self.assertGreater(device.start_calls, starts_before)

    # -- C: already current --------------------------------------------------
    def test_current_list_does_not_reconnect_the_pendrive(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        self._publish_first(service, device)
        starts_before = device.start_calls

        result = service.refresh_inventory(tasks=[TASK_READY])
        self.assertEqual(result["state"], "already_current", result)
        self.assertFalse(result["interrupts_playback"],
                         "an already-current list must not claim an interruption")
        self.assertIs(result["reconnected"], False)
        self.assertEqual(device.start_calls, starts_before)
        self.assertIs(result["confirmed"], True)

    # -- ambiguity and transport -------------------------------------------
    def test_tablet_unreachable_reports_unknown_and_never_claims_success(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        self._publish_first(service, device)
        device.unreachable = True

        result = service.refresh_inventory(tasks=[TASK_READY])
        self.assertEqual(result["state"], "unknown", result)
        self.assertFalse(result["interrupts_playback"])
        self.assertIsNot(result.get("confirmed"), True)

    def test_second_click_while_publishing_is_idempotent(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        self._publish_first(service, device)
        # Simulate an in-flight publication.
        service._update_journal(phase="publishing", requested_task="inventory")
        starts_before = device.start_calls

        result = service.refresh_inventory(tasks=[TASK_READY])
        # A repeated click for the same update reports it, never restarts it.
        self.assertEqual(result["state"], "in_progress", result)
        self.assertEqual(device.start_calls, starts_before)

    def test_bridge_down_is_reported_and_never_falls_back_to_the_legacy_switch(self):
        """With mixed mode active but the bridge unavailable, the UI must not
        fall back to the legacy VOD switch: that would reconnect the pendrive
        with the old, wrong list."""
        html = Path("dashboard.html").read_text()
        self.assertIn("refreshMixedInventory", html)
        # The refresh path returns before the legacy branch when unavailable.
        start = html.index("        async function refreshMixedInventory(")
        end = html.index("        // Voltar do Cinema", start)
        body = html[start:end]
        self.assertIn("data.available === false", body)
        self.assertIn("indisponível", body)
        self.assertNotIn("/api/vod/play", body)


    # -- safety rails found in review ---------------------------------------
    def test_unreachable_catalog_never_becomes_an_empty_list(self):
        """A failed read must not publish 'no videos' and wipe the TV list."""
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        self._publish_first(service, device)
        starts_before = device.start_calls
        for bad in (None, {}, {"tasks": None}, {"tasks": "nope"}, []):
            with self.subTest(catalog=bad):
                service.fetch_vod_status = lambda bad=bad: bad
                result = service.refresh_inventory(tasks=None)
                self.assertEqual(result["state"], "unknown", result)
                self.assertEqual(result["reason"], "inventory_unavailable")
                self.assertFalse(result["interrupts_playback"])
        self.assertEqual(device.start_calls, starts_before)

    def test_a_genuinely_empty_catalog_is_still_valid(self):
        """No VODs left is a real state: it must be publishable."""
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        self._publish_first(service, device)
        self._stage_for(service, inventory=[], generation=3, suffix="d", live_only=True)
        service.fetch_vod_status = lambda: {"tasks": []}
        result = service.refresh_inventory(tasks=None)
        self.assertIn(result["state"], ("active", "already_current"), result)
        self.assertEqual(service.state.get("active_index"), {})

    def test_an_automatic_cycle_prepares_but_never_publishes(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        self._publish_first(service, device)
        starts_before = device.start_calls
        next_tasks = [TASK_READY, {"id": "b2c3d4e5", "status": "ready",
                                   "file_size": 5 * 1024 * 1024, "title": "Novo",
                                   "remote_md5": "b" * 32}]
        self._stage_for(service, inventory=next_tasks)

        result = service.refresh_inventory(tasks=list(next_tasks), manual=False)
        self.assertEqual(result["state"], "prepared", result)
        self.assertEqual(result["reason"], "manual_publication_required")
        self.assertFalse(result["interrupts_playback"])
        self.assertEqual(device.start_calls, starts_before)

    def test_the_catalog_changing_while_preparing_rebuilds_once(self):
        device = FakeDevice()
        service = self.service(transport=mgs.TabletTransport(runner=device))
        service.guard_request = FakeGuard()
        service.fetch_server_status = lambda: idle_status()
        self._publish_first(service, device)

        newer = [TASK_READY, {"id": "b2c3d4e5", "status": "ready",
                              "file_size": 5 * 1024 * 1024, "title": "Novo",
                              "remote_md5": "b" * 32}]
        # The first prepare sees the old list; the re-read sees the new one.
        calls = {"n": 0}

        def fake_prepare(_tasks, _export=None):
            calls["n"] += 1
            inventory = newer if calls["n"] > 1 else [TASK_READY]
            session = service.session_name(3 + calls["n"], "e" * 32)
            staging = self.tmp_dir / session
            staging.mkdir(exist_ok=True)
            for name in pkg_builder.ARTIFACTS:
                (staging / name).write_bytes(f"artifact:{name}\n".encode())
            write_runtime_files(staging)
            mid = write_manifest(staging, 3 + calls["n"],
                                 extra_vod=(("b2c3d4e5", 5 * 1024 * 1024, "b" * 32),))
            write_sums(staging)
            self._arm(service, staging, mid, generation=3 + calls["n"],
                      session=session, tasks=tuple(inventory))
            return {"manifest_id": mid}

        service.check_and_prepare_generation = lambda tasks, export=None: fake_prepare(tasks)
        service.fetch_vod_status = lambda: {"tasks": newer}
        result = service.refresh_inventory(tasks=None)
        self.assertGreaterEqual(calls["n"], 2, "the package must be rebuilt when the list moves")
        self.assertIn(result["state"], ("active", "already_current", "prepared"), result)
        if result["state"] == "active":
            self.assertIn("b2c3d4e5", service.state.get("active_index") or {})


class ApplyCliContractTest(unittest.TestCase):
    """The dashboard launches the real script, so its CLI contract is tested.

    Nothing is written to the tablet: the server is unreachable and publishing
    is disabled, so the publisher must answer with the documented JSON shape
    without ever touching a device.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mixed-cli-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_cli(self, *extra):
        env = dict(os.environ)
        env["MIXED_STATE_FILE"] = str(self.root / "state.json")
        command = [sys.executable, str(ROOT / "scripts" / "mixed_generation_service.py"),
                   "--server-url", "http://127.0.0.1:1", "--no-publish",
                   "--state-file", str(self.root / "state.json"),
                   "--staging-base", str(self.root / "staging"), "--json", *extra]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=60, env=env)
        return completed

    def test_apply_prints_the_documented_json_shape(self):
        completed = self.run_cli("--apply", "--task-id", "a1b2c3d4")
        self.assertEqual(completed.returncode, 1, completed.stderr)
        payload = json.loads(completed.stdout)
        for key in ("state", "generation", "interrupts_playback", "message", "reason"):
            self.assertIn(key, payload)
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["reason"], "task_not_ready")

    def test_apply_rejects_an_invalid_task_id_without_side_effects(self):
        completed = self.run_cli("--apply", "--task-id", "not-a-task")
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["reason"], "invalid_task_id")
        self.assertFalse(payload["interrupts_playback"])

    def test_apply_status_never_leaks_paths_or_tokens(self):
        completed = self.run_cli("--apply", "--status", "--task-id", "a1b2c3d4")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        raw = json.dumps(payload).lower()
        self.assertNotIn("token", raw)
        self.assertNotIn(str(self.root).lower(), raw)
        self.assertIn("state", payload)


class OperationTrackerTests(unittest.TestCase):
    """The publisher must adopt and finish the operation the endpoint created.

    Without this, the row stays in `preparing` forever: `preparing` holds the
    tablet slot, and the panel would show a publication that never ends. This is
    the caller side that a store-only test cannot prove.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.db = Path(self._dir.name) / "ops.db"
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        os.environ.pop("MIXED_OPERATIONS_DB", None)
        os.environ.pop("MIXED_TABLET_ID", None)
        os.environ.pop("ADB_SERIAL", None)
        os.environ.pop("TABLET_SERIAL", None)

    def _open_store(self):
        import mixed_operations
        return mixed_operations.OperationStore(self.db)

    def _create_operation_like_the_endpoint(self, state="preparing"):
        store = self._open_store()
        op_id, _ = store.begin(
            tablet_id=mgs._publisher_tablet_id(), principal="dashboard",
            intention={"task_id": "a1b2c3d4", "operation": "apply"},
            user_message="Atualização solicitada")
        store.set_phase(op_id, state=state, stage="launch")
        return op_id

    def test_without_a_store_the_tracker_is_inert(self):
        tracker = mgs._OperationTracker("tablet")
        self.assertFalse(tracker.active)
        # Must not raise: observability never blocks a publication.
        tracker.phase("preparing", "x")
        tracker.finish("succeeded", "x")

    def test_tracker_adopts_the_operation_the_endpoint_created(self):
        op_id = self._create_operation_like_the_endpoint()
        os.environ["MIXED_OPERATIONS_DB"] = str(self.db)
        tracker = mgs._OperationTracker(mgs._publisher_tablet_id(), "a1b2c3d4")
        self.assertEqual(tracker.claim(), "owned")
        self.assertTrue(tracker.active)
        self.assertEqual(tracker.operation_id, op_id)

    def test_worker_records_phases_and_finishes_the_operation(self):
        op_id = self._create_operation_like_the_endpoint()
        os.environ["MIXED_OPERATIONS_DB"] = str(self.db)
        tracker = mgs._OperationTracker(mgs._publisher_tablet_id(), "a1b2c3d4")
        self.assertEqual(tracker.claim(), "owned")
        tracker.phase("preparing", "publication", "Preparando a publicação")
        tracker.finish("succeeded", "Lista atualizada")
        store = self._open_store()
        row = store.get(op_id)
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(row["user_message"], "Lista atualizada")
        # The slot is free again, so the next click is not refused.
        self.assertIsNone(store.active_for_tablet(mgs._publisher_tablet_id()))

    def test_correlation_refuses_an_active_operation_for_another_task(self):
        """Uniqueness by tablet alone is not proof of association.

        The slot is unique per tablet, so adopting whatever is active would
        attach this run to an operation meant for a different video.
        """
        self._create_operation_like_the_endpoint()  # task a1b2c3d4
        os.environ["MIXED_OPERATIONS_DB"] = str(self.db)
        tracker = mgs._OperationTracker(mgs._publisher_tablet_id(), "ffffffff")
        self.assertEqual(tracker.claim(), "blocked")
        self.assertFalse(tracker.active)

    def test_a_publisher_with_no_active_operation_records_its_own_intent(self):
        """Ownership is recorded BEFORE any effect, not negotiated after it."""
        os.environ["MIXED_OPERATIONS_DB"] = str(self.db)
        tracker = mgs._OperationTracker(mgs._publisher_tablet_id(), "b2c3d4e5")
        self.assertEqual(tracker.claim(), "owned")
        row = self._open_store().get(tracker.operation_id)
        self.assertEqual(row["task_id"], "b2c3d4e5")
        self.assertEqual(row["principal"], "publisher")

    def test_no_store_configured_is_legacy_and_never_blocks(self):
        tracker = mgs._OperationTracker("tablet", "a1b2c3d4")
        self.assertEqual(tracker.claim(), "legacy")
        self.assertFalse(tracker.active)

    def test_an_unusable_store_blocks_instead_of_publishing_blind(self):
        """Configured but broken must refuse: no owner, no effect."""
        os.environ["MIXED_OPERATIONS_DB"] = str(self.db)
        self.db.write_text("not a database")
        tracker = mgs._OperationTracker(mgs._publisher_tablet_id(), "a1b2c3d4")
        self.assertEqual(tracker.claim(), "blocked")
        self.assertFalse(tracker.active)

    def test_ambiguous_results_require_reconciliation_not_a_clean_failure(self):
        # `unconfirmed`/`unknown` mean the device may have been touched.
        self.assertEqual(mgs._OPERATION_OUTCOME["unconfirmed"],
                         "needs_reconciliation")
        self.assertEqual(mgs._OPERATION_OUTCOME["unknown"],
                         "needs_reconciliation")
        self.assertEqual(mgs._OPERATION_OUTCOME["failed"], "failed_safe")
        self.assertEqual(mgs._OPERATION_OUTCOME["active"], "succeeded")
        # A publication owned by another worker must not be finished here.
        self.assertNotIn("in_progress", mgs._OPERATION_OUTCOME)

    def test_unrecognised_state_fails_closed_into_reconciliation(self):
        """An unknown result must not leave the operation stuck.

        Found by independent review: if the publisher ever returns a state that
        is not in the map (a future status, a typo), the row would stay in its
        last active state and hold the tablet slot forever. The ambiguity is
        exactly what reconciliation exists for.
        """
        for state in ("timeout", "cancelled", "some_future_state", ""):
            with self.subTest(state=state):
                self.assertEqual(mgs._operation_outcome_for(state),
                                 "needs_reconciliation")

    def test_known_states_map_to_their_outcome(self):
        self.assertEqual(mgs._operation_outcome_for("active"), "succeeded")
        self.assertEqual(mgs._operation_outcome_for("already_current"),
                         "succeeded")
        self.assertEqual(mgs._operation_outcome_for("failed"), "failed_safe")
        self.assertEqual(mgs._operation_outcome_for("blocked"), "blocked")
        self.assertEqual(mgs._operation_outcome_for("unconfirmed"),
                         "needs_reconciliation")

    def test_a_stale_operation_for_another_tablet_is_not_adopted(self):
        store = self._open_store()
        store.begin(tablet_id="other-tablet", principal="dashboard",
                    intention={"task_id": "deadbeef"})
        os.environ["MIXED_OPERATIONS_DB"] = str(self.db)
        tracker = mgs._OperationTracker("tablet")
        self.assertFalse(tracker.active)

    def test_a_broken_store_does_not_stop_the_publisher(self):
        os.environ["MIXED_OPERATIONS_DB"] = str(Path(self._dir.name) / "nope" / "x.db")
        Path(os.environ["MIXED_OPERATIONS_DB"]).parent.mkdir(parents=True, exist_ok=True)
        Path(os.environ["MIXED_OPERATIONS_DB"]).write_text("not a database")
        tracker = mgs._OperationTracker("tablet")
        self.assertFalse(tracker.active)
        tracker.phase("preparing")
        tracker.finish("succeeded", "x")


if __name__ == "__main__":
    unittest.main()


class SupervisorIdentityTests(unittest.TestCase):
    """Fase 2: identity is an INCARNATION, not a path.

    Covers the scenarios the architect required: recovery into an alternative
    folder, same manifest with a different owner, and a reused PID.
    """

    LINE = ("describe protocol=handoff.ack 1 boot_id=abc123 generation=18 "
            "manifest_id=b9d04e0a package_digest=aaa111 runtime_digest=bbb222 "
            "supervisor=1076-4242 candidate=1350-5151 "
            "lun=/data/local/tmp/x/mnt/tv_stream.img lock_owned=1 "
            "path=/data/local/tmp/live-favorites/mixed-gen18-0ca1b784")

    def parse(self, line=None):
        return mgs.SupervisorIdentity.parse(self.LINE if line is None else line)

    def test_parses_every_reported_field(self):
        ident = self.parse()
        self.assertEqual(ident.protocol, "handoff.ack 1")
        self.assertEqual(ident.generation, "18")
        self.assertEqual(ident.manifest_id, "b9d04e0a")
        self.assertEqual(ident.incarnation, "1076-4242")
        self.assertTrue(ident.lock_owned)

    def test_a_value_with_spaces_is_not_truncated(self):
        """The protocol announcement itself contains a space.

        Splitting on whitespace silently cut it to `handoff.ack`, which would
        make a valid supervisor look unsupported.
        """
        ident = self.parse()
        self.assertEqual(ident.protocol, "handoff.ack 1")

    def test_a_path_with_spaces_is_not_truncated(self):
        alt = self.LINE.replace(
            "path=/data/local/tmp/live-favorites/mixed-gen18-0ca1b784",
            "path=/data/local/tmp/live-favorites/gen 18 recuperada")
        self.assertEqual(self.parse(alt).path,
                         "/data/local/tmp/live-favorites/gen 18 recuperada")

    def test_recovery_in_an_alternative_folder_keeps_the_same_identity(self):
        """The case that refused publication 19: same supervisor, other path."""
        recovered = self.LINE.replace(
            "path=/data/local/tmp/live-favorites/mixed-gen18-0ca1b784",
            "path=/data/local/tmp/live-favorites/gen18-recuperada")
        original, moved = self.parse(), self.parse(recovered)
        self.assertNotEqual(original.path, moved.path)
        self.assertTrue(original.same_incarnation(moved))

    def test_a_reused_pid_is_not_the_same_supervisor(self):
        """A pid alone is recycled by the kernel; the start time is the proof."""
        reused = self.LINE.replace("supervisor=1076-4242", "supervisor=1076-9999")
        self.assertFalse(self.parse().same_incarnation(self.parse(reused)))

    def test_a_reboot_changes_the_incarnation(self):
        rebooted = (self.LINE.replace("supervisor=1076-4242", "supervisor=2048-77")
                    .replace("boot_id=abc123", "boot_id=def456"))
        ident = self.parse(rebooted)
        self.assertFalse(self.parse().same_incarnation(ident))
        self.assertEqual(ident.boot_id, "def456")

    def test_same_manifest_with_a_different_owner_is_a_different_instance(self):
        """Equal manifest alone does not prove it is the same supervisor."""
        other = (self.LINE.replace("supervisor=1076-4242", "supervisor=3000-111")
                 .replace("path=/data/local/tmp/live-favorites/mixed-gen18-0ca1b784",
                          "path=/data/local/tmp/live-favorites/outra"))
        a, b = self.parse(), self.parse(other)
        self.assertEqual(a.manifest_id, b.manifest_id)
        self.assertFalse(a.same_incarnation(b))

    def test_none_never_matches_anything(self):
        """Two unknowns are not evidence of sameness."""
        unknown = self.parse(self.LINE.replace("supervisor=1076-4242",
                                               "supervisor=none"))
        self.assertFalse(self.parse().same_incarnation(unknown))
        self.assertFalse(unknown.same_incarnation(unknown))

    def test_a_reply_without_the_protocol_is_refused(self):
        chopped = self.LINE.replace("protocol=handoff.ack 1 ", "")
        self.assertIsNone(self.parse(chopped))

    def test_unrelated_output_is_not_an_identity(self):
        self.assertIsNone(self.parse("nada aqui"))
        self.assertIsNone(self.parse(""))


class SupervisorReconciliationTests(unittest.TestCase):
    """Fase 2 §4: a path is a location, an incarnation is the identity.

    A generation recovered into an alternative folder supervises the same
    content from somewhere else, so the recorded location is reconciled by
    compare-and-swap — and only with full proof. A folder that merely contains
    the same manifest is never adopted.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        base = Path(self._dir.name)
        (base / "staging").mkdir()
        self.service = mgs.MixedGenerationService(
            server_url="http://127.0.0.1:8080",
            export_config_path=base / "export.json",
            staging_base=base / "staging",
            state_file=base / "state.json",
            now=lambda: 1000.0, poll_interval=0.0, publication_timeout=0.0)
        self.service.state.update({"active_session": "mixed-gen18-0ca1b784",
                                   "active_generation": 18,
                                   "active_manifest_id": "mid18"})
        self.service.transport = mgs.TabletTransport(runner=FakeDevice())

    @staticmethod
    def _identity(path="/data/local/tmp/live-favorites/gen18-recuperada",
                  incarnation="4242-999", manifest="mid18", lock_owned="1",
                  generation="18"):
        return mgs.SupervisorIdentity.parse(
            f"describe protocol=handoff.ack 1 boot_id=b1 generation={generation} "
            f"manifest_id={manifest} package_digest=p runtime_digest=r "
            f"supervisor={incarnation} candidate=5-5 lun=/lun/tv.img "
            f"lock_owned={lock_owned} path={path}")

    def _observation(self, session="gen18-recuperada", **over):
        identity = self._identity(**over)
        return {"answered": True, "reason": None, "identity": identity,
                "path": identity.path, "session": session,
                "lock_owner": identity.incarnation.split("-")[0]}

    def test_recovery_in_an_alternative_folder_reconciles_the_location(self):
        out = self.service.reconcile_location(
            self._observation(), generation=18, manifest_id="mid18")
        self.assertTrue(out["reconciled"])
        self.assertEqual(self.service.state["active_session"], "gen18-recuperada")
        self.assertEqual(self.service.state["session_reconciled_from"],
                         "mixed-gen18-0ca1b784")
        # The swap is written where every reader looks, and it survives a reload
        # of the journal file (an in-memory-only change would be lost).
        self.assertEqual(out["from"], "mixed-gen18-0ca1b784")
        again = self.service.reconcile_location(
            self._observation(), generation=18, manifest_id="mid18")
        self.assertTrue(again["reconciled"])
        self.assertEqual(again["reason"], "location_already_reconciled")

    def test_a_different_manifest_is_not_adopted_just_by_location(self):
        out = self.service.reconcile_location(
            self._observation(manifest="outro"), generation=18,
            manifest_id="mid18")
        self.assertFalse(out["reconciled"])
        self.assertEqual(out["reason"], "observed_manifest_differs")
        self.assertEqual(self.service.state["active_session"],
                         "mixed-gen18-0ca1b784")

    def test_a_supervisor_that_does_not_own_the_lock_is_not_adopted(self):
        out = self.service.reconcile_location(
            self._observation(lock_owned="0"), generation=18, manifest_id="mid18")
        self.assertFalse(out["reconciled"])
        self.assertEqual(out["reason"], "observed_supervisor_does_not_own_lock")

    def test_an_unproven_incarnation_is_never_reconciled(self):
        out = self.service.reconcile_location(
            self._observation(incarnation="none"), generation=18,
            manifest_id="mid18")
        self.assertFalse(out["reconciled"])
        self.assertEqual(out["reason"], "observed_incarnation_unproven")

    def test_identity_changed_between_query_and_action_blocks_the_takeover(self):
        """Reboot, a recycled pid, or another publisher winning the race."""
        first = self._identity()
        moved = self._identity(incarnation="4242-424242")
        self.service.observe_active_supervisor = lambda: {
            "answered": True, "reason": None, "identity": moved,
            "path": moved.path, "session": "gen18-recuperada",
            "lock_owner": "4242"}
        self.assertFalse(self.service.revalidate_supervisor(first))
        with self.assertRaises(mgs.PreviousSupervisorNotUpgradable) as ctx:
            self.service.handoff_takeover("sess-x", "b" * 32, "gen18-recuperada",
                                          observed=first)
        self.assertIn("changed before the takeover", str(ctx.exception))

    def test_an_unchanged_identity_revalidates(self):
        first = self._identity()
        self.service.observe_active_supervisor = lambda: self._observation()
        self.assertTrue(self.service.revalidate_supervisor(first))


class OperationTrackerCorrelationTests(unittest.TestCase):
    """Correlation is proved, not assumed: one tablet slot is not enough.

    The slot says *some* operation is active for the tablet; it never says that
    this task, this revision and this execution are the ones that reserved it.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.db = Path(self._dir.name) / "ops.db"
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        os.environ["MIXED_OPERATIONS_DB"] = str(self.db)
        os.environ.pop("MIXED_TABLET_ID", None)
        import mixed_operations
        self.store = mixed_operations.OperationStore(self.db)
        self.op_id, _ = self.store.begin(
            tablet_id=mgs._publisher_tablet_id(), principal="dashboard",
            intention={"task_id": "a1b2c3d4", "operation": "apply",
                       "revision": "7"},
            user_message="Atualização solicitada")

    def _tracker(self, *, revision=7, executor="hostA:10-100"):
        return mgs._OperationTracker(mgs._publisher_tablet_id(), "a1b2c3d4",
                                     revision=revision, executor=executor)

    def test_claim_binds_the_operation_to_the_execution(self):
        tracker = self._tracker()
        self.assertEqual(tracker.claim(), "owned")
        self.assertEqual(self.store.get(self.op_id)["executor"], "hostA:10-100")
        # The same execution re-claims its own row: a retry inside one run is not
        # a conflict.
        self.assertEqual(tracker.claim(), "owned")
        # A different execution must never adopt it.
        other = self._tracker(executor="hostB:20-200")
        self.assertEqual(other.claim(), "blocked")
        self.assertEqual(self.store.get(self.op_id)["executor"], "hostA:10-100")

    def test_a_different_revision_is_refused(self):
        self.assertEqual(self._tracker(revision=8).claim(), "blocked")

    def test_a_different_task_is_refused(self):
        tracker = mgs._OperationTracker(mgs._publisher_tablet_id(), "deadbeef",
                                        revision=7, executor="hostA:10-100")
        self.assertEqual(tracker.claim(), "blocked")

    def test_a_coordination_failure_after_the_effect_is_not_a_safe_failure(self):
        tracker = self._tracker()
        self.assertEqual(tracker.claim(), "owned")

        def boom(*_args, **_kw):
            raise RuntimeError("database is gone")

        tracker._store.finish = boom
        before = self._tracker()
        self.assertEqual(before.claim(), "owned")
        before._store.finish = boom
        # No effect yet: an unrecorded result is a clean failure.
        self.assertEqual(before.finish("failed_safe", "x"), "unrecorded")
        # After a possible effect, a clean failure would be a fabrication.
        tracker.mark_effect_started()
        self.assertEqual(tracker.finish("failed_safe", "x"),
                         "unrecorded_after_effect")
