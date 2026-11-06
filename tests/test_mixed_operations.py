#!/usr/bin/env python3
"""Contracts for the persistent operation store (Gerações V2, fase 1).

Each test names the acceptance-matrix row it covers. The store is storage: it
never talks to the tablet. What is proven here is durability and idempotency —
not that the TV shows anything.
"""

import pathlib
import sqlite3
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import mixed_operations as mo  # noqa: E402


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = pathlib.Path(self._dir.name) / "ops.db"
        self.store = mo.OperationStore(self.path)

    def begin(self, **kw):
        kw.setdefault("tablet_id", "t1")
        kw.setdefault("principal", "default")
        kw.setdefault("intention", {"task_id": "a1b2c3d4"})
        return self.store.begin(**kw)


class IdempotencyTests(StoreTestCase):
    """Matrix: 'Clique duplicado/reload/two workers'."""

    def test_same_key_and_intention_returns_the_original_operation(self):
        op1, created1 = self.begin(idempotency_key="k1")
        op2, created2 = self.begin(idempotency_key="k1")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(op1, op2)

    def test_same_key_different_intention_is_a_conflict_with_the_original_id(self):
        op1, _ = self.begin(idempotency_key="k1")
        with self.assertRaises(mo.IdempotencyConflict) as ctx:
            self.begin(idempotency_key="k1",
                       intention={"task_id": "ffffffff"})
        self.assertEqual(ctx.exception.operation_id, op1)

    def test_intention_hash_ignores_key_order(self):
        a = mo.intention_hash({"task_id": "x", "revision": "md5:1"})
        b = mo.intention_hash({"revision": "md5:1", "task_id": "x"})
        self.assertEqual(a, b)

    def test_expired_key_is_rejected_not_recycled(self):
        op1, _ = self.begin(idempotency_key="k1")
        self.store.finish(op1, state="succeeded")
        # Force expiry, then release the key the way maintenance would.
        with self.store._tx() as conn:
            conn.execute("UPDATE operations SET expires_at=0 WHERE operation_id=?",
                         (op1,))
        self.store.expire_old_keys()
        self.assertIsNone(self.store.find_by_key(principal="default",
                                                 tablet_id="t1",
                                                 idempotency_key="k1"))
        # A fresh key starts a fresh operation; the old one is still readable.
        op2, created = self.begin(idempotency_key="k2")
        self.assertTrue(created)
        self.assertNotEqual(op1, op2)

    def test_unknown_key_returns_none(self):
        self.assertIsNone(self.store.find_by_key(principal="default",
                                                 tablet_id="t1",
                                                 idempotency_key="nope"))


class SingleWriterTests(StoreTestCase):
    """Matrix: 'Clique duplicado/reload/two workers' — at most one switch."""

    def test_second_operation_for_the_same_tablet_is_refused(self):
        op1, _ = self.begin(idempotency_key="k1")
        with self.assertRaises(mo.TabletBusy) as ctx:
            self.begin(idempotency_key="k2",
                       intention={"task_id": "b2c3d4e5"})
        self.assertEqual(ctx.exception.operation_id, op1)

    def test_slot_is_released_only_by_a_terminal_state(self):
        op1, _ = self.begin(idempotency_key="k1")
        self.store.set_phase(op1, state="transferring")
        with self.assertRaises(mo.TabletBusy):
            self.begin(idempotency_key="k2", intention={"task_id": "b2c3d4e5"})
        self.store.finish(op1, state="succeeded")
        op2, created = self.begin(idempotency_key="k2",
                                  intention={"task_id": "b2c3d4e5"})
        self.assertTrue(created)
        self.assertNotEqual(op1, op2)

    def test_needs_reconciliation_keeps_holding_the_slot(self):
        """An ambiguous device must not admit a second writer."""
        op1, _ = self.begin(idempotency_key="k1")
        self.store.finish(op1, state="needs_reconciliation",
                          user_message="Conexão perdida após a troca")
        with self.assertRaises(mo.TabletBusy) as ctx:
            self.begin(idempotency_key="k2", intention={"task_id": "b2c3d4e5"})
        self.assertEqual(ctx.exception.operation_id, op1)
        # It also stays visible as an unresolved problem.
        self.assertIn(op1, [o["operation_id"] for o in self.store.unresolved()])

    def test_two_tablets_are_independent(self):
        op1, _ = self.begin(tablet_id="t1", idempotency_key="k1")
        op2, created = self.begin(tablet_id="t2", idempotency_key="k1")
        self.assertTrue(created)
        self.assertNotEqual(op1, op2)


