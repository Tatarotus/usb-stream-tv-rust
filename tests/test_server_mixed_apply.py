"""Manual mixed-generation update: the existing "NA TV" button applies the
virtual pendrive list (server endpoints + idempotence + guard respect).

The click is the authorisation: the server launches the publisher in the
background (list form, never a shell string) and reports the journalled state.
A repeated click while the same task is in flight is idempotent.
"""

import http.client
import os
import socket
import subprocess
import json
import sys
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from subprocess import CompletedProcess
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import server
import mixed_generation_service as mgs
from scripts import mixed_operations as mo

PIN = "mixed-apply-test-pin"
TASK_ID = "a1b2c3d4"
OTHER_TASK = "b2c3d4e5"


class QuietHandler(server.RequestHandler):
    def log_message(self, *args):
        pass


class TestMixedApply(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mixed-apply-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / f"{TASK_ID}.mp4"
        self.media.write_bytes(b"\x00" * 1024)
        self.state_file = self.root / "state.json"
        self.apply_log = self.root / "apply.log"

        # A stub publisher: records its argv and exits immediately.
        self.stub = self.root / "stub_apply.py"
        self.stub.write_text(
            "import json, pathlib, sys\n"
            f"pathlib.Path({str(self.apply_log)!r}).write_text(' '.join(sys.argv))\n"
            "if '--verify' in sys.argv:\n"
            "    print(json.dumps({'confirmed': True, 'state': 'confirmed',\n"
            "                      'generation': 2, 'reason': 'tablet_active_generation_matches'}))\n"
        )

        self.tasks = {
            TASK_ID: {"id": TASK_ID, "title": "Film", "display_name": "Film.mp4",
                      "status": "ready", "storage": "local",
                      "file_path": str(self.media),
                      "file_size": self.media.stat().st_size},
            OTHER_TASK: {"id": OTHER_TASK, "title": "Other", "display_name": "Other.mp4",
                         "status": "queued", "storage": "local"},
        }
        self.guard = server.ModeGuard()
        self.popen = mock.Mock(return_value=mock.Mock(pid=4242))
        replacements = {
            "AUTH_PIN": PIN,
            "MODE_GUARD": self.guard,
            "VOD_TASKS": self.tasks,
            "VOD_DIR": str(self.root),
            "log_event": mock.Mock(),
            "subprocess": mock.Mock(Popen=self.popen),
        }
        for name, value in replacements.items():
            patcher = mock.patch.object(server, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.env = mock.patch.dict("os.environ", {
            "MIXED_APPLY_CMD": f"{sys.executable} {self.stub}",
            "MIXED_STATE_FILE": str(self.state_file),
            "MIXED_APP_DIR": str(self.root),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

        self.httpd = server.ThreadedHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_http)

    def _stop_http(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, payload=None, pin=True):
        headers = {"Connection": "close"}
        if pin:
            headers["X-Auth-PIN"] = PIN
        body = None
        if payload is not None:
            body = json.dumps(payload)
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            conn.close()

    def write_state(self, **state):
        self.state_file.write_text(json.dumps(state))
        self.state_file.chmod(0o600)

    # --- status ------------------------------------------------------------

    def test_status_reports_unavailable_without_a_publisher(self):
        with mock.patch.dict("os.environ", {"MIXED_APPLY_CMD": ""}, clear=False):
            with mock.patch.object(server, "_mixed_apply_command", return_value=None):
                status, body = self.request("GET", "/api/mixed/status")
        self.assertEqual(status, 200)
        self.assertFalse(body["available"])
        self.assertEqual(body["state"], "unavailable")

    def test_status_derives_selected_in_active_from_the_recorded_index(self):
        self.write_state(active_generation=2, active_session="mixed-gen2-aaaaaaaa",
                         active_index={TASK_ID: {"size": 1024, "revision": "md5:" + "a" * 32}},
                         publication={"phase": "active"})
        status, body = self.request("GET", f"/api/mixed/status?task_id={TASK_ID}")
        self.assertEqual(status, 200)
        self.assertTrue(body["available"])
        self.assertTrue(body["selected_in_active"])
        self.assertEqual(body["generation"], 2)
        status, body = self.request("GET", f"/api/mixed/status?task_id={OTHER_TASK}")
        self.assertFalse(body["selected_in_active"])
        # Never leaks a token, a PIN or a filesystem path.
        raw = json.dumps(body).lower()
        self.assertNotIn("token", raw)
        self.assertNotIn(PIN.lower(), raw)
        self.assertNotIn(str(self.root).lower(), raw)

    def test_pending_generation_supersedes_old_success_even_if_item_exists(self):
        self.write_state(active_generation=5, pending_generation=6,
                         active_index={TASK_ID: {"size": 1024, "revision": "md5:" + "a" * 32}},
                         apply={"task_id": TASK_ID, "state": "active", "generation": 5,
                                "message": "Lista atualizada", "reconnected": True},
                         publication={"phase": "prepared", "generation": 6})
        status, body = self.request("GET", f"/api/mixed/status?task_id={TASK_ID}")
        self.assertEqual(status, 200)
        self.assertTrue(body["selected_in_active"])
        self.assertEqual(body["pending_generation"], 6)
        self.assertEqual(body["state"], "prepared")
        self.assertIsNone(body["reconnected"])

    # --- apply -------------------------------------------------------------

    def test_confirm_without_task_checks_whole_inventory(self):
        payload = json.dumps({"confirmed": False, "state": "list_mismatch", "reason": "inventory_changed"})
        with mock.patch.object(server.subprocess, "run", return_value=CompletedProcess([], 0, payload, "")) as run:
            status, body = self.request("GET", "/api/mixed/confirm")
        self.assertEqual(status, 200)
        self.assertIs(body["confirmed"], False)
        self.assertEqual(body["reason"], "inventory_changed")
        self.assertIn("--verify-inventory", run.call_args.args[0])
        self.assertNotIn("--apply", run.call_args.args[0])

    def test_confirm_requires_pin_and_relays_the_publisher_proof(self):
        # An unauthenticated caller must not be able to spawn the probe.
        status, body = self.request("GET", f"/api/mixed/confirm?task_id={TASK_ID}", pin=False)
        self.assertEqual(status, 401)

        status, body = self.request("GET", "/api/mixed/confirm?task_id=nope")
        self.assertEqual(status, 400)

        payload = json.dumps({"confirmed": True, "state": "confirmed", "generation": 2,
                              "reason": "tablet_active_generation_matches"})
        with mock.patch.object(server.subprocess, "run",
                               return_value=CompletedProcess([], 0, payload, "")) as run:
            status, body = self.request("GET", f"/api/mixed/confirm?task_id={TASK_ID}")
        self.assertEqual(status, 200)
        self.assertTrue(body["available"])
        self.assertIs(body["confirmed"], True)
        self.assertEqual(body["generation"], 2)
        argv = run.call_args.args[0]
        self.assertIn("--verify", argv)
        self.assertIn("--apply", argv)
        # Read-only probe: it never launches a background publication.
        self.assertNotIn("--background", argv)
        self.assertEqual(argv[:2], [sys.executable, str(self.stub)])
        # The token-free shape never leaks the PIN or a path.
        raw = json.dumps(body).lower()
        self.assertNotIn(PIN.lower(), raw)
        self.assertNotIn(str(self.root).lower(), raw)

        # An unparseable answer is "unknown", never success.
        with mock.patch.object(server.subprocess, "run",
                               return_value=CompletedProcess([], 0, "not json", "")):
            status, body = self.request("GET", f"/api/mixed/confirm?task_id={TASK_ID}")
        self.assertEqual(status, 200)
        self.assertIsNone(body["confirmed"])
        self.assertEqual(body["reason"], "unparseable_confirmation")

    def test_apply_requires_pin_and_valid_ready_task(self):
        status, body = self.request("POST", "/api/mixed/apply", {"task_id": TASK_ID}, pin=False)
        self.assertEqual(status, 401)
        status, body = self.request("POST", "/api/mixed/apply", {"task_id": "nope"})
        self.assertEqual(status, 400)
        status, body = self.request("POST", "/api/mixed/apply", {"task_id": "ffffffff"})
        self.assertEqual(status, 404)
        status, body = self.request("POST", "/api/mixed/apply", {"task_id": OTHER_TASK})
        self.assertEqual(status, 409)
        self.popen.assert_not_called()

    def test_apply_launches_the_publisher_with_a_list_form_command(self):
        status, body = self.request("POST", "/api/mixed/apply", {"task_id": TASK_ID})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["available"])
        self.assertEqual(body["state"], "started")
        self.popen.assert_called_once()
        argv = self.popen.call_args.args[0]
        self.assertIsInstance(argv, list)
        self.assertIn("--apply", argv)
        self.assertIn(TASK_ID, argv)
        self.assertIn("--background", argv)
        # Never a shell string: the publisher is launched as an argv list.
        self.assertIn(self.popen.call_args.kwargs.get("shell"), (None, False))
        self.assertNotIsInstance(argv, str)
        self.assertEqual(argv[:2], [sys.executable, str(self.stub)])

    def test_apply_without_a_store_keeps_the_legacy_response(self):
        """Migration guarantee: with no store configured nothing changes.

        The persistent operation model is additive. If the endpoint started
        depending on it, an unset or broken store could block a publication —
        which is exactly what must not happen.
        """
        with mock.patch.dict("os.environ", {"MIXED_OPERATIONS_DB": ""}, clear=False):
            status, body = self.request("POST", "/api/mixed/apply",
                                        {"task_id": TASK_ID})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["state"], "started")
        self.assertNotIn("operation_id", body)
        self.popen.assert_called_once()

    def test_apply_with_a_store_adds_a_persistent_operation_id(self):
        """With the store on, the panel gets a durable id to follow."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            with mock.patch.dict("os.environ", {"MIXED_OPERATIONS_DB": db},
                                 clear=False):
                patcher = mock.patch.object(server, "_mixed_ops_store", None,
                                            create=True)
                patcher.start()
                self.addCleanup(patcher.stop)
                status, body = self.request("POST", "/api/mixed/apply",
                                            {"task_id": TASK_ID})
            self.assertEqual(status, 200)
            operation_id = body.get("operation_id")
            self.assertIsNotNone(operation_id)
            # The id was persisted before the launch and is readable.
            from scripts.mixed_operations import OperationStore
            row = OperationStore(db).get(operation_id)
            self.assertEqual(row["tablet_id"], "tablet")
            self.assertIn(row["state"], ("preparing", "queued"))

    def test_apply_records_a_failed_safe_operation_when_the_launch_fails(self):
        """A launch that never happened must not read as progress."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            with mock.patch.dict("os.environ", {"MIXED_OPERATIONS_DB": db},
                                 clear=False):
                patcher = mock.patch.object(server, "_mixed_ops_store", None,
                                            create=True)
                patcher.start()
                self.addCleanup(patcher.stop)
                self.popen.side_effect = OSError("boom")
                status, body = self.request("POST", "/api/mixed/apply",
                                            {"task_id": TASK_ID})
            self.assertEqual(status, 503)
            self.assertFalse(body["ok"])
            from scripts.mixed_operations import OperationStore
            store = OperationStore(db)
            active = store.active_for_tablet("tablet")
            self.assertIsNone(active)

    def test_duplicate_click_does_not_reset_the_phase_of_a_running_operation(self):
        """The `if created` guard: a repeat must not rewind a live operation.

        Without it, a second POST with the same idempotency key would push the
        phase back to preparing/launch while the publisher is already further
        along, making the panel report progress that went backwards.
        """
        import tempfile
        from pathlib import Path
        from scripts.mixed_operations import OperationStore
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            with mock.patch.dict("os.environ", {"MIXED_OPERATIONS_DB": db},
                                 clear=False):
                patcher = mock.patch.object(server, "_mixed_ops_store", None,
                                            create=True)
                patcher.start()
                self.addCleanup(patcher.stop)
                status, first = self.request("POST", "/api/mixed/apply",
                                             {"task_id": TASK_ID,
                                              "idempotency_key": "k-dup"})
                self.assertEqual(status, 200)
                op_id = first["operation_id"]
                # The publisher moves the operation forward.
                store = OperationStore(db)
                store.set_phase(op_id, state="transferring")
                # A duplicate click arrives.
                status2, second = self.request("POST", "/api/mixed/apply",
                                               {"task_id": TASK_ID,
                                                "idempotency_key": "k-dup"})
            self.assertEqual(status2, 200)
            self.assertEqual(second["operation_id"], op_id)
            # The phase must NOT have been rewound to preparing.
            self.assertEqual(OperationStore(db).get(op_id)["state"], "transferring")

    def test_a_duplicate_apply_does_not_relaunch_the_publisher(self):
        """Same key + same intention = the operation that already exists.

        Relaunching would start a SECOND publication for the same target; the
        panel would follow an id whose work was done twice.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            self._with_store(db)
            status, first = self.request("POST", "/api/mixed/apply",
                                         {"task_id": TASK_ID,
                                          "idempotency_key": "k-once"})
            self.assertEqual(status, 200)
            calls_after_first = self.popen.call_count
            self.assertEqual(calls_after_first, 1)
            status, second = self.request("POST", "/api/mixed/apply",
                                          {"task_id": TASK_ID,
                                           "idempotency_key": "k-once"})
            self.assertEqual(status, 200)
            self.assertEqual(second["operation_id"], first["operation_id"])
            self.assertTrue(second.get("duplicate"))
            self.assertEqual(self.popen.call_count, calls_after_first)

    def test_apply_is_refused_with_409_when_the_tablet_slot_is_taken(self):
        """With a store configured, coordination fails CLOSED: no launch."""
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            self._with_store(db)
            from scripts.mixed_operations import OperationStore
            holder, _ = OperationStore(db).begin(
                tablet_id="tablet", principal="dashboard",
                intention={"task_id": OTHER_TASK, "operation": "apply"},
                idempotency_key="holder-key", user_message="Atualização solicitada")
            status, body = self.request("POST", "/api/mixed/apply",
                                        {"task_id": TASK_ID,
                                         "idempotency_key": "mine-key"})
            self.assertEqual(status, 409)
            self.assertEqual(body["active_operation_id"], holder)
            self.popen.assert_not_called()

    def test_apply_is_refused_with_409_when_the_key_means_something_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            self._with_store(db)
            first = self.request("POST", "/api/mixed/apply",
                                 {"task_id": TASK_ID,
                                  "idempotency_key": "k-intent"})[1]
            self.popen.reset_mock()
            status, body = self.request("POST", "/api/mixed/apply",
                                        {"task_id": TASK_ID,
                                         "idempotency_key": "k-intent",
                                         "confirm_interrupt": True})
            self.assertEqual(status, 409)
            self.assertEqual(body["operation_id"], first["operation_id"])
            self.popen.assert_not_called()

    def test_apply_fails_closed_when_the_operation_cannot_be_recorded(self):
        """No durable owner ⇒ no publication. The device must not be touched."""
        broken = mock.Mock()
        broken.begin.side_effect = RuntimeError("database is gone")
        with mock.patch.object(server, "_mixed_operations_store_required",
                               return_value=(broken, False), create=True):
            status, body = self.request("POST", "/api/mixed/apply",
                                        {"task_id": TASK_ID})
        self.assertEqual(status, 503)
        self.assertIn("registrar", body["error"])
        self.popen.assert_not_called()

    # --- the same fail-closed discipline for the inventory refresh -----------
    def test_refresh_fails_closed_when_the_operation_cannot_be_recorded(self):
        broken = mock.Mock()
        broken.begin.side_effect = RuntimeError("database is gone")
        with mock.patch.object(server, "_mixed_operations_store_required",
                               return_value=(broken, False), create=True):
            status, body = self.request("POST", "/api/mixed/refresh", {})
        self.assertEqual(status, 503)
        self.assertIn("registrar", body["error"])
        self.popen.assert_not_called()

    def test_refresh_fails_closed_when_the_store_is_configured_but_unusable(self):
        """Configured but unreachable is a coordination failure, not legacy mode."""
        with mock.patch.object(server, "_mixed_operations_store_required",
                               return_value=(None, True), create=True):
            status, body = self.request("POST", "/api/mixed/refresh", {})
        self.assertEqual(status, 503)
        self.assertIn("registrar", body["error"])
        self.popen.assert_not_called()

    def test_apply_fails_closed_when_the_store_is_configured_but_unusable(self):
        with mock.patch.object(server, "_mixed_operations_store_required",
                               return_value=(None, True), create=True):
            status, body = self.request("POST", "/api/mixed/apply",
                                        {"task_id": TASK_ID})
        self.assertEqual(status, 503)
        self.popen.assert_not_called()

    def test_refresh_fails_closed_when_the_phase_cannot_be_written(self):
        """A row whose phase cannot be written must not be published behind.

        And it must not keep holding the tablet's slot: it is closed as
        failed_safe, which is a result the panel can show.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            self._with_store(db)
            broken = mock.Mock(wraps=server._mixed_get_operations_store())
            broken.set_phase.side_effect = RuntimeError("read-only filesystem")
            with mock.patch.object(server, "_mixed_operations_store_required",
                                   return_value=(broken, False), create=True):
                status, body = self.request("POST", "/api/mixed/refresh", {})
            self.assertEqual(status, 503)
            self.popen.assert_not_called()
            from scripts.mixed_operations import OperationStore
            pending = OperationStore(db).unresolved()
            self.assertEqual([row["state"] for row in pending], ["failed_safe"])

    def test_a_duplicate_refresh_returns_the_operation_and_launches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            self._with_store(db)
            status, first = self.request("POST", "/api/mixed/refresh",
                                         {"idempotency_key": "r-dup"})
            self.assertEqual(status, 200)
            self.assertEqual(self.popen.call_count, 1)
            status, second = self.request("POST", "/api/mixed/refresh",
                                          {"idempotency_key": "r-dup"})
            self.assertEqual(status, 200)
            self.assertEqual(second["operation_id"], first["operation_id"])
            self.assertTrue(second.get("duplicate"))
            self.assertEqual(self.popen.call_count, 1)

    def test_refresh_http_to_worker_chain_adopts_the_same_operation(self):
        """One chain, not two unit tests.

        The endpoint creates the operation, hands its id to the launched worker,
        and the worker adopts THAT id although a refresh has no task id at all.
        Ids and revisions on both ends must be the same value.
        """
        self.write_state(pending_generation=9)
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            self._with_store(db)
            status, body = self.request("POST", "/api/mixed/refresh", {})
            self.assertEqual(status, 200)
            operation_id = body.get("operation_id")
            self.assertIsNotNone(operation_id)
            # The launched worker received the very same id.
            argv = self.popen.call_args.args[0]
            self.assertIn("--operation-id", argv)
            self.assertEqual(argv[argv.index("--operation-id") + 1], operation_id)
            # The worker adopts it: taskless refresh, same revision.
            tracker = mgs._OperationTracker("tablet", "", revision=9,
                                            executor="hostA:10-100",
                                            operation_id=operation_id)
            self.assertEqual(tracker.claim(), "owned")
            from scripts.mixed_operations import OperationStore
            row = OperationStore(db).get(operation_id)
            self.assertEqual(row["executor"], "hostA:10-100")
            self.assertEqual(row["intent_revision"], "9")
            # And finishing closes the SAME row the panel is following.
            self.assertEqual(tracker.finish("succeeded", "Lista atualizada"),
                             "recorded")
            self.assertEqual(OperationStore(db).get(operation_id)["state"],
                             "succeeded")

    def test_a_worker_with_another_revision_is_refused_on_the_same_id(self):
        """The reserved revision is binding, not decorative."""
        self.write_state(pending_generation=9)
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            self._with_store(db)
            operation_id = self.request("POST", "/api/mixed/refresh", {})[1][
                "operation_id"]
            tracker = mgs._OperationTracker("tablet", "", revision=10,
                                            executor="hostA:10-100",
                                            operation_id=operation_id)
            self.assertEqual(tracker.claim(), "blocked")

    def test_a_terminal_operation_is_reported_not_republished(self):
        self.write_state(pending_generation=9)
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            self._with_store(db)
            operation_id = self.request("POST", "/api/mixed/refresh", {})[1][
                "operation_id"]
            from scripts.mixed_operations import OperationStore
            OperationStore(db).finish(operation_id, state="succeeded",
                                      user_message="Lista atualizada")
            tracker = mgs._OperationTracker("tablet", "", revision=9,
                                            executor="hostA:10-100",
                                            operation_id=operation_id)
            self.assertEqual(tracker.claim(), "already_terminal")

    def test_an_in_flight_apply_from_the_journal_reports_the_operation_id(self):
        """B7: the journal gate answers before any store write, so the id has to
        be looked up - otherwise a reloaded panel has nothing to follow."""
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            self._with_store(db)
            from scripts.mixed_operations import OperationStore
            op_id, _ = OperationStore(db).begin(
                tablet_id="tablet", principal="dashboard",
                intention={"task_id": TASK_ID, "operation": "apply"},
                idempotency_key="inflight-key", user_message="Atualização solicitada")
            self.write_state(apply={"task_id": TASK_ID, "state": "publishing"})
            status, body = self.request("POST", "/api/mixed/apply",
                                        {"task_id": TASK_ID})
            self.assertEqual(status, 200)
            self.assertEqual(body["state"], "in_progress")
            self.assertEqual(body.get("operation_id"), op_id)
            self.popen.assert_not_called()

    def test_an_in_flight_refresh_from_the_journal_reports_the_operation_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            self._with_store(db)
            from scripts.mixed_operations import OperationStore
            op_id, _ = OperationStore(db).begin(
                tablet_id="tablet", principal="dashboard",
                intention={"task_id": TASK_ID, "operation": "apply"},
                idempotency_key="inflight-key-2",
                user_message="Atualização solicitada")
            self.write_state(apply={"task_id": TASK_ID, "state": "publishing"})
            status, body = self.request("POST", "/api/mixed/refresh", {})
            self.assertEqual(status, 200)
            self.assertEqual(body["state"], "in_progress")
            self.assertEqual(body.get("operation_id"), op_id)
            self.popen.assert_not_called()

    def test_mixed_events_requires_the_pin(self):
        """UI13: the payload carries user_message and error codes."""
        status, _body = self.request("GET", "/api/mixed/events?cursor=0", pin=False)
        self.assertEqual(status, 401)
        status, body = self.request("GET", "/api/mixed/events?cursor=0")
        self.assertEqual(status, 200)

    def test_mixed_status_requires_the_pin(self):
        status, _body = self.request("GET", "/api/mixed/status", pin=False)
        self.assertEqual(status, 401)
        status, body = self.request("GET", "/api/mixed/status")
        self.assertEqual(status, 200)

    def test_the_server_and_the_store_share_one_exception_class(self):
        """`mixed_operations` e `scripts.mixed_operations` são módulos DIFERENTES.

        Se um teste montar o store por um caminho e chamar o helper do server por
        outro, `TabletBusy` deixa de casar e o resultado vira artefato de import
        (foi um bloqueio FALSO no harness do worker2). Esta asserção tranca isso.
        """
        from scripts import mixed_operations as ops_mod
        self.assertIs(server.TabletBusy, ops_mod.TabletBusy)
        self.assertIs(server.IdempotencyConflict, ops_mod.IdempotencyConflict)

    def test_a_new_request_reclaims_the_slot_from_a_dead_executor(self):
        """OOM/SIGKILL não pode travar o endpoint para sempre.

        Diferente do reenvio com o mesmo id: aqui um pedido NOVO (outra chave)
        encontra a vaga presa por um executor comprovadamente morto neste host.
        A operação órfã vira pendência visível e a vaga passa ao pedido novo.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            self._with_store(db)
            from scripts.mixed_operations import OperationStore
            store = OperationStore(db)
            # Um executor deste host que já não existe.
            dead = subprocess.Popen([sys.executable, "-c", "pass"])
            dead_pid = dead.pid
            dead.wait(timeout=30)          # já colhido: /proc/<pid> não existe
            stale, _ = store.begin(
                tablet_id="tablet", principal="dashboard",
                intention={"task_id": OTHER_TASK, "operation": "apply"},
                idempotency_key="stale-key", user_message="Atualização solicitada")
            store.set_phase(stale, state="preparing", stage="launch")
            store.claim_execution(stale, task_id=OTHER_TASK, revision=None,
                                  executor="%s:%d-1" % (socket.gethostname()[:32],
                                                        dead_pid))
            status, body = self.request("POST", "/api/mixed/refresh", {})
            self.assertEqual(status, 200, body)
            self.assertNotEqual(body.get("operation_id"), stale)
            # A órfã ficou VISÍVEL como pendência, não silenciosa.
            row = OperationStore(db).get(stale)
            self.assertEqual(row["state"], "failed_safe")
            self.assertEqual(row["error_code"], "executor_dead")
            self.assertIn(stale, [o["operation_id"]
                                  for o in OperationStore(db).unresolved()])

    def test_a_live_executor_is_never_stolen(self):
        """Contraprova: dono VIVO (ou de outro host) continua 409."""
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            self._with_store(db)
            from scripts.mixed_operations import OperationStore
            store = OperationStore(db)
            holder, _ = store.begin(
                tablet_id="tablet", principal="dashboard",
                intention={"task_id": OTHER_TASK, "operation": "apply"},
                idempotency_key="live-key", user_message="Atualização solicitada")
            store.set_phase(holder, state="preparing", stage="launch")
            # Identidade REAL deste processo: pid vivo E starttime correto (um
            # starttime inventado significaria, corretamente, "pid reciclado").
            store.claim_execution(holder, task_id=OTHER_TASK, revision=None,
                                  executor=mgs._local_execution_identity())
            status, body = self.request("POST", "/api/mixed/refresh", {})
            self.assertEqual(status, 409)
            self.assertEqual(body["active_operation_id"], holder)
            self.assertEqual(OperationStore(db).get(holder)["state"], "preparing")

    def test_events_without_a_store_says_unavailable_not_empty(self):
        """"No store" and "no events" are different facts.

        A panel that read `available: false` as "nothing happened" would
        silently drop a pending failure, so the endpoint must say which one it
        is.
        """
        with mock.patch.dict("os.environ", {"MIXED_OPERATIONS_DB": ""}, clear=False):
            status, body = self.request("GET", "/api/mixed/events")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["available"])
        self.assertEqual(body["events"], [])
        self.assertEqual(body["unresolved"], [])

    def test_events_with_a_store_returns_a_cursor_after_an_operation(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            with mock.patch.dict("os.environ", {"MIXED_OPERATIONS_DB": db},
                                 clear=False):
                patcher = mock.patch.object(server, "_mixed_ops_store", None,
                                            create=True)
                patcher.start()
                self.addCleanup(patcher.stop)
                self.request("POST", "/api/mixed/apply", {"task_id": TASK_ID})
                status, body = self.request("GET", "/api/mixed/events")
            self.assertEqual(status, 200)
            self.assertTrue(body["available"])
            self.assertGreater(body["cursor"], 0)
            # Two events for one operation: `created` from begin(), then `phase`
            # from the preparing/launch transition.
            kinds = [e["kind"] for e in body["events"]]
            self.assertEqual(kinds, ["created", "phase"])
            self.assertEqual(body["events"][0]["state"], "queued")
            # Following the cursor from the end yields nothing new.
            with mock.patch.dict("os.environ", {"MIXED_OPERATIONS_DB": db},
                                 clear=False):
                patcher = mock.patch.object(server, "_mixed_ops_store", None,
                                            create=True)
                patcher.start()
                self.addCleanup(patcher.stop)
                status2, later = self.request(
                    "GET", "/api/mixed/events?cursor=%d" % body["cursor"])
            self.assertEqual(status2, 200)
            self.assertEqual(later["events"], [])

    def test_events_rejects_an_invalid_cursor(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            with mock.patch.dict("os.environ", {"MIXED_OPERATIONS_DB": db},
                                 clear=False):
                patcher = mock.patch.object(server, "_mixed_ops_store", None,
                                            create=True)
                patcher.start()
                self.addCleanup(patcher.stop)
                status, body = self.request("GET", "/api/mixed/events?cursor=abc")
            self.assertEqual(status, 400)
            self.assertFalse(body["ok"])

    def test_foreign_guard_blocks_apply_without_launching(self):
        token = self.guard.acquire("other-publisher", 60, "publishing")["token"]
        try:
            status, body = self.request("POST", "/api/mixed/apply", {"task_id": TASK_ID})
            self.assertEqual(status, 409)
            self.assertIn("troca de modo", body["error"])
            self.popen.assert_not_called()
        finally:
            self.guard.release(token)

    def test_repeated_click_is_idempotent_while_the_same_task_is_in_flight(self):
        self.write_state(apply={"task_id": TASK_ID, "state": "publishing"})
        status, body = self.request("POST", "/api/mixed/apply", {"task_id": TASK_ID})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "in_progress")
        self.popen.assert_not_called()

    def test_other_task_in_flight_is_refused(self):
        self.write_state(apply={"task_id": OTHER_TASK, "state": "publishing"})
        status, body = self.request("POST", "/api/mixed/apply", {"task_id": TASK_ID})
        self.assertEqual(status, 409)
        self.assertEqual(body["task_id"], OTHER_TASK)
        self.popen.assert_not_called()

    def test_apply_does_not_touch_vod_state_or_history(self):
        with mock.patch.object(server.VOD_HISTORY, "record_selected", mock.Mock()) as recorder, \
                mock.patch.object(server.VOD_HISTORY, "checkpoint", mock.Mock()) as checkpoint, \
                mock.patch.object(server, "set_active_vod", mock.Mock(), create=True) as setter, \
                mock.patch.object(server, "dispatch_device_cmd", mock.Mock(), create=True) as dispatch:
            status, body = self.request("POST", "/api/mixed/apply", {"task_id": TASK_ID})
        self.assertEqual(status, 200)
        setter.assert_not_called()
        dispatch.assert_not_called()
        recorder.assert_not_called()
        checkpoint.assert_not_called()

    def test_unavailable_publisher_keeps_the_legacy_path(self):
        with mock.patch.object(server, "_mixed_apply_command", return_value=None):
            status, body = self.request("POST", "/api/mixed/apply", {"task_id": TASK_ID})
        self.assertEqual(status, 200)
        self.assertFalse(body["available"])
        self.popen.assert_not_called()

    # --- bridge transport --------------------------------------------------

    def test_bridge_transport_needs_no_publisher_inside_the_container(self):
        spool = self.root / "bridge"
        spool.mkdir()
        script = Path(server.__file__).resolve().parent / "scripts" / "mixed_bridge.py"
        self.assertTrue(script.is_file())
        with mock.patch.dict("os.environ", {
                "MIXED_APPLY_CMD": "", "MIXED_BRIDGE_SPOOL": str(spool),
                "MIXED_APP_DIR": str(ROOT)}):
            # Available even though no local publisher exists at all.
            status, body = self.request("GET", "/api/mixed/status")
            self.assertEqual(status, 200)
            self.assertTrue(body["available"])

            with mock.patch.object(server.subprocess, "run",
                                   return_value=CompletedProcess([], 1, "", "")) as run:
                status, body = self.request("GET", f"/api/mixed/confirm?task_id={TASK_ID}")
            self.assertEqual(status, 200, body)
            self.assertIsNone(body["confirmed"])
            argv = run.call_args.args[0]
            self.assertEqual(argv[:3], [sys.executable, str(script), "client"])
            self.assertIn("--spool", argv)
            self.assertIn(str(spool), argv)
            self.assertEqual(argv[argv.index("--action") + 1], "verify")
            self.assertEqual(argv[argv.index("--task-id") + 1], TASK_ID)
            # Never a shell string, never adb/ssh inside the container.
            self.assertIsInstance(argv, list)
            self.assertNotIn("shell", [str(a).lower() for a in argv])

    def test_bridge_apply_launches_the_client_without_background(self):
        spool = self.root / "bridge2"
        spool.mkdir()
        with mock.patch.dict("os.environ", {
                "MIXED_APPLY_CMD": "", "MIXED_BRIDGE_SPOOL": str(spool),
                "MIXED_APP_DIR": str(ROOT)}):
            status, body = self.request("POST", "/api/mixed/apply", {"task_id": TASK_ID})
        self.assertEqual(status, 200)
        self.assertTrue(body["available"])
        self.popen.assert_called_once()
        argv = self.popen.call_args.args[0]
        self.assertIn("client", argv)
        self.assertEqual(argv[argv.index("--action") + 1], "apply")
        # The bridge client waits for the daemon's answer itself.
        self.assertNotIn("--background", argv)

    # --- inventory refresh (no task anchor) ---------------------------------

    def test_refresh_requires_pin_and_does_not_need_a_ready_vod(self):
        # No ready VOD exists at all: the endpoint must still work, because the
        # thing that changed is the inventory.
        self.tasks.clear()
        status, body = self.request("POST", "/api/mixed/refresh", {}, pin=False)
        self.assertEqual(status, 401)
        self.popen.assert_not_called()

        status, body = self.request("POST", "/api/mixed/refresh", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["state"], "started")
        self.popen.assert_called_once()
        argv = self.popen.call_args.args[0]
        self.assertIn("--refresh", argv)
        self.assertNotIn("--task-id", argv)
        # Direct transport: the local publisher detaches itself.
        self.assertIn("--background", argv)

    def test_refresh_passes_the_interruption_confirmation_only_when_asked(self):
        status, body = self.request("POST", "/api/mixed/refresh",
                                    {"confirm_interrupt": True})
        self.assertEqual(status, 200)
        argv = self.popen.call_args.args[0]
        self.assertIn("--confirm-interrupt", argv)

        self.popen.reset_mock()
        status, body = self.request("POST", "/api/mixed/refresh", {})
        argv = self.popen.call_args.args[0]
        self.assertNotIn("--confirm-interrupt", argv)

    def test_refresh_is_refused_while_another_update_is_in_flight(self):
        self.write_state(apply={"task_id": TASK_ID, "state": "publishing"})
        status, body = self.request("POST", "/api/mixed/refresh", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "in_progress")
        self.assertEqual(body["task_id"], TASK_ID)
        self.popen.assert_not_called()

    def test_refresh_reports_unavailable_without_a_publisher(self):
        with mock.patch.object(server, "_mixed_apply_command", return_value=None), \
                mock.patch.object(server, "_mixed_bridge_spool", return_value=None):
            status, body = self.request("POST", "/api/mixed/refresh", {})
        self.assertEqual(status, 200)
        self.assertFalse(body["available"])
        self.popen.assert_not_called()

    def _with_store(self, db):
        """Point the endpoint at a throwaway store and forget any cached one."""
        patcher = mock.patch.dict("os.environ", {"MIXED_OPERATIONS_DB": db},
                                  clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        reset = mock.patch.object(server, "_mixed_ops_store", None, create=True)
        reset.start()
        self.addCleanup(reset.stop)

    def test_the_reserved_revision_travels_with_the_operation(self):
        """Correlation needs the TARGET, not just the task.

        The revision is read from the journal the publisher writes, so both sides
        name the same generation and the worker's claim can compare them instead
        of trusting the tablet's single slot.
        """
        self.write_state(pending_generation=7, pending_session="mixed-gen7-abc")
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            self._with_store(db)
            status, body = self.request("POST", "/api/mixed/apply",
                                        {"task_id": TASK_ID})
            self.assertEqual(status, 200)
            from scripts.mixed_operations import OperationStore
            row = OperationStore(db).get(body["operation_id"])
            self.assertEqual(row["intent_revision"], "7")
            self.assertEqual(row["desired_generation"], 7)

    def test_refresh_with_a_store_records_a_persistent_operation(self):
        """A refresh is a publication: it needs a durable id too."""
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            self._with_store(db)
            status, body = self.request("POST", "/api/mixed/refresh", {})
            self.assertEqual(status, 200)
            operation_id = body.get("operation_id")
            self.assertIsNotNone(operation_id)
            from scripts.mixed_operations import OperationStore
            row = OperationStore(db).get(operation_id)
            self.assertEqual(row["intention_hash"], mo.intention_hash(
                {"operation": "refresh", "confirm_interrupt": False,
                 "revision": None, "desired_generation": None}))
            self.assertIn(row["state"], ("preparing", "queued"))

    def test_a_repeated_refresh_key_returns_the_same_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            self._with_store(db)
            first = self.request("POST", "/api/mixed/refresh",
                                 {"idempotency_key": "r-key-1"})[1]
            second = self.request("POST", "/api/mixed/refresh",
                                  {"idempotency_key": "r-key-1"})[1]
            self.assertIsNotNone(first.get("operation_id"))
            self.assertEqual(first["operation_id"], second.get("operation_id"))

    def test_a_refresh_key_with_another_intention_is_a_named_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            self._with_store(db)
            original = self.request("POST", "/api/mixed/refresh",
                                    {"idempotency_key": "r-key-2"})[1]
            status, body = self.request("POST", "/api/mixed/refresh",
                                        {"idempotency_key": "r-key-2",
                                         "confirm_interrupt": True})
            self.assertEqual(status, 409)
            # The name of the operation that already exists, never a silent reuse.
            self.assertEqual(body["operation_id"], original["operation_id"])

    def test_refresh_while_the_tablet_slot_is_taken_starts_no_second_publisher(self):
        """The device has ONE slot: a competing request is pointed at it."""
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            self._with_store(db)
            from scripts.mixed_operations import OperationStore
            store = OperationStore(db)
            holder, _ = store.begin(tablet_id="tablet", principal="dashboard",
                                    intention={"task_id": TASK_ID,
                                               "operation": "apply"},
                                    idempotency_key="other-key",
                                    user_message="Atualização solicitada")
            status, body = self.request("POST", "/api/mixed/refresh",
                                        {"idempotency_key": "r-key-3"})
            self.assertEqual(status, 409)
            self.assertEqual(body["active_operation_id"], holder)
            self.popen.assert_not_called()

    def test_legacy_vod_play_still_works(self):
        """The old button behaviour must be untouched when mixed is absent."""
        with mock.patch.object(server, "VOD_PLAYBACK_CACHE", mock.Mock(), create=True), \
                mock.patch.object(server, "VOD_HISTORY", mock.Mock(), create=True), \
                mock.patch.object(server, "ACTIVE_VOD_TASK", None, create=True), \
                mock.patch.object(server, "VOD_SELECTION_GENERATION", 0, create=True), \
                mock.patch.object(server, "VOD_SELECTION_TASK", None, create=True), \
                mock.patch.object(server, "set_active_vod", mock.Mock(), create=True), \
                mock.patch.object(server, "dispatch_device_cmd", mock.Mock(return_value=True),
                                  create=True) as dispatch:
            status, body = self.request("POST", "/api/vod/play", {"task_id": TASK_ID})
        self.assertEqual(status, 200)
        self.assertTrue(body["success"])
        dispatch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
