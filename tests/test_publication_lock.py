"""Concurrency contract between the migrator, the bridge and the publisher.

The point of these tests is not "the spool looked empty": it is that a
publication can never be *running* while the migration window is open, including
when the request arrives exactly in the interval that used to lose the race.

Everything runs real processes (real ``mixed_bridge.py`` daemon, real stub
publisher, real ``publication_lock.py`` holder) inside a temporary directory. No
network, no adb, no ssh, no systemd.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import mixed_bridge as bridge  # noqa: E402
import publication_lock  # noqa: E402

TASK = "a1b2c3d4"
LOCK_PY = SCRIPTS / "publication_lock.py"
BRIDGE_PY = SCRIPTS / "mixed_bridge.py"


def publisher_source(marker: Path, events: Path, *, seconds: float,
                     lock: Path) -> str:
    """A publisher that behaves like the real one: shared lock + long effect."""
    return textwrap.dedent(
        f"""
        import os, sys, time, json, pathlib
        sys.path.insert(0, {str(SCRIPTS)!r})
        import publication_lock
        lock = publication_lock.acquire({str(lock)!r}, shared=True, kind="publisher")
        pid = os.getpid()
        with open({str(events)!r}, "a") as handle:
            handle.write("start %d\\n" % pid)
            handle.flush()
        time.sleep({seconds!r})
        with open({str(events)!r}, "a") as handle:
            handle.write("end %d\\n" % pid)
            handle.flush()
        {str(marker)!r} and pathlib.Path({str(marker)!r}).write_text("done")
        lock.release()
        print(json.dumps({{"confirmed": True, "state": "active",
                          "generation": 7, "reason": None}}))
        """
    )


class ConcurrencyCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="conc-lock-")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.spool = self.root / "spool"
        self.state = self.root / "state.json"
        self.lock = self.root / "publication.lock"
        self.events = self.root / "events.log"
        self.marker = self.root / "finished"
        self.publisher_script = self.root / "publisher.py"
        self.publisher_script.write_text(publisher_source(
            self.marker, self.events, seconds=1.5, lock=self.lock))
        self.state.write_text(json.dumps({"active_generation": 6}))
        bridge._ensure_layout(self.spool)
        publication_lock.ensure_file(self.lock, owner_of=self.state)
        self._procs = []
        self.addCleanup(self._reap)

    def _reap(self):
        for proc in self._procs:
            if proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGTERM)
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

    # ---------------------------------------------------------------- helpers
    def start_daemon(self, *, times=1):
        # ``--publisher`` uses argparse.REMAINDER, so everything after it belongs
        # to the publisher argv: the daemon's own flags must come FIRST.
        # ``--max-requests N`` (never ``--once``) is what makes the daemon poll
        # until it has actually served N requests: ``--once`` returns after a
        # single poll, which would hide exactly the behaviour under test.
        argv = [sys.executable, str(BRIDGE_PY), "serve",
                "--spool", str(self.spool), "--poll-interval", "0.02",
                "--client-uid", str(os.getuid()),
                "--max-requests", str(times),
                "--publisher", sys.executable, str(self.publisher_script),
                 "--state-file", str(self.state)]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        self._procs.append(proc)
        return proc

    def submit(self, action="apply", task_id=TASK):
        request = bridge.build_request(action, task_id)
        bridge.submit(self.spool, request, request_owner_uid=os.getuid())
        return request["id"]

    def start_window(self, *, timeout=5.0):
        ready = self.root / "window.ready"
        proc = subprocess.Popen(
            [sys.executable, str(LOCK_PY), "hold", "--lock", str(self.lock),
             "--exclusive", "--timeout", str(timeout), "--kind", "migration",
             "--ready-file", str(ready)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._procs.append(proc)
        for _ in range(400):
            if ready.exists() and ready.read_text().strip():
                return proc, ready.read_text().strip()
            if proc.poll() is not None:
                return proc, ""
            time.sleep(0.02)
        return proc, ""

    def events_now(self):
        if not self.events.exists():
            return []
        return [line.split()[0] for line in self.events.read_text().splitlines() if line]

    def wait_for(self, predicate, timeout=10.0, interval=0.02):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    def running_publishers(self):
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                args = (entry / "cmdline").read_bytes().split(b"\0")
            except (OSError, PermissionError):
                continue
            if any(str(self.publisher_script).encode() in token for token in args[:3]):
                found.append(entry.name)
        return found

    # ------------------------------------------------------------------ tests
    def test_a_served_claim_releases_the_shared_lock_while_the_daemon_lives(self):
        """A leaked SH after a claim would block every future migration window.

        The bridge holds SHARED from the claim until its publisher exits, so the
        release must happen on the success path too - not only when the rename
        fails. Otherwise the first served request would leave the lock held for
        the lifetime of the daemon and no migration could ever start.
        """
        self.submit()
        daemon = self.start_daemon(times=2)   # stays alive after serving one
        self.assertTrue(self.wait_for(lambda: self.marker.exists(), timeout=20),
                        "the first request was not served")
        self.assertTrue(
            self.wait_for(lambda: publication_lock.probe(self.lock)[0] == "free",
                          timeout=20),
            f"the bridge kept the lock after serving: {publication_lock.probe(self.lock)}")
        self.assertIsNone(daemon.poll(), "the daemon should still be alive")
        # With the daemon alive and idle, a migration window must be available.
        window, ready = self.start_window(timeout=2.0)
        self.assertTrue(ready.startswith("locked"), ready)
        window.send_signal(signal.SIGTERM)
        window.wait(timeout=10)
        daemon.send_signal(signal.SIGTERM)
        daemon.wait(timeout=10)

    def test_window_open_means_no_claim_and_no_publisher(self):
        """A request queued during the window waits; nothing is started."""
        request_id = self.submit()
        window, ready = self.start_window()
        self.assertTrue(ready.startswith("locked"), ready)
        daemon = self.start_daemon(times=1)
        # The daemon must stay alive and must NOT start the publisher.
        time.sleep(1.0)
        self.assertIsNone(daemon.poll(), "the daemon exited during the window")
        self.assertEqual(self.events_now(), [], "a publisher ran inside the window")
        # The request is still exactly where the client left it.
        self.assertTrue((self.spool / "requests" / f"{request_id}.json").exists())
        self.assertFalse((self.spool / "responses" / f"{request_id}.json").exists())
        # Closing the window releases the queue: the same daemon now serves it.
        window.send_signal(signal.SIGTERM)
        window.wait(timeout=10)
        self.assertTrue(self.wait_for(lambda: self.marker.exists(), timeout=15),
                        "the request was not served after the window closed")
        self.assertIn("start", self.events_now())
        # The daemon must only exit because it served its one request, never
        # blocked by the lock. Waiting for its exit makes every effect below
        # observable, instead of racing the response write by microseconds.
        daemon.wait(timeout=25)
        self.assertEqual(daemon.returncode, 0,
                         f"daemon did not finish cleanly: {daemon.returncode}")

    def test_request_arriving_inside_the_interval_is_never_killed(self):
        """The exact race: request lands between "window taken" and "bridge stopped".

        The publisher must run to completion; the migrator's window must never
        overlap it. Here the migrator is represented by the window holder and the
        bridge by the daemon: the assertion is that the publisher that started is
        still alive and finishing when the window closes, i.e. it was never
        interrupted.
        """
        window, ready = self.start_window()
        self.assertTrue(ready.startswith("locked"), ready)
        # Request arrives AFTER the window is open (the old TOCTOU interval).
        request_id = self.submit()
        daemon = self.start_daemon(times=1)
        time.sleep(0.6)
        self.assertEqual(self.events_now(), [])
        self.assertIsNone(daemon.poll())
        window.send_signal(signal.SIGTERM)
        window.wait(timeout=10)
        # Wait for the daemon to finish the publication: it exits only after the
        # response is written, so nothing below races a microsecond window.
        daemon.wait(timeout=30)
        self.assertEqual(daemon.returncode, 0,
                         f"daemon did not finish cleanly: {daemon.returncode}")
        events = self.events_now()
        self.assertIn("start", events)
        self.assertIn("end", events, f"publisher interrupted: events={events}")
        self.assertTrue(self.marker.exists(), "the publisher did not complete")
        answer = json.loads((self.spool / "responses" / f"{request_id}.json").read_text())
        self.assertTrue(answer.get("ok"))

    def test_window_is_refused_while_a_publication_is_running(self):
        """A running publisher holds SHARED: the migrator must be refused."""
        # Start a publication through the daemon and wait until it holds.
        daemon = self.start_daemon(times=1)
        self.submit()
        self.assertTrue(self.wait_for(lambda: "start" in self.events_now(), timeout=15),
                        "the publisher never started")
        window, ready = self.start_window(timeout=0.2)
        self.assertTrue(ready.startswith("busy"), ready)
        self.assertIn("publisher", ready)
        # The publication is untouched: it reaches its own end.
        self.assertTrue(self.wait_for(lambda: "end" in self.events_now(), timeout=20),
                        "the publisher was interrupted by the refused window")
        daemon.wait(timeout=20)
        # The claim holder released the lock, so the daemon could finish too.
        self.assertEqual(daemon.returncode, 0, daemon.stderr.read()[:400])

    def test_foreign_publisher_with_its_own_state_file_is_detected(self):
        """A publisher that resolved ANOTHER lock path must still block the window.

        This is the case the shared file cannot see: the process scan is the
        backstop, and it must find the publisher without signalling it.
        """
        other_state = self.root / "elsewhere" / "state.json"
        other_state.parent.mkdir()
        other_script = self.root / "publisher.py"
        proc = subprocess.Popen(
            [sys.executable, str(other_script), "--apply", "--task-id", TASK,
             "--state-file", str(other_state), "--json"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._procs.append(proc)
        self.assertTrue(self.wait_for(lambda: str(proc.pid) in self.running_publishers(),
                                      timeout=10),
                        "the scan did not see the publisher")
        # The scan is read-only: the process is still alive after it.
        self.assertIsNone(proc.poll(), "the process scan signalled the publisher")

    def test_crashed_holder_leaves_no_stale_window(self):
        """The kernel drops the lock: no heartbeat, no PID trust."""
        window, ready = self.start_window()
        self.assertTrue(ready.startswith("locked"), ready)
        window.kill()
        window.wait(timeout=10)
        state, _ = publication_lock.probe(self.lock)
        self.assertEqual(state, "free")
        # And a new window can be taken immediately.
        again, ready2 = self.start_window()
        self.assertTrue(ready2.startswith("locked"), ready2)
        again.send_signal(signal.SIGTERM)
        again.wait(timeout=10)

    def test_lock_file_is_never_removed_while_used(self):
        """A new inode would let two holders both be exclusive."""
        window, ready = self.start_window()
        self.assertTrue(ready.startswith("locked"), ready)
        inode = self.lock.stat().st_ino
        window.send_signal(signal.SIGTERM)
        window.wait(timeout=10)
        self.assertEqual(self.lock.stat().st_ino, inode)
        self.assertEqual(oct(self.lock.stat().st_mode & 0o777), "0o666")


class ResolveTests(unittest.TestCase):
    """The three users must land on the same file, or the migrator must refuse."""

    def test_explicit_override_wins_everywhere(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state.json"
            spool = Path(temp) / "spool"
            explicit = "/run/somewhere/publication.lock"
            self.assertEqual(
                str(publication_lock.resolve_lock(explicit=explicit, state_file=state,
                                                  spool=spool)), explicit)
            self.assertEqual(
                str(publication_lock.resolve_lock_for_publisher(explicit=explicit,
                                                                state_file=state)),
                explicit)

    def test_state_file_directory_is_the_default(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "vod" / "state.json"
            self.assertEqual(publication_lock.resolve_lock(state_file=state),
                             state.parent / publication_lock.LOCK_NAME)
            self.assertEqual(publication_lock.resolve_lock_for_publisher(state_file=state),
                             state.parent / publication_lock.LOCK_NAME)

    def test_spool_fallback_is_visible_to_the_bridge_only(self):
        with tempfile.TemporaryDirectory() as temp:
            spool = Path(temp) / "spool"
            self.assertEqual(publication_lock.resolve_lock(spool=spool),
                             spool / publication_lock.LOCK_NAME)
            # The publisher has no spool: it cannot resolve the same file, which
            # is exactly why the migrator refuses in that configuration.
            self.assertIsNone(publication_lock.resolve_lock_for_publisher())


class BridgeWiringTests(unittest.TestCase):
    def test_state_file_is_read_from_the_publisher_argv(self):
        argv = ["python3", "/app/x.py", "--apply", "--state-file", "/app/state.json",
                "--json"]
        self.assertEqual(bridge.state_file_from_publisher(argv), "/app/state.json")
        self.assertEqual(
            bridge.state_file_from_publisher(["p", "--state-file=/s.json"]), "/s.json")
        self.assertEqual(bridge.state_file_from_publisher(["p", "--apply"]), "")


if __name__ == "__main__":
    unittest.main()