class PhaseTests(StoreTestCase):
    """Matrix: 'Crash antes/depois de cada efeito'."""

    def test_phases_are_recorded_in_order_with_events(self):
        op, _ = self.begin(idempotency_key="k1")
        for state in ("preparing", "transferring", "validating",
                      "ready_to_activate", "activating", "verifying"):
            self.store.set_phase(op, state=state, stage=state)
        current = self.store.get(op)
        self.assertEqual(current["state"], "verifying")
        kinds = [e["kind"] for e in self.store.events_since(0)]
        self.assertEqual(kinds[0], "created")
        self.assertEqual(kinds.count("phase"), 6)

    def test_unknown_state_is_refused(self):
        op, _ = self.begin(idempotency_key="k1")
        with self.assertRaises(mo.OperationsError):
            self.store.set_phase(op, state="banana")

    def test_readiness_is_not_confirmation(self):
        """`ready_to_activate` must not read as applied."""
        op, _ = self.begin(idempotency_key="k1")
        self.store.set_phase(op, state="ready_to_activate")
        self.assertNotIn(self.store.get(op)["state"], mo.TERMINAL_STATES)

    def test_heartbeat_changes_no_state(self):
        op, _ = self.begin(idempotency_key="k1")
        self.store.set_phase(op, state="transferring")
        before = self.store.get(op)["state"]
        self.store.heartbeat(op)
        self.assertEqual(self.store.get(op)["state"], before)

    def test_unknown_operation_raises(self):
        with self.assertRaises(mo.UnknownOperation):
            self.store.set_phase("deadbeef", state="preparing")
        with self.assertRaises(mo.UnknownOperation):
            self.store.get("deadbeef")

    def test_unknown_bytes_are_null_not_zero(self):
        """A total that was never measured must not become a fake 0/0 bar."""
        op, _ = self.begin(idempotency_key="k1")
        current = self.store.get(op)
        self.assertIsNone(current["bytes_done"])
        self.assertIsNone(current["bytes_total"])


class CrashConsistencyTests(StoreTestCase):
    """Matrix: 'Falha UI/rede, sair e voltar' and terminal durability."""

    def test_terminal_state_and_event_are_written_together(self):
        op, _ = self.begin(idempotency_key="k1")
        self.store.finish(op, state="failed_safe", user_message="Falha na transferência",
                          error_code="transfer_timeout", retryable=True)
        events = self.store.events_since(0)
        terminal = [e for e in events if e["kind"] == "terminal"]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["state"], "failed_safe")
        self.assertEqual(terminal[0]["operation_id"], op)

    def test_interrupted_operation_survives_a_new_store_instance(self):
        """A worker restart must not erase an interrupted operation."""
        op, _ = self.begin(idempotency_key="k1")
        self.store.set_phase(op, state="activating")
        reopened = mo.OperationStore(self.path)
        self.assertEqual(reopened.get(op)["state"], "activating")
        self.assertIsNotNone(reopened.active_for_tablet("t1"))

    def test_unresolved_problem_returns_after_reload_with_stale_cursor(self):
        op, _ = self.begin(idempotency_key="k1")
        self.store.finish(op, state="needs_reconciliation",
                          user_message="Estado da TV desconhecido")
        cursor = self.store.snapshot()["cursor"]
        reopened = mo.OperationStore(self.path)
        # A client that already consumed every event still learns of the problem.
        snap = reopened.snapshot(cursor)
        self.assertIn(op, snap["unresolved"])
        self.assertIn(op, [o["operation_id"] for o in snap["operations"]])

    def test_terminal_state_is_not_offered_as_unresolved(self):
        op, _ = self.begin(idempotency_key="k1")
        self.store.finish(op, state="succeeded")
        self.assertEqual(self.store.unresolved(), [])


class CursorTests(StoreTestCase):
    """Matrix: 'Falha UI/rede, sair e voltar' — cursor is durable."""

    def test_cursor_advances_and_is_exclusive(self):
        op, _ = self.begin(idempotency_key="k1")
        first = self.store.snapshot(0)
        self.assertEqual(len(first["events"]), 1)
        again = self.store.snapshot(first["cursor"])
        self.assertEqual(again["events"], [])
        self.store.set_phase(op, state="preparing")
        after = self.store.snapshot(first["cursor"])
        self.assertEqual(len(after["events"]), 1)
        self.assertEqual(after["events"][0]["state"], "preparing")

    def test_cursor_beyond_the_end_is_harmless(self):
        self.begin(idempotency_key="k1")
        snap = self.store.snapshot(99_999)
        self.assertEqual(snap["events"], [])
        self.assertEqual(snap["cursor"], 99_999)


