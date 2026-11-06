#!/usr/bin/env python3
"""Contracts for the HTTP-facing operation adapter (Gerações V2, fase 1).

These tests replace the socket with a direct call: what matters is the mapping
request -> operation and operation -> public payload, not the HTTP plumbing.
Every test names the acceptance-matrix row it covers.
"""

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import mixed_operations as ops  # noqa: E402
import mixed_ops_api as api  # noqa: E402

TASK = "a1b2c3d4"


class AdapterTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = ops.OperationStore(pathlib.Path(self._dir.name) / "ops.db")

    def apply(self, **body):
        body.setdefault("task_id", TASK)
        return api.request_apply(self.store, tablet_id="t1",
                                 principal="default", body=body)


class ApplyTests(AdapterTestCase):
    """Matrix: 'Clique duplicado/reload/two workers'."""

    def test_new_request_returns_202_with_a_persisted_operation_id(self):
        status, payload = self.apply()
        self.assertEqual(status, api.HTTP_ACCEPTED)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["duplicate"])
        # The id must already be readable: it was persisted before returning.
        row = self.store.get(payload["operation_id"])
        self.assertEqual(row["state"], "queued")

    def test_same_key_and_intention_returns_the_same_operation_with_200(self):
        first_status, first = self.apply(idempotency_key="k1")
        second_status, second = self.apply(idempotency_key="k1")
        self.assertEqual(first_status, api.HTTP_ACCEPTED)
        self.assertEqual(second_status, api.HTTP_OK)
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["operation_id"], second["operation_id"])

    def test_same_key_different_intention_is_409_naming_the_original(self):
        _, first = self.apply(idempotency_key="k1")
        status, payload = self.apply(idempotency_key="k1", task_id="ffffffff")
        self.assertEqual(status, api.HTTP_CONFLICT)
        self.assertEqual(payload["error"], "idempotency_key_reused")
        self.assertEqual(payload["operation_id"], first["operation_id"])

    def test_second_operation_for_the_same_tablet_is_409_with_active_id(self):
        _, first = self.apply(idempotency_key="k1")
        status, payload = self.apply(idempotency_key="k2", task_id="b2c3d4e5")
        self.assertEqual(status, api.HTTP_CONFLICT)
        self.assertEqual(payload["error"], "operation_in_progress")
        self.assertEqual(payload["active_operation_id"], first["operation_id"])

    def test_invalid_task_id_is_refused_before_creating_anything(self):
        for bad in ("", "xyz", "123", TASK + "0", None):
            with self.subTest(task_id=bad):
                status, payload = self.apply(task_id=bad)
                self.assertEqual(status, api.HTTP_BAD_REQUEST)
                self.assertFalse(payload["ok"])
        self.assertIsNone(self.store.active_for_tablet("t1"))

    def test_invalid_key_is_refused(self):
        status, _ = self.apply(idempotency_key="")
        self.assertEqual(status, api.HTTP_BAD_REQUEST)
        status, _ = self.apply(idempotency_key="x" * 200)
        self.assertEqual(status, api.HTTP_BAD_REQUEST)

    def test_refresh_needs_no_task_id(self):
        status, payload = api.request_apply(
            self.store, tablet_id="t1", principal="default",
            body={"operation": "refresh"})
        self.assertEqual(status, api.HTTP_ACCEPTED)
        self.assertTrue(payload["ok"])

    def test_unknown_operation_kind_is_refused(self):
        status, _ = api.request_apply(
            self.store, tablet_id="t1", principal="default",
            body={"operation": "delete_everything"})
        self.assertEqual(status, api.HTTP_BAD_REQUEST)


class PublicPayloadTests(AdapterTestCase):
    """Contract: the payload never carries secrets or internals."""

    def test_internal_fields_never_reach_the_panel(self):
        _, created = self.apply(idempotency_key="super-secret-key")
        status, payload = api.request_status(self.store,
                                             operation_id=created["operation_id"])
        self.assertEqual(status, api.HTTP_OK)
        op = payload["operation"]
        for forbidden in ("idempotency_key", "principal", "intention_hash",
                          "expires_at", "fencing_epoch"):
            self.assertNotIn(forbidden, op)
        self.assertNotIn("super-secret-key", repr(payload))

    def test_unknown_totals_are_null_not_zero(self):
        _, created = self.apply()
        _, payload = api.request_status(self.store,
                                        operation_id=created["operation_id"])
        op = payload["operation"]
        self.assertIsNone(op["bytes_done"])
        self.assertIsNone(op["bytes_total"])
        self.assertIsNone(op["percent"])

    def test_measured_totals_produce_a_percentage(self):
        _, created = self.apply()
        op_id = created["operation_id"]
        self.store.set_phase(op_id, state="transferring",
                             bytes_done=512, bytes_total=1024)
        _, payload = api.request_status(self.store, operation_id=op_id)
        self.assertEqual(payload["operation"]["percent"], 50.0)


class StatusTests(AdapterTestCase):
    def test_unknown_operation_is_404(self):
        status, payload = api.request_status(self.store, operation_id="deadbeef")
        self.assertEqual(status, api.HTTP_NOT_FOUND)
        self.assertEqual(payload["error"], "operation_not_found")

    def test_missing_operation_id_is_400(self):
        status, _ = api.request_status(self.store, operation_id="")
        self.assertEqual(status, api.HTTP_BAD_REQUEST)

    def test_active_returns_the_operation_holding_the_slot(self):
        _, created = self.apply()
        _, payload = api.request_active(self.store, tablet_id="t1")
        self.assertIsNotNone(payload["operation"])
        self.assertEqual(payload["operation"]["operation_id"],
                         created["operation_id"])

    def test_active_is_null_when_idle(self):
        _, payload = api.request_active(self.store, tablet_id="t1")
        self.assertIsNone(payload["operation"])


class EventTests(AdapterTestCase):
    """Matrix: 'Falha UI/rede, sair e voltar'."""

    def test_events_carry_a_cursor_that_advances(self):
        _, created = self.apply()
        status, first = api.request_events(self.store, cursor=0)
        self.assertEqual(status, api.HTTP_OK)
        self.assertGreater(first["cursor"], 0)
        _, again = api.request_events(self.store, cursor=first["cursor"])
        self.assertEqual(again["events"], [])

    def test_unresolved_problem_appears_even_with_a_current_cursor(self):
        _, created = self.apply()
        op_id = created["operation_id"]
        self.store.finish(op_id, state="needs_reconciliation",
                          user_message="Conexão perdida após a troca")
        _, snap = api.request_events(self.store, cursor=0)
        cursor = snap["cursor"]
        _, later = api.request_events(self.store, cursor=cursor)
        self.assertIn(op_id, later["unresolved"])

    def test_resolved_problem_leaves_the_unresolved_list(self):
        _, created = self.apply()
        op_id = created["operation_id"]
        self.store.finish(op_id, state="needs_reconciliation")
        self.store.resolve_reconciliation(
            op_id, resolution="not_applied",
            proof="LUN aponta para ntfs_lab; nada foi aplicado")
        _, snap = api.request_events(self.store, cursor=0)
        self.assertEqual(snap["unresolved"], [])

    def test_invalid_cursor_is_refused(self):
        status, _ = api.request_events(self.store, cursor="not-a-number")
        self.assertEqual(status, api.HTTP_BAD_REQUEST)


if __name__ == "__main__":
    unittest.main()