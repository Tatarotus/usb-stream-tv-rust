#!/usr/bin/env python3
"""Host → tablet, end to end, with the REAL shell script.

Why this file exists
====================

The previous rounds proved the store API and the shell actions separately. The
auditor showed the gap that lets through: the host built an argv the shell could
never accept (session duplicated, identity sent as `<pid>-<starttime>` when the
shell requires the full five-part token), so the positive handoff path was
impossible while every isolated test stayed green.

Here the argv is produced by the real ``MixedGenerationService`` and executed by
the real ``activate_live_favorites_tablet.sh``. Only the ADB transport boundary
is replaced, because there is no tablet: everything above and below it is the
production code.

Nothing here touches adb, ssh, the container, the tablet or the USB bus. The
fake supervisor is a shell process created by this test and killed by it.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from subprocess import CompletedProcess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import mixed_generation_service as mgs  # noqa: E402

SCRIPT = ROOT / "scripts" / "activate_live_favorites_tablet.sh"
# The script calls applets through ``$BB``. Android ships busybox; a workstation
# does not, so the sandbox provides a shim that dispatches ``busybox <applet>``
# to the real binary. The script under test is unmodified.
BUSYBOX_SHIM = """#!/bin/sh
cmd=$1
shift
exec "$cmd" "$@"
"""

FAKE_SUPERVISOR = """#!/bin/bash
set -u
_term() {
    if [ -f "$A/activation/takeover.request" ]; then
        read -r _t _p _i < "$A/activation/takeover.request" || true
        printf '%s %s\\n' "$_t" "$_i" > "$A/activation/handoff.ack.tmp"
        mv -f "$A/activation/handoff.ack.tmp" "$A/activation/handoff.ack"
    fi
    rm -rf "$L"
    exit 0
}
trap _term TERM
while :; do sleep 0.1; done
"""


class LocalShellTransport(mgs.TabletTransport):
    """The production transport with ADB replaced by a local shell run.

    ``root_shell`` in production wraps the command in ``adb shell su -c``. Here
    the same command string is executed by ``/bin/sh -c`` with the sandbox
    ``BASE``/``USB``/``BB``, so the script under test is the real one.
    """

    def __init__(self, *, base, usb, bb, path):
        self.base = base
        self.usb = usb
        self.bb = bb
        self.path = path
        self.commands = []

    def root_shell(self, command, check=True, timeout=None):
        self.commands.append(command)
        environment = dict(os.environ)
        environment.update({"BB": self.bb, "BASE": self.base, "USB": self.usb,
                            "PATH": self.path})
        completed = subprocess.run(["/bin/sh", "-c", command], text=True,
                                   capture_output=True, timeout=timeout,
                                   env=environment)
        if check and completed.returncode != 0:
            raise mgs.TabletCommandError(completed.returncode,
                                         completed.stderr or completed.stdout)
        return CompletedProcess([command], completed.returncode,
                                completed.stdout, completed.stderr)


class HostToTabletHandoffTests(unittest.TestCase):
    """The handoff the publisher actually performs, against the real script."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="host-tablet-")
        self.addCleanup(self._dir.cleanup)
        root = Path(self._dir.name)
        self.base = str(root / "base")
        self.usb = str(root / "usb")
        (root / "usb" / "f_mass_storage" / "lun0").mkdir(parents=True)
        self.new_session = "mixed-gen19-aaaa"
        self.prev_session = "mixed-gen18-bbbb"
        self.prev = Path(self.base) / "live-favorites" / self.prev_session
        (self.prev / "activation").mkdir(parents=True)
        (self.prev / "mnt").mkdir(parents=True)
        # A package the shell can describe: manifest, sums and a runtime.
        (self.prev / "favorites_manifest.json").write_text(json.dumps({
            "generation": 18, "manifest_id": "b9d04e0a" * 8}))
        # A supervisor that understands the handoff announces it; the host reads
        # that announcement from the RUNNING revision, never from a file path.
        (self.prev / "activation" / "handoff.protocol").write_text("handoff.ack 1\n")
        (self.prev / "SHA256SUMS").write_text(
            hashlib.sha256(b"prev-package").hexdigest() + "  inode\n")
        (self.prev / "activate_live_favorites_tablet.sh").write_text(
            SCRIPT.read_text())
        # The NEW package is transferred before the handoff, and the host runs
        # ITS script: that is the revision being published.
        self.new = Path(self.base) / "live-favorites" / self.new_session
        (self.new / "activation").mkdir(parents=True)
        (self.new / "activate_live_favorites_tablet.sh").write_text(
            SCRIPT.read_text())
        (self.new / "favorites_manifest.json").write_text(json.dumps({
            "generation": 19, "manifest_id": "b9d04e0b" * 8}))
        (self.prev / "favorites_fuse").write_bytes(b"\x7fELF")
        self.supervisor = None
        self.lock = Path(self.base) / "mode_switch.lock"
        self.bin = root / "bin"
        self.bin.mkdir()
        self.bb = self.bin / "busybox"
        self.bb.write_text(BUSYBOX_SHIM)
        self.bb.chmod(0o755)

    def tearDown(self):
        if self.supervisor is not None and self.supervisor.poll() is None:
            self.supervisor.kill()
            self.supervisor.wait(timeout=10)

    # -- fixtures ---------------------------------------------------------
    def start_supervisor(self):
        """A live process whose cmdline names the package script it supervises."""
        helper = self.prev / "fake_supervisor.sh"
        helper.write_text(FAKE_SUPERVISOR)
        helper.chmod(0o755)
        environment = dict(os.environ)
        environment.update({"A": str(self.prev), "L": str(self.lock)})
        # A real supervisor's cmdline names the script it supervises. A shell
        # script cannot keep a custom argv[0] (the kernel re-executes the
        # interpreter), so the path is passed as an argument instead: discovery
        # accepts any token that names the package script.
        self.supervisor = subprocess.Popen(
            ["/bin/bash", str(helper),
             "%s/activate_live_favorites_tablet.sh" % self.prev],
            env=environment, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        # The supervisor records itself exactly like the production path does.
        (self.prev / "activation" / "supervisor.pid").write_text(
            str(self.supervisor.pid))
        (self.prev / "activation" / "instance.id").write_text(
            hashlib.sha256(b"instance-%d" % self.supervisor.pid).hexdigest()[:32])
        self.lock.mkdir(parents=True, exist_ok=True)
        (self.lock / "pid").write_text(str(self.supervisor.pid))
        time.sleep(0.3)

    def service(self):
        service = mgs.MixedGenerationService.__new__(mgs.MixedGenerationService)
        service.transport = LocalShellTransport(
            base=self.base, usb=self.usb, bb=str(self.bb),
            path="%s:%s" % (self.bin, os.environ.get("PATH", "")))
        service.guard_ttl = 60
        return service

    def token(self):
        service = self.service()
        observation = service.observe_active_supervisor()
        self.assertTrue(observation["answered"], observation)
        return observation["identity"].identity_token

    # -- proofs -----------------------------------------------------------
    def test_the_host_observes_the_running_supervisor_by_its_full_identity(self):
        self.start_supervisor()
        service = self.service()
        observation = service.observe_active_supervisor()
        self.assertTrue(observation["answered"], observation)
        identity = observation["identity"]
        self.assertEqual(observation["session"], self.prev_session)
        self.assertTrue(identity.lock_owned)
        self.assertTrue(identity.identity_is_complete(),
                        "identity must carry all five components: %s"
                        % identity.identity_token)
        # The token the host will send is the one the tablet itself computes.
        components = identity.identity_token.split("|")
        self.assertEqual(len(components), 5)
        self.assertEqual(components[2], identity.incarnation)

    def test_the_host_handoff_lease_is_accepted_by_the_real_script(self):
        """The bug the auditor found: this call could NEVER be accepted."""
        self.start_supervisor()
        service = self.service()
        token = self.token()
        # No exception: the real script answered "handoff-lease token=...".
        service.handoff_lease(self.new_session, "a" * 32,
                              previous_session=self.prev_session,
                              observed_incarnation=token)
        lease = Path(self.base) / "mixed_handoff.lease"
        self.assertTrue(lease.is_file())
        self.assertEqual(oct(lease.stat().st_mode & 0o777), "0o600")

    def test_the_host_takeover_of_the_observed_instance_succeeds_with_its_ack(self):
        self.start_supervisor()
        service = self.service()
        observation = service.observe_active_supervisor()
        identity = observation["identity"]
        service.handoff_lease(self.new_session, "c" * 32,
                              previous_session=self.prev_session,
                              observed_incarnation=identity.identity_token)
        self.assertTrue(service.handoff_takeover(
            self.new_session, "c" * 32, self.prev_session, observed=identity))
        # The supervisor acknowledged and released the lock; nothing else ran.
        self.assertTrue((self.prev / "activation" / "handoff.ack").is_file())
        self.assertFalse(self.lock.exists())

    def test_a_token_from_a_previous_boot_is_refused_by_the_real_script(self):
        """Negative case, same entry point: no signal, explicit refusal."""
        self.start_supervisor()
        service = self.service()
        observation = service.observe_active_supervisor()
        identity = observation["identity"]
        stale = "btime:1|" + "|".join(identity.identity_token.split("|")[1:])
        with self.assertRaises(mgs.PreviousSupervisorTakeoverRefused):
            service.handoff_takeover(self.new_session, "d" * 32,
                                     self.prev_session, observed=identity)
        # The refusal above must have come from the boot component, so now prove
        # the stale token specifically is refused while the owner stays alive.
        with self.assertRaises(mgs.PublicationError):
            service.handoff_lease(self.new_session, "e" * 32,
                                 previous_session=self.prev_session,
                                 observed_incarnation=stale)
        self.assertTrue(self.lock.exists())
        self.assertIsNone(self.supervisor.poll())


if __name__ == "__main__":
    unittest.main()