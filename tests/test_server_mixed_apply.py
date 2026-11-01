"""Manual mixed-generation update: the existing "NA TV" button applies the
virtual pendrive list (server endpoints + idempotence + guard respect).

The click is the authorisation: the server launches the publisher in the
background (list form, never a shell string) and reports the journalled state.
A repeated click while the same task is in flight is idempotent.
"""

import http.client
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

import server

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
