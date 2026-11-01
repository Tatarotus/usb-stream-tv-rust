"""The container<->publisher bridge: real client, real daemon, real spool.

Everything here runs the actual scripts/mixed_bridge.py code in a temp spool
directory with a stub publisher, so the transport (atomic file handoff, owner
and mode proof, freshness, replay guard, argv-only execution) is exercised end
to end. No network, no adb, no ssh.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import mixed_bridge as bridge  # noqa: E402

TASK = "a1b2c3d4"


class BridgeCase(unittest.TestCase):
    def test_inventory_verification_is_read_only_and_has_no_task(self):
        request = bridge.build_request('verify_inventory')
        argv = bridge.publisher_argv(['publisher'], request)
        self.assertEqual(argv, ['publisher', '--verify-inventory', '--json'])
        with self.assertRaises(bridge.BridgeError):
            bridge.build_request('verify_inventory', TASK)
        with self.assertRaises(bridge.BridgeError):
            bridge.build_request('verify_inventory', confirm_interrupt=True)

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="bridge-test-")
        self.addCleanup(self._temp.cleanup)
        self.spool = Path(self._temp.name) / "spool"
        self.log = Path(self._temp.name) / "publisher.argv"
        # A stub publisher: records its argv and answers with a token-free
        # payload, exactly like mixed_generation_service.py --json.
        self.publisher = [sys.executable, "-c",
                          "import json,pathlib,sys\n"
                          f"pathlib.Path({str(self.log)!r}).write_text(' '.join(sys.argv[1:]))\n"
                          "print(json.dumps({'confirmed': True, 'state': 'active',"
                          " 'generation': 7, 'reason': None}))\n"]
        bridge._ensure_layout(self.spool)

    def run_daemon(self, *, client_uid=None, times=1, use_own_uid=True):
        argv = [sys.executable, str(ROOT / "scripts" / "mixed_bridge.py"), "serve",
                "--spool", str(self.spool), "--poll-interval", "0.01",
                "--client-uid", str(os.getuid() if use_own_uid else os.getuid() + 1)]
        if times == 1:
            argv.append("--once")
        else:
            argv += ["--max-requests", str(times)]
        if client_uid is not None:
            argv += ["--client-uid", str(client_uid)]
        argv += ["--publisher", *self.publisher]
        return subprocess.run(argv, capture_output=True, text=True, timeout=120)

    def run_client(self, action="apply", task_id=TASK, timeout=20.0):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "mixed_bridge.py"), "client",
             "--spool", str(self.spool), "--action", action,
             "--task-id", task_id, "--timeout", str(timeout)],
            capture_output=True, text=True, timeout=120)

    def write_request(self, payload, *, name=None, mode=0o600):
        name = name or payload.get("id", "0" * 32)
        path = self.spool / "requests" / f"{name}.json"
        path.write_text(json.dumps(payload))
        os.chmod(path, mode)
        return path

    def answer(self, *, now=None, client_uid=None):
        return bridge.process_one(
            self.spool, self.publisher,
            client_uid=os.getuid() if client_uid is None else client_uid,
            runner=self._runner, now=now)

    def _runner(self, argv, timeout):
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, check=False)


class HappyPathTests(BridgeCase):
    def test_publisher_timeout_is_separate_and_daemon_can_handle_next_request(self):
        request = bridge.build_request('apply', TASK)
        bridge.submit(self.spool, request)
        seen = []
        def timeout_runner(argv, timeout):
            seen.append(timeout)
            raise subprocess.TimeoutExpired(argv, timeout)
        self.assertEqual(bridge.process_one(self.spool, self.publisher,
            client_uid=os.getuid(), runner=timeout_runner), request['id'])
        response = json.loads((self.spool / 'responses' / (request['id'] + '.json')).read_text())
        self.assertEqual(response['state'], 'unknown')
        self.assertEqual(response['reason'], 'publisher_timeout_reconciliation_required')
        self.assertGreater(seen[0], bridge.FRESHNESS_SECONDS)
        another = bridge.build_request('verify', TASK)
        bridge.submit(self.spool, another)
        self.assertEqual(self.answer(), another['id'])
        self.assertTrue(json.loads((self.spool / 'responses' / (another['id'] + '.json')).read_text())['confirmed'])

    def test_confirmed_apply_round_trip_forwards_explicit_flag(self):
        client = subprocess.Popen(
            [sys.executable, str(ROOT / 'scripts' / 'mixed_bridge.py'), 'client',
             '--spool', str(self.spool), '--action', 'apply', '--task-id', TASK,
             '--confirm-interrupt', '--timeout', '60'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and self.answer() is None:
                time.sleep(0.05)
            out, err = client.communicate(timeout=60)
        finally:
            if client.poll() is None:
                client.kill()
        self.assertEqual(client.returncode, 0, err)
        self.assertIs(json.loads(out)['confirmed'], True)
        self.assertIn('--confirm-interrupt', self.log.read_text().split())

    def test_unconfirmed_apply_does_not_forward_flag(self):
        request = bridge.build_request('apply', TASK)
        bridge.submit(self.spool, request)
        self.assertEqual(self.run_daemon().returncode, 0)
        self.assertNotIn('--confirm-interrupt', self.log.read_text().split())

    def test_rejects_invalid_or_verify_interruption_confirmation(self):
        for value, action in [('true', 'apply'), (1, 'apply'), (True, 'verify')]:
            request = bridge.build_request(action, TASK)
            request['confirm_interrupt'] = value
            self.write_request(request)
            self.answer()
            self.assertFalse(self.log.exists())
        with self.assertRaises(bridge.BridgeError):
            bridge.build_request('verify', TASK, confirm_interrupt=True)

    def test_client_request_is_answered_by_the_daemon(self):
        request = bridge.build_request("apply", TASK)
        response = bridge.submit(self.spool, request)
        self.assertTrue(response.name.startswith(request["id"]))
        self.assertTrue((self.spool / "requests" / f"{request['id']}.json").is_file())

        completed = self.run_daemon()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        data, info = bridge._read_bounded(response, bridge.MAX_RESPONSE_BYTES)
        payload = json.loads(data)
        self.assertIs(payload["confirmed"], True)
        self.assertEqual(payload["generation"], 7)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        # The publisher was called as an argv list with the exact action.
        argv = self.log.read_text()
        self.assertIn("--apply", argv)
        self.assertIn(f"--task-id {TASK}", argv)
        self.assertIn("--json", argv)
        self.assertNotIn("--verify", argv)
        # The request was consumed and replay-guarded.
        self.assertFalse((self.spool / "requests" / f"{request['id']}.json").exists())
        self.assertFalse((self.spool / "processing" / f"{request['id']}.json").exists())
        self.assertTrue((self.spool / "done" / f"{request['id']}.json").is_file())

    def test_verify_action_is_forwarded_as_verify(self):
        request = bridge.build_request("verify", TASK)
        bridge.submit(self.spool, request)
        self.assertEqual(self.run_daemon().returncode, 0)
        argv = self.log.read_text()
        self.assertIn("--verify", argv)
        self.assertIn("--apply", argv)

    def test_full_client_to_daemon_round_trip(self):
        # The real client blocks on the answer, so the daemon runs concurrently.
        client = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "mixed_bridge.py"), "client",
             "--spool", str(self.spool), "--action", "apply", "--task-id", TASK,
             "--timeout", "60"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and self.answer() is None:
                time.sleep(0.05)
            out, err = client.communicate(timeout=60)
        finally:
            client.kill()
        self.assertEqual(client.returncode, 0, err)
        self.assertIs(json.loads(out)["confirmed"], True)
        # Nothing is left behind on either side.
        for name in ("requests", "processing", "responses"):
            self.assertEqual(list((self.spool / name).iterdir()), [])

    def test_repeated_requests_are_answered_in_order(self):
        ids = []
        for _ in range(3):
            request = bridge.build_request("apply", TASK)
            bridge.submit(self.spool, request)
            ids.append(request["id"])
        self.assertEqual(self.run_daemon(times=3).returncode, 0)
        answered = sorted(p.name for p in (self.spool / "responses").iterdir())
        self.assertEqual(answered, sorted(f"{i}.json" for i in ids))


class RejectionTests(BridgeCase):
    def _reject(self, payload, *, name=None, mode=0o600, now=None):
        self.write_request(payload, name=name, mode=mode)
        self.answer(now=now)
        files = sorted((self.spool / "responses").glob("*.json"))
        self.assertEqual(len(files), 1, [f.name for f in files])
        payload = json.loads(files[0].read_text())
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["state"], "rejected")
        # A rejected request never reaches the publisher.
        self.assertFalse(self.log.exists())
        return payload["reason"]

    def test_a_group_or_world_readable_request_is_refused(self):
        request = bridge.build_request("apply", TASK)
        reason = self._reject(request, mode=0o644)
        self.assertIn("not private", reason)

    def test_a_foreign_owner_is_refused(self):
        request = bridge.build_request("apply", TASK)
        self.write_request(request)
        # Pretend the configured client uid is somebody else's: the request is
        # refused even though its mode is perfect.
        answer = bridge.process_one(self.spool, self.publisher,
                                    client_uid=os.getuid() + 1, runner=self._runner)
        self.assertIsNotNone(answer)
        payload = json.loads(next((self.spool / "responses").glob("*.json")).read_text())
        self.assertIs(payload["ok"], False)
        self.assertIn("owner", payload["reason"])
        self.assertFalse(self.log.exists())

    def test_a_stale_or_future_request_is_refused(self):
        now = time.time()
        for issued in (int(now) - bridge.FRESHNESS_SECONDS - 60,
                       int(now) + bridge.FRESHNESS_SECONDS + 60):
            with self.subTest(issued=issued):
                for path in (self.spool / "responses").glob("*.json"):
                    path.unlink()
                request = bridge.build_request("apply", TASK, now=issued)
                request["issued_at"] = int(now) - 10 ** 6 if issued < now else int(now) + 10 ** 6
                self._reject(request)

    def test_an_unsupported_action_or_bad_task_id_is_refused(self):
        request = bridge.build_request("apply", TASK)
        for field, value in (("action", "exec"), ("action", "apply; rm -rf /"),
                             ("task_id", "NOT-A-TASK"), ("task_id", "../../etc/passwd"),
                             ("version", 99), ("id", "zz")):
            with self.subTest(field=field, value=value):
                for path in (self.spool / "responses").glob("*.json"):
                    path.unlink()
                for name in ("processing", "requests"):
                    for stale in (self.spool / name).glob("*.json"):
                        stale.unlink()
                payload = dict(request)
                payload["id"] = request["id"]
                payload[field] = value
                # The file name stays the valid minted id: a payload whose id
                # disagrees with its own file name is the rejection under test.
                self._reject(payload, name=request["id"])

    def test_a_replayed_request_is_refused(self):
        request = bridge.build_request("apply", TASK)
        response = self.spool / "responses" / f"{request['id']}.json"
        self.write_request(request)
        self.answer()
        first = json.loads(response.read_text())
        self.assertIs(first["ok"], True)
        self.assertIs(first["confirmed"], True)
        # The same id is replayed: the done marker blocks a second execution.
        self.write_request(request)
        self.answer()
        second = json.loads(response.read_text())
        self.assertIs(second["ok"], False)
        self.assertEqual(second["reason"], "replayed_request")
        self.assertEqual(self.log.read_text().count("--apply"), 1)

    def test_an_oversized_request_is_refused(self):
        request = bridge.build_request("apply", TASK)
        path = self.write_request(request)
        with path.open("a") as handle:
            handle.write(" " * (bridge.MAX_REQUEST_BYTES + 10))
        self.answer()
        payload = json.loads(next((self.spool / "responses").glob("*.json")).read_text())
        self.assertIs(payload["ok"], False)
        self.assertFalse(self.log.exists())

    def test_a_request_named_outside_the_id_shape_is_ignored(self):
        # Only a minted 32-hex id ever names a spool file, so a stray basename
        # must not produce an answer file or reach the publisher.
        path = self.spool / "requests" / "not-a-hex-id.json"
        path.write_text(json.dumps({"version": 1, "id": "not-a-hex-id",
                                    "action": "apply", "task_id": TASK,
                                    "issued_at": int(time.time())}))
        os.chmod(path, 0o600)
        self.assertIsNone(self.answer())
        self.assertEqual(list((self.spool / "responses").iterdir()), [])
        self.assertEqual(list((self.spool / "processing").iterdir()), [])
        self.assertFalse(self.log.exists())

    def test_a_symlinked_request_is_never_followed(self):
        request = bridge.build_request("apply", TASK)
        target = Path(self._temp.name) / "outside.json"
        target.write_text(json.dumps(request))
        os.chmod(target, 0o600)
        link = self.spool / "requests" / f"{request['id']}.json"
        link.symlink_to(target)
        self.assertIsNone(self.answer())            # ignored, never executed
        self.assertEqual(list((self.spool / "responses").iterdir()), [])
        self.assertFalse(self.log.exists())
        self.assertTrue(target.exists())            # and never consumed/removed


class ProtocolTests(BridgeCase):
    def test_build_request_validates_its_inputs(self):
        with self.assertRaises(bridge.BridgeError):
            bridge.build_request("exec", TASK)
        for bad in ("", "ABC", "a1b2c3d", "a1b2c3d4e", "../x", "a1b2c3d "):
            with self.subTest(task=bad):
                with self.assertRaises(bridge.BridgeError):
                    bridge.build_request("apply", bad)

    def test_no_credential_is_ever_written_into_the_spool(self):
        os.environ["TV_PIN"] = "p-1234-secret"
        try:
            self.addCleanup(os.environ.pop, "TV_PIN", None)
            request = bridge.build_request("apply", TASK)
            bridge.submit(self.spool, request)
            self.run_daemon()
        finally:
            os.environ.pop("TV_PIN", None)
        blob = ""
        for path in self.spool.rglob("*"):
            if path.is_file():
                blob += path.read_text(errors="replace")
        self.assertNotIn("p-1234-secret", blob)
        self.assertNotIn("token", blob.lower())


if __name__ == "__main__":
    unittest.main()