class FencingTests(StoreTestCase):
    """Contract 2: a stale worker must not promote a newer state."""

    def test_epoch_grows_monotonically_per_tablet(self):
        e1 = self.store.bump_epoch("t1")
        e2 = self.store.bump_epoch("t1")
        self.assertEqual(e2, e1 + 1)
        self.assertEqual(self.store.bump_epoch("t2"), 1)

    def test_stale_epoch_cannot_finish_an_operation(self):
        op, _ = self.begin(idempotency_key="k1")
        epoch = self.store.bump_epoch("t1")
        self.store.set_epoch(op, epoch)
        self.store.bump_epoch("t1")  # a newer worker takes over
        with self.assertRaises(mo.OperationsError):
            self.store.finish(op, state="succeeded", fencing_epoch=epoch)

    def test_current_epoch_can_finish(self):
        op, _ = self.begin(idempotency_key="k1")
        epoch = self.store.bump_epoch("t1")
        self.store.set_epoch(op, epoch)
        done = self.store.finish(op, state="succeeded", fencing_epoch=epoch)
        self.assertEqual(done["state"], "succeeded")

    def test_stale_epoch_cannot_write_a_phase(self):
        """Revision (d): a superseded worker must not report progress either."""
        op, _ = self.begin(idempotency_key="k1")
        epoch = self.store.bump_epoch("t1")
        self.store.set_epoch(op, epoch)
        self.store.bump_epoch("t1")  # superseded
        with self.assertRaises(mo.OperationsError):
            self.store.set_phase(op, state="transferring", fencing_epoch=epoch)
        # The stale write must not have changed anything.
        self.assertEqual(self.store.get(op)["state"], "queued")

    def test_current_epoch_may_write_a_phase(self):
        op, _ = self.begin(idempotency_key="k1")
        epoch = self.store.bump_epoch("t1")
        self.store.set_epoch(op, epoch)
        updated = self.store.set_phase(op, state="transferring",
                                       fencing_epoch=epoch)
        self.assertEqual(updated["state"], "transferring")

    def test_terminal_operation_cannot_be_reopened(self):
        op, _ = self.begin(idempotency_key="k1")
        self.store.finish(op, state="succeeded")
        with self.assertRaises(mo.OperationsError):
            self.store.set_phase(op, state="activating")
        self.assertEqual(self.store.get(op)["state"], "succeeded")

    def test_phase_without_epoch_still_works_for_non_publication_callers(self):
        op, _ = self.begin(idempotency_key="k1")
        self.store.set_phase(op, state="preparing")
        self.assertEqual(self.store.get(op)["state"], "preparing")


class OutboxTests(StoreTestCase):
    """Contract: Telegram must never block a publication."""

    def test_enqueue_is_deduplicated_per_operation_channel_kind(self):
        op, _ = self.begin(idempotency_key="k1")
        self.store.enqueue_notification(op, channel="telegram", kind="terminal",
                                        payload={"text": "a"})
        self.store.enqueue_notification(op, channel="telegram", kind="terminal",
                                        payload={"text": "b"})
        pending = self.store.pending_notifications(channel="telegram")
        self.assertEqual(len(pending), 1)

    def test_failed_send_keeps_the_item_pending_with_attempts(self):
        op, _ = self.begin(idempotency_key="k1")
        self.store.enqueue_notification(op, channel="telegram", kind="terminal",
                                        payload={"text": "x"})
        seq = self.store.pending_notifications(channel="telegram")[0]["seq"]
        self.store.mark_notification(seq, sent=False, error="boom")
        still = self.store.pending_notifications(channel="telegram")
        self.assertEqual(len(still), 1)
        self.assertEqual(still[0]["attempts"], 1)
        self.assertEqual(still[0]["last_error"], "boom")

    def test_successful_send_leaves_the_pending_set(self):
        op, _ = self.begin(idempotency_key="k1")
        self.store.enqueue_notification(op, channel="telegram", kind="terminal",
                                        payload={"text": "x"})
        seq = self.store.pending_notifications(channel="telegram")[0]["seq"]
        self.store.mark_notification(seq, sent=True)
        self.assertEqual(self.store.pending_notifications(channel="telegram"), [])


class DurabilityTests(StoreTestCase):
    def test_schema_is_created_once_and_survives_reopen(self):
        mo.OperationStore(self.path)
        conn = sqlite3.connect(str(self.path))
        try:
            version = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(int(version), mo.SCHEMA_VERSION)

    def test_events_are_ordered_by_durable_sequence(self):
        op, _ = self.begin(idempotency_key="k1")
        self.store.set_phase(op, state="preparing")
        self.store.set_phase(op, state="transferring")
        seqs = [e["seq"] for e in self.store.events_since(0)]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))


class ReconciliationTests(StoreTestCase):
    """Contract 5 + §6: proving resolution must clear the pendency.

    The protection (an ambiguous device holds its slot) must not become a
    permanent deadlock, and the way out must be explicit and recorded.
    """

    def _ambiguous(self):
        op, _ = self.begin(idempotency_key="k1")
        self.store.finish(op, state="needs_reconciliation",
                          user_message="Conexao perdida apos a troca")
        return op

    def test_reconciliation_requires_recorded_proof(self):
        op = self._ambiguous()
        with self.assertRaises(mo.OperationsError):
            self.store.resolve_reconciliation(op, resolution="applied", proof="")
        with self.assertRaises(mo.OperationsError):
            self.store.resolve_reconciliation(op, resolution="applied", proof="curto")

    def test_reconciliation_rejects_an_unknown_resolution(self):
        op = self._ambiguous()
        with self.assertRaises(mo.OperationsError):
            self.store.resolve_reconciliation(op, resolution="talvez",
                                              proof="inspecionei o LUN e o lock")

    def test_reconciliation_only_applies_to_ambiguous_operations(self):
        op, _ = self.begin(idempotency_key="k1")
        self.store.set_phase(op, state="transferring")
        with self.assertRaises(mo.OperationsError):
            self.store.resolve_reconciliation(op, resolution="applied",
                                              proof="inspecionei o LUN e o lock")

    def test_proven_resolution_clears_the_pendency_and_frees_the_slot(self):
        op = self._ambiguous()
        self.assertEqual(len(self.store.unresolved()), 1)
        self.assertIsNotNone(self.store.active_for_tablet("t1"))
        done = self.store.resolve_reconciliation(
            op, resolution="not_applied",
            proof="LUN aponta para ntfs_lab; lock nao pertence a operacao")
        self.assertEqual(done["state"], "failed_safe")
        self.assertEqual(self.store.unresolved(), [])
        self.assertIsNone(self.store.active_for_tablet("t1"))
        # The proof is kept for audit, out of the public row.
        self.assertIn("ntfs_lab", self.store.reconciliation_proof(op) or "")
        self.assertNotIn("proof", done)

    def test_history_survives_reconciliation(self):
        op = self._ambiguous()
        self.store.resolve_reconciliation(
            op, resolution="applied",
            proof="manifesto e LUN conferem com a operacao")
        self.assertEqual(self.store.get(op)["state"], "succeeded")
        kinds = [e["kind"] for e in self.store.events_since(0)]
        self.assertIn("reconciled", kinds)

    def test_a_new_operation_is_accepted_after_reconciliation(self):
        op = self._ambiguous()
        self.store.resolve_reconciliation(
            op, resolution="not_applied", proof="estado observado: nada aplicado")
        op2, created = self.begin(idempotency_key="k2",
                                  intention={"task_id": "b2c3d4e5"})
        self.assertTrue(created)
        self.assertNotEqual(op, op2)

    def test_migration_adds_the_pendency_column_to_an_older_database(self):
        """An existing journal must survive the upgrade, not be discarded."""
        import sqlite3
        other = pathlib.Path(self._dir.name) / "legacy.db"
        conn = sqlite3.connect(str(other))
        conn.execute(
            "CREATE TABLE operations (operation_id TEXT PRIMARY KEY, "
            "tablet_id TEXT, principal TEXT, idempotency_key TEXT, "
            "intention_hash TEXT, state TEXT, stage TEXT, stage_started_at REAL, "
            "created_at REAL, updated_at REAL, heartbeat_at REAL, "
            "bytes_done INTEGER, bytes_total INTEGER, desired_generation INTEGER, "
            "observed_generation INTEGER, error_code TEXT, "
            "user_message TEXT DEFAULT '', retryable INTEGER DEFAULT 0, "
            "reconnected INTEGER DEFAULT 0, next_action TEXT, "
            "fencing_epoch INTEGER DEFAULT 0, expires_at REAL)")
        conn.commit()
        conn.close()
        store = mo.OperationStore(other)  # must migrate, not raise
        cols = {r[1] for r in store._connect().execute("PRAGMA table_info(operations)")}
        self.assertIn("pendency_closed", cols)


if __name__ == "__main__":
    unittest.main()
