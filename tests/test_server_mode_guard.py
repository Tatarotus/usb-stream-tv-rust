"""Exclusive ModeGuard lease: acquire/renew/release, auth and enforcement.

The mixed-media publisher proves the TV is idle, then switches the USB LUN.
Playback is started through this server, so the server must hold the lease
and refuse foreign playback/mode commands for as long as it is live.
"""

import http.client
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import server

PIN = "mode-guard-test-pin"
TASK_ID = "a1b2c3d4"
TOKEN_RE = re.compile(r"[0-9a-f]{32}")


class QuietHandler(server.RequestHandler):
    def log_message(self, *args):
        pass


class TestModeGuardLease(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mode-guard-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / f"{TASK_ID}.mp4"
        self.media.write_bytes(b"\x00" * 1024)

        self.tasks = {
            TASK_ID: {
                "id": TASK_ID,
                "title": "Guard Movie",
                "display_name": "Guard Movie.mp4",
                "status": "ready",
                "storage": "local",
                "file_path": str(self.media),
                "file_size": self.media.stat().st_size,
            }
        }
        self.guard = server.ModeGuard()
        self.history = mock.Mock()
        self.cache = mock.Mock()
        self.bus_submit = mock.Mock(return_value=mock.Mock())

        replacements = {
            "AUTH_PIN": PIN,
            "MODE_GUARD": self.guard,
            "VOD_TASKS": self.tasks,
            "VOD_DIR": str(self.root),
            "ACTIVE_VOD_FILE": str(self.root / "active_vod.txt"),
            "ACTIVE_VOD_TASK": None,
            "VOD_SELECTION_GENERATION": 0,
            "VOD_SELECTION_TASK": None,
            "VOD_HISTORY": self.history,
            "VOD_PLAYBACK_CACHE": self.cache,
            "LAST_VOD_STREAM_TIME": 0.0,
            "log_event": mock.Mock(),
            "subprocess": mock.Mock(),
        }
        for name, value in replacements.items():
            patcher = mock.patch.object(server, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

        for name in ("pending_command", "tablet_pending_cmd", "tablet_vod_mode"):
            default = "" if name == "tablet_vod_mode" else None
            patcher = mock.patch.object(server.HUB, name, default, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(server.HUB.bus, "submit", self.bus_submit)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.httpd = server.ThreadedHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_http)

    def _stop_http(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, payload=None, pin=True, extra_headers=None):
        headers = {"Connection": "close"}
        if pin:
            headers["X-Auth-PIN"] = PIN
        headers.update(extra_headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload)
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            return response.status, raw
        finally:
            conn.close()

    def post_guard(self, payload, pin=True, extra_headers=None):
        status, raw = self.request("POST", "/api/mode_guard", payload,
                                   pin=pin, extra_headers=extra_headers)
        return status, json.loads(raw or b"{}"), raw

    def acquire_guard(self, owner="publisher", ttl=30, phase="usb_switch"):
        status, body, raw = self.post_guard(
            {"action": "acquire", "owner": owner, "ttl": ttl, "phase": phase})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        return body["token"]

    # --- lease lifecycle -------------------------------------------------

    def test_acquire_renew_release_happy_path(self):
        status, body, raw = self.post_guard(
            {"action": "acquire", "owner": "publisher", "ttl": 30,
             "phase": "usb_switch"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        token = body["token"]
        self.assertEqual(len(token), 32)
        self.assertEqual(body["owner"], "publisher")
        self.assertEqual(body["phase"], "usb_switch")
        self.assertEqual(body["expires_in"], 30)

        status, view, raw = self.post_guard({"action": "status"})
        self.assertEqual(status, 200)
        self.assertTrue(view["held"])
        self.assertEqual(view["owner"], "publisher")
        self.assertEqual(view["phase"], "usb_switch")
        self.assertIsNone(TOKEN_RE.search(raw.decode()))
        self.assertNotIn(token, raw.decode())

        status, renewed, raw = self.post_guard(
            {"action": "renew", "token": token, "ttl": 90})
        self.assertEqual(status, 200)
        self.assertTrue(renewed["ok"])
        self.assertEqual(renewed["expires_in"], 90)
        self.assertIsNone(TOKEN_RE.search(raw.decode()))

        status, released, raw = self.post_guard({"action": "release", "token": token})
        self.assertEqual(status, 200)
        self.assertTrue(released["released"])

        status, view, _ = self.post_guard({"action": "status"})
        self.assertEqual(status, 200)
        self.assertFalse(view["held"])

    def test_ttl_is_clamped_and_invalid_inputs_rejected(self):
        status, body, _ = self.post_guard(
            {"action": "acquire", "owner": "publisher", "ttl": 1,
             "phase": "usb_switch"})
        self.assertEqual(status, 200)
        self.assertEqual(body["expires_in"], 15)  # clamped up to the minimum

        status, body, _ = self.post_guard(
            {"action": "acquire", "owner": "publisher", "ttl": 9999,
             "phase": "usb_switch"})
        self.assertEqual(status, 409)  # still held by the previous lease

        self.post_guard({"action": "release", "token": body.get("token")})

        status, body, _ = self.post_guard(
            {"action": "acquire", "owner": "bad owner!", "ttl": 30,
             "phase": "usb_switch"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

        status, body, _ = self.post_guard(
            {"action": "acquire", "owner": "publisher", "ttl": 30,
             "phase": "NOT VALID"})
        self.assertEqual(status, 400)

        status, body, _ = self.post_guard(
            {"action": "acquire", "owner": "publisher", "ttl": "abc",
             "phase": "usb_switch"})
        self.assertEqual(status, 400)

    def test_second_acquire_while_held_is_busy(self):
        token = self.acquire_guard()
        status, body, raw = self.post_guard(
            {"action": "acquire", "owner": "intruder", "ttl": 30,
             "phase": "usb_switch"})
        self.assertEqual(status, 409)
        self.assertFalse(body["ok"])
        self.assertTrue(body["busy"])
        self.assertEqual(body["holder"]["owner"], "publisher")
        self.assertTrue(body["holder"]["held"])
        self.assertIsNone(TOKEN_RE.search(raw.decode()))
        self.assertNotIn(token, raw.decode())

    def test_expired_lease_is_cleared_and_reacquirable(self):
        self.acquire_guard()
        with self.guard._lock:
            self.guard._state["expires_at"] = time.time() - 1

        status, body, _ = self.post_guard(
            {"action": "acquire", "owner": "publisher", "ttl": 30,
             "phase": "usb_switch"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_wrong_token_cannot_renew_or_release(self):
        token = self.acquire_guard()
        wrong = "f" * 32

        status, body, raw = self.post_guard(
            {"action": "renew", "token": wrong, "ttl": 60})
        self.assertEqual(status, 409)
        self.assertEqual(body, {"ok": False, "expired": True})
        self.assertNotIn(token, raw.decode())

        status, body, _ = self.post_guard({"action": "release", "token": wrong})
        self.assertEqual(status, 409)
        self.assertEqual(body, {"ok": False, "expired": True})

        status, view, _ = self.post_guard({"action": "status"})
        self.assertTrue(view["held"])
        self.assertEqual(view["owner"], "publisher")

        # The real token still owns the lease.
        status, body, _ = self.post_guard(
            {"action": "renew", "token": token, "ttl": 30})
        self.assertEqual(status, 200)

    def test_auth_pin_required(self):
        payload = {"action": "acquire", "owner": "publisher", "ttl": 30,
                   "phase": "usb_switch"}
        status, raw = self.request("POST", "/api/mode_guard", payload, pin=False)
        self.assertEqual(status, 401)
        self.assertFalse(json.loads(raw)["success"])

        status, raw = self.request(
            "POST", "/api/mode_guard", payload, pin=False,
            extra_headers={"X-Auth-PIN": "wrong-pin"})
        self.assertEqual(status, 401)

        status, body, _ = self.post_guard(payload)
        self.assertEqual(status, 200)

    def test_concurrent_acquire_has_exactly_one_winner(self):
        barrier = threading.Barrier(8)
        results = []

        def worker(index):
            barrier.wait()
            results.append(self.guard.acquire(f"worker{index}", 30, "switch"))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        winners = [r for r in results if r.get("ok")]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len([r for r in results if r.get("busy")]), 7)

    # --- enforcement -----------------------------------------------------

    def test_foreign_guard_blocks_dispatch_device_cmd(self):
        token = self.acquire_guard()
        self.assertIsNone(server.HUB.pending_command)

        result = server.dispatch_device_cmd("sh /data/local/tmp/switch_live.sh")
        self.assertIs(result, False)
        self.assertIsNone(server.HUB.pending_command)
        self.bus_submit.assert_not_called()

        # The holder's own token may dispatch.
        result = server.dispatch_device_cmd(
            "sh /data/local/tmp/switch_live.sh", guard_token=token)
        self.assertIsNot(result, None)
        self.assertIsNot(result, False)
        self.assertIn("switch_live.sh", server.HUB.pending_command)
        self.bus_submit.assert_called_once()

    def test_foreign_guard_blocks_vod_play_without_side_effects(self):
        token = self.acquire_guard()
        self.cache.reset_mock()
        self.history.reset_mock()

        status, body, _ = self.post_guard({"action": "status"})
        self.assertTrue(body["held"])

        status, raw = self.request(
            "POST", "/api/vod/play", {"task_id": TASK_ID})
        self.assertEqual(status, 409)
        payload = json.loads(raw)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"], "troca de modo em andamento")

        self.assertIsNone(server.ACTIVE_VOD_TASK)
        self.assertEqual(server.HUB.tablet_vod_mode, "")
        self.history.record_selected.assert_not_called()
        self.cache.start.assert_not_called()
        self.cache.stop.assert_not_called()
        self.bus_submit.assert_not_called()

        # The holder can play with its token (X-Mode-Guard-Token header).
        status, raw = self.request(
            "POST", "/api/vod/play", {"task_id": TASK_ID},
            extra_headers={"X-Mode-Guard-Token": token})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["success"])
        self.assertEqual(server.ACTIVE_VOD_TASK, TASK_ID)
        self.assertEqual(server.HUB.tablet_vod_mode, TASK_ID)
        self.history.record_selected.assert_called_once()
        self.bus_submit.assert_called_once()

    def test_status_endpoint_exposes_token_free_mode_guard(self):
        status, raw = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertIn("mode_guard", data)
        self.assertFalse(data["mode_guard"]["held"])

        token = self.acquire_guard()
        status, raw = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["mode_guard"]["held"])
        self.assertEqual(data["mode_guard"]["owner"], "publisher")
        self.assertIsNone(TOKEN_RE.search(raw.decode()))
        self.assertNotIn(token, raw.decode())


if __name__ == "__main__":
    unittest.main()