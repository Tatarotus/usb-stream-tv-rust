#!/usr/bin/env python3
"""Persistent publication operations: durable IDs, idempotency and events.

Why this module exists
======================

The V1 publisher correlates the panel with the server by *timestamp* and keeps
progress in temporary banners. That has two costs, both observed:

* a user who closes and reopens the page loses the failure entirely;
* two clicks in quick succession cannot tell "the same operation" from "a
  different one", so a duplicate either starts a second publication or reports
  a conflict that the panel has no stable name for.

This module gives every publication a durable ``operation_id`` and keeps its
state and its events in one place, so the panel can poll *an operation* instead
of guessing from time. It is storage only: it never talks to the tablet, never
opens a transaction across a call to the network, and never decides whether an
interruption is allowed. Those decisions stay where they are today.

Design rules that matter for correctness
========================================

* One row per operation; the row is written **before** the effect it describes
  and updated **after** it, so a crash in between is visible as an operation
  stuck in a non-terminal phase rather than as silence.
* A terminal result and its event are written in the **same transaction**. If
  the process dies, either both are there or neither is.
* ``idempotency_key`` is scoped to ``(principal, tablet_id)``. The same key with
  the same intention returns the original operation; the same key with a
  *different* intention is a conflict, not a silent reuse.
* At most one operation per tablet may be in a non-terminal state, enforced by a
  partial unique index rather than by application-level hope.
* Events carry a durable sequence number, so a client can resume from a cursor.
  An unresolved problem is always part of the initial snapshot, whatever the
  cursor says.

No SQLite transaction is held while a caller performs its own work: the caller
opens a short transaction, closes it, acts, then opens another.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

# Bumped when the shape changes so an operator can tell which revision a file
# was created by; the additive migrations below bring older files forward.
SCHEMA_VERSION = 2

# Phases a publication moves through, in order. ``ready_to_activate`` is NOT
# "applied": it means the artifacts are in place and the device has not been
# touched yet.
PHASES = (
    "queued",
    "preparing",
    "transferring",
    "validating",
    "ready_to_activate",
    "activating",
    "verifying",
    "succeeded",
)

# Extra outcomes that end an operation away from the happy path.
OUTCOMES = (
    "already_current",
    "failed_safe",
    "blocked",
    "superseded",
    "needs_reconciliation",
    "interrupted",
)

TERMINAL_STATES = frozenset({"succeeded", "already_current", "failed_safe",
                             "blocked", "superseded"})

# States that hold the tablet's single publication slot. ``needs_reconciliation``
# holds it on purpose: an ambiguous device must be reconciled before anything
# else touches it, so a second writer is not allowed to simply proceed.
ACTIVE_STATES = frozenset({"queued", "preparing", "transferring", "validating",
                           "ready_to_activate", "activating", "verifying",
                           "needs_reconciliation", "interrupted"})

# A problem that is still pending must keep showing up until the *same* effect
# is proven resolved. These are the states whose notification is sticky.
UNRESOLVED_STATES = frozenset({"needs_reconciliation", "interrupted",
                               "failed_safe", "blocked"})

DEFAULT_RETENTION_SECONDS = 7 * 24 * 3600


class OperationsError(Exception):
    """Base error for this module."""


class IdempotencyConflict(OperationsError):
    """The same key was reused with a different intention."""

    def __init__(self, operation_id: str):
        super().__init__("idempotency key reused with a different intention")
        self.operation_id = operation_id


class TabletBusy(OperationsError):
    """Another operation already holds this tablet's publication slot."""

    def __init__(self, operation_id: str):
        super().__init__("another operation is active for this tablet")
        self.operation_id = operation_id


class UnknownOperation(OperationsError):
    """No such operation."""


def intention_hash(intention: dict) -> str:
    """Stable digest of what an operation intends to do.

    Sorted keys and no whitespace, so the same request built by two different
    code paths hashes identically. This is what makes a retried POST
    idempotent instead of merely similar.
    """
    blob = json.dumps(intention, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def new_operation_id() -> str:
    """Opaque, sortable-enough identifier. Never derived from a path or a name."""
    return uuid.uuid4().hex


class OperationStore:
    """SQLite-backed store for publication operations and their events."""

    def __init__(self, path: str | os.PathLike, *,
                 retention_seconds: int = DEFAULT_RETENTION_SECONDS,
                 busy_timeout_ms: int = 5000, now=None):
        self.path = Path(path)
        self.retention_seconds = int(retention_seconds)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._now = now or time.time
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ---------------------------------------------------------------- plumbing
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=self.busy_timeout_ms / 1000.0,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=%d" % self.busy_timeout_ms)
        # WAL keeps a reader (the panel polling) from blocking the writer
        # (the worker recording a phase). Both are short transactions.
        if str(self.path) != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _tx(self):
        """One short transaction. Never wrap a network call in this."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    @contextmanager
    def _ro(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        # `executescript` commits any open transaction before running, which
        # would end the explicit BEGIN IMMEDIATE and make our own COMMIT fail
        # with "no transaction is active". Schema creation therefore runs on its
        # own connection, outside `_tx`.
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS operations (
                    operation_id       TEXT PRIMARY KEY,
                    tablet_id          TEXT NOT NULL,
                    principal          TEXT NOT NULL,
                    idempotency_key    TEXT,
                    intention_hash     TEXT NOT NULL,
                    -- Explicit task identity. The intention *hash* cannot be used
                    -- for correlation because callers legitimately build the
                    -- intention differently (the endpoint records
                    -- confirm_interrupt, the publisher does not), so equal work
                    -- would hash differently and a valid adoption would look
                    -- like a conflict.
                    task_id            TEXT,
                    -- The revision the operation reserved. Correlation needs more
                    -- than "an active operation for this tablet": the id alone
                    -- does not say WHICH intention it belongs to.
                    intent_revision    TEXT,
                    -- The execution that proved it owns this operation, recorded
                    -- by compare-and-swap. Without it, "the worker that ran" is
                    -- an assumption, not an association.
                    executor           TEXT,
                    executor_claimed_at REAL,
                    state              TEXT NOT NULL,
                    stage              TEXT,
                    stage_started_at   REAL,
                    created_at         REAL NOT NULL,
                    updated_at         REAL NOT NULL,
                    heartbeat_at       REAL,
                    bytes_done         INTEGER,
                    bytes_total        INTEGER,
                    desired_generation INTEGER,
                    observed_generation INTEGER,
                    error_code         TEXT,
                    user_message       TEXT NOT NULL DEFAULT '',
                    retryable          INTEGER NOT NULL DEFAULT 0,
                    reconnected        INTEGER NOT NULL DEFAULT 0,
                    next_action        TEXT,
                    fencing_epoch      INTEGER NOT NULL DEFAULT 0,
                    expires_at         REAL,
                    -- Set only by an explicit reconciliation. Dismissing a banner
                    -- must not clear a real problem, and proving resolution must
                    -- actually clear it: the two are different columns.
                    pendency_closed    INTEGER NOT NULL DEFAULT 0
                );

                -- One key per principal+tablet. The intention hash is what
                -- turns "same key" into "same operation" or "conflict".
                CREATE UNIQUE INDEX IF NOT EXISTS ux_operations_idem
                    ON operations (principal, tablet_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;

                -- At most one non-terminal operation per tablet. Enforced by
                -- the database, not by a read-then-write race in Python.
                CREATE UNIQUE INDEX IF NOT EXISTS ux_operations_active
                    ON operations (tablet_id)
                    WHERE state IN ('queued','preparing','transferring',
                                    'validating','ready_to_activate',
                                    'activating','verifying',
                                    'needs_reconciliation','interrupted');

                CREATE TABLE IF NOT EXISTS events (
                    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id  TEXT NOT NULL,
                    kind          TEXT NOT NULL,
                    state         TEXT NOT NULL,
                    stage         TEXT,
                    user_message  TEXT NOT NULL DEFAULT '',
                    error_code    TEXT,
                    created_at    REAL NOT NULL,
                    FOREIGN KEY (operation_id) REFERENCES operations(operation_id)
                );

                CREATE INDEX IF NOT EXISTS ix_events_operation
                    ON events (operation_id, seq);

                -- Dispatch of the optional Telegram outbox is separate from
                -- publication, so a failed send can never block a publication.
                CREATE TABLE IF NOT EXISTS outbox (
                    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL,
                    channel      TEXT NOT NULL,
                    kind         TEXT NOT NULL,
                    payload      TEXT NOT NULL,
                    created_at   REAL NOT NULL,
                    attempts     INTEGER NOT NULL DEFAULT 0,
                    sent_at      REAL,
                    last_error   TEXT,
                    UNIQUE (operation_id, channel, kind)
                );
                """
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO NOTHING", (str(SCHEMA_VERSION),))
            # Databases created before the pendency flag gain it here, so an
            # existing journal is not lost when the module is upgraded.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(operations)")}
            if "pendency_closed" not in cols:
                conn.execute("ALTER TABLE operations ADD COLUMN "
                             "pendency_closed INTEGER NOT NULL DEFAULT 0")
            if "task_id" not in cols:
                conn.execute("ALTER TABLE operations ADD COLUMN task_id TEXT")
            if "intent_revision" not in cols:
                conn.execute("ALTER TABLE operations ADD COLUMN intent_revision TEXT")
            if "executor" not in cols:
                conn.execute("ALTER TABLE operations ADD COLUMN executor TEXT")
            if "executor_claimed_at" not in cols:
                conn.execute("ALTER TABLE operations ADD COLUMN "
                             "executor_claimed_at REAL")
            # An older file keeps the version it was created with otherwise, and
            # an operator reading `schema_version` would be told the wrong shape.
            conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version' "
                "AND CAST(value AS INTEGER) < ?", (str(SCHEMA_VERSION), SCHEMA_VERSION))
        finally:
            conn.close()

    # ------------------------------------------------------------------ writes
    def begin(self, *, tablet_id: str, principal: str, intention: dict,
              idempotency_key: str | None = None, desired_generation: int | None = None,
              state: str = "queued", stage: str | None = None,
              user_message: str = "") -> tuple[str, bool]:
        """Create an operation, or return the existing one for the same key.

        Returns ``(operation_id, created)``. Raises :class:`IdempotencyConflict`
        when the key exists with a different intention, and :class:`TabletBusy`
        when a *different* operation already holds the tablet slot.
        """
        digest = intention_hash(intention)
        # Explicit identity, kept alongside the hash so a reader can correlate by
        # task without depending on how each caller built the intention.
        task_id = intention.get("task_id") if isinstance(intention, dict) else None
        task_id = str(task_id) if task_id else None
        # The revision is part of the identity of the intention, not decoration:
        # a worker that adopts an operation for a DIFFERENT revision would attach
        # its result to someone else's target.
        revision = (intention.get("revision") if isinstance(intention, dict) else None)
        if revision is None and desired_generation is not None:
            revision = desired_generation
        revision = str(revision) if revision is not None else None
        now = self._now()
        op_id = new_operation_id()
        try:
            with self._tx() as conn:
                if idempotency_key is not None:
                    row = conn.execute(
                        "SELECT operation_id, intention_hash, expires_at FROM operations "
                        "WHERE principal=? AND tablet_id=? AND idempotency_key=?",
                        (principal, tablet_id, idempotency_key)).fetchone()
                    if row is not None:
                        # Expired keys are rejected, never silently recycled into
                        # a new activation: the caller must use a fresh key.
                        expires_at = row["expires_at"]
                        if expires_at is not None and expires_at <= now:
                            raise IdempotencyConflict(row["operation_id"])
                        if row["intention_hash"] != digest:
                            raise IdempotencyConflict(row["operation_id"])
                        return row["operation_id"], False
                # Checked inside the same transaction. BEGIN IMMEDIATE already
                # serialises writers, so this SELECT sees any concurrent commit
                # and the reported id is the real owner. Re-reading after an
                # IntegrityError would race with the other operation finishing
                # and could report an empty id.
                busy = conn.execute(
                    "SELECT operation_id FROM operations WHERE tablet_id=? "
                    "AND state IN (%s) ORDER BY created_at LIMIT 1"
                    % ",".join("?" for _ in sorted(ACTIVE_STATES)),
                    (tablet_id, *sorted(ACTIVE_STATES))).fetchone()
                if busy is not None:
                    raise TabletBusy(busy["operation_id"])
                conn.execute(
                    """INSERT INTO operations (
                           operation_id, tablet_id, principal, idempotency_key,
                           intention_hash, task_id, intent_revision, state, stage,
                           stage_started_at, created_at, updated_at, heartbeat_at,
                           desired_generation, user_message, expires_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (op_id, tablet_id, principal, idempotency_key, digest,
                     task_id, revision, state, stage, now, now, now, now,
                     desired_generation, user_message, None))
                conn.execute(
                    "INSERT INTO events (operation_id, kind, state, stage, "
                    "user_message, created_at) VALUES (?,?,?,?,?,?)",
                    (op_id, "created", state, stage, user_message, now))
        except sqlite3.IntegrityError as exc:
            # The partial unique index is the last line of defence: if it ever
            # fires despite the in-transaction check, something is wrong with
            # the caller's transaction discipline. Do not guess an id.
            raise TabletBusy("") from exc
        return op_id, True

    def claim_execution(self, operation_id: str, *, task_id: str | None,
                        revision: str | None, executor: str,
                        steal_dead: bool = False) -> dict:
        """Bind ONE execution to this operation, by compare-and-swap.

        Why the tablet's single active slot is not enough: it proves only that
        *some* operation holds the slot, never that this worker, for this task and
        this revision, is the one that reserved it. Without this CAS a worker
        could attach its result to another intention's operation and the panel
        would show a success that belongs to something else.

        The claim is idempotent for the SAME executor (a retry inside one run
        re-claims its own operation) and refused for a different one, so a stale
        or parallel worker cannot hijack the row.

        Raises :class:`OperationsError` when the operation does not belong to the
        claimed intention.
        """
        if not executor:
            raise OperationsError("an execution identity is required")
        now = self._now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT state, task_id, intent_revision, executor, tablet_id "
                "FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                raise UnknownOperation(operation_id)
            if row["state"] in TERMINAL_STATES:
                raise OperationsError("operation is already terminal: %s" % row["state"])
            if task_id is not None and str(row["task_id"] or "") != str(task_id):
                raise OperationsError("operation belongs to another task")
            # A NULL recorded revision means the creator did not state one, so
            # there is nothing to compare against: the task id and the executor
            # CAS still have to agree. When both sides DO state a revision, a
            # mismatch is a refusal — that is the case that would attach this
            # run's result to another revision's target.
            if revision is not None and row["intent_revision"] is not None \
                    and str(row["intent_revision"]) != str(revision):
                raise OperationsError("operation belongs to another revision")
            current = row["executor"]
            if current is not None and str(current) != str(executor):
                if not steal_dead:
                    raise OperationsError("operation is owned by another execution")
                # The caller proved the recorded execution is GONE (its process
                # does not exist any more). Without this, an OOM/SIGKILL/reboot
                # during the publication left the row active for ever: 'preparing'
                # holds the tablet's single slot, is not an "unresolved" state, and
                # no API could close it ('resolve_reconciliation' demands
                # needs_reconciliation/interrupted). The proof stays with the
                # caller because only it can look at /proc.
                now = self._now()
                epoch_row = conn.execute(
                    "SELECT value FROM meta WHERE key=?",
                    ("epoch:%s" % row["tablet_id"],)).fetchone()
                fencing_bump = int(epoch_row["value"]) + 1 if epoch_row else 1
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("epoch:%s" % row["tablet_id"], str(fencing_bump)))
                conn.execute(
                    "UPDATE operations SET executor=?, executor_claimed_at=?, "
                    "updated_at=?, error_code=? WHERE operation_id=?",
                    (str(executor), now, now,
                     "previous_execution_dead", operation_id))
            conn.execute(
                "UPDATE operations SET executor=?, executor_claimed_at=?, "
                "updated_at=? WHERE operation_id=?",
                (str(executor), now, now, operation_id))
        return self.get(operation_id)

    def abandon_dead_execution(self, operation_id: str, *, proof: str,
                               expected_executor: str) -> dict:
        """Close an operation whose worker died, freeing the tablet slot.

        Only reachable with ``proof``, and only from a state where the worker had
        not reported progress yet (``queued``/``preparing``). The result is a
        VISIBLE pendency (``failed_safe``), not silence: an OOM/SIGKILL during
        preparation means nobody knows whether the device was touched, so it must
        be shown and reconciled by a human - but it must not hold the tablet
        hostage from every later request.

        This is the door the endpoint uses when a NEW request finds the slot
        occupied by an executor that is provably gone.
        """
        if not isinstance(proof, str) or len(proof.strip()) < 8:
            raise OperationsError("abandoning an operation requires recorded proof")
        if not expected_executor:
            raise OperationsError("abandoning requires the executor that was proven dead")
        now = self._now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT state, executor FROM operations WHERE operation_id=?",
                (operation_id,)).fetchone()
            if row is None:
                raise UnknownOperation(operation_id)
            if row["state"] not in ("queued", "preparing"):
                raise OperationsError(
                    "only an operation that never reported progress can be "
                    "abandoned (state=%s)" % row["state"])
            # Compare-and-swap on the OWNER: the caller proved a specific
            # execution is dead, so only THAT one may be abandoned. Without this
            # the proof was a free-text argument and a live owner could be
            # evicted by a caller that lied - the guard has to live where the
            # damage would happen, not only in the HTTP path.
            current = row["executor"]
            if current is not None and str(current) != str(expected_executor):
                raise OperationsError(
                    "the operation is owned by a different execution now; "
                    "refusing to abandon it")
            conn.execute(
                "UPDATE operations SET state='failed_safe', updated_at=?, "
                "heartbeat_at=?, error_code='executor_dead', retryable=1, "
                "user_message=?, expires_at=? WHERE operation_id=?",
                (now, now,
                 "O worker desta atualização parou de existir; o resultado é "
                 "desconhecido. Nada foi confirmado na TV.",
                 now + self.retention_seconds, operation_id))
            conn.execute(
                "INSERT INTO events (operation_id, kind, state, user_message, "
                "error_code, created_at) VALUES (?,?,?,?,?,?)",
                (operation_id, "terminal", "failed_safe",
                 "Worker morto durante a preparação; reconciliar.",
                 "executor_dead", now))
        self._record_proof(operation_id, proof)
        return self.get(operation_id)

    def set_phase(self, operation_id: str, *, fencing_epoch: int | None = None,
                  state: str, stage: str | None = None,
                  user_message: str | None = None, bytes_done: int | None = None,
                  bytes_total: int | None = None,
                  observed_generation: int | None = None,
                  error_code: str | None = None,
                  retryable: bool | None = None, reconnected: bool | None = None,
                  next_action: str | None = None, emit_event: bool = True) -> dict:
        """Record a phase transition. Call **before** the effect it describes.

        ``fencing_epoch`` is optional so non-publication callers stay simple,
        but the publication path must pass it: a worker whose epoch was
        superseded would otherwise keep writing phases it no longer owns. A
        terminal operation is never re-opened by a late writer.
        """
        if state not in PHASES and state not in OUTCOMES:
            raise OperationsError("unknown state: %s" % state)
        now = self._now()
        sets = ["state=?", "updated_at=?", "heartbeat_at=?"]
        args: list = [state, now, now]
        if stage is not None:
            sets += ["stage=?", "stage_started_at=?"]
            args += [stage, now]
        if user_message is not None:
            sets.append("user_message=?")
            args.append(user_message)
        if bytes_done is not None:
            sets.append("bytes_done=?")
            args.append(int(bytes_done))
        if bytes_total is not None:
            sets.append("bytes_total=?")
            args.append(int(bytes_total))
        if observed_generation is not None:
            sets.append("observed_generation=?")
            args.append(int(observed_generation))
        if error_code is not None:
            sets.append("error_code=?")
            args.append(error_code)
        if retryable is not None:
            sets.append("retryable=?")
            args.append(1 if retryable else 0)
        if reconnected is not None:
            sets.append("reconnected=?")
            args.append(1 if reconnected else 0)
        if next_action is not None:
            sets.append("next_action=?")
            args.append(next_action)
        args.append(operation_id)
        with self._tx() as conn:
            current = conn.execute(
                "SELECT state, fencing_epoch FROM operations WHERE operation_id=?",
                (operation_id,)).fetchone()
            if current is None:
                raise UnknownOperation(operation_id)
            if fencing_epoch is not None \
                    and int(fencing_epoch) != int(current["fencing_epoch"]):
                raise OperationsError("stale fencing epoch")
            if current["state"] in TERMINAL_STATES:
                raise OperationsError("operation is already terminal")
            cur = conn.execute(
                "UPDATE operations SET %s WHERE operation_id=?" % ",".join(sets), args)
            if cur.rowcount == 0:
                raise UnknownOperation(operation_id)
            if emit_event:
                conn.execute(
                    "INSERT INTO events (operation_id, kind, state, stage, "
                    "user_message, error_code, created_at) VALUES (?,?,?,?,?,?,?)",
                    (operation_id, "phase", state, stage,
                     user_message if user_message is not None
                     else self._message_of(conn, operation_id),
                     error_code, now))
        return self.get(operation_id)

    @staticmethod
    def _message_of(conn, operation_id: str) -> str:
        row = conn.execute("SELECT user_message FROM operations WHERE operation_id=?",
                           (operation_id,)).fetchone()
        return row["user_message"] if row else ""

    def finish(self, operation_id: str, *, state: str, user_message: str = "",
               error_code: str | None = None, retryable: bool = False,
               reconnected: bool = False, next_action: str | None = None,
               fencing_epoch: int | None = None) -> dict:
        """Write the terminal result **and its event in one transaction**.

        This is the crash-consistency point: a client that reads the operation
        after a crash sees either the pre-terminal row or the finished row plus
        its event, never a finished row whose notification was lost.
        """
        if state not in TERMINAL_STATES and state not in ("needs_reconciliation",
                                                          "interrupted"):
            raise OperationsError("not a finishing state: %s" % state)
        now = self._now()
        with self._tx() as conn:
            if fencing_epoch is not None:
                row = conn.execute(
                    "SELECT fencing_epoch FROM operations WHERE operation_id=?",
                    (operation_id,)).fetchone()
                if row is None:
                    raise UnknownOperation(operation_id)
                # A stale worker (older epoch) must not promote a newer state.
                if int(row["fencing_epoch"]) > int(fencing_epoch):
                    raise OperationsError("stale fencing epoch")
            cur = conn.execute(
                """UPDATE operations SET state=?, updated_at=?, heartbeat_at=?,
                       user_message=?, error_code=?, retryable=?, reconnected=?,
                       next_action=?, expires_at=?
                   WHERE operation_id=?""",
                (state, now, now, user_message, error_code,
                 1 if retryable else 0, 1 if reconnected else 0, next_action,
                 now + self.retention_seconds, operation_id))
            if cur.rowcount == 0:
                raise UnknownOperation(operation_id)
            conn.execute(
                "INSERT INTO events (operation_id, kind, state, user_message, "
                "error_code, created_at) VALUES (?,?,?,?,?,?)",
                (operation_id, "terminal", state, user_message, error_code, now))
        return self.get(operation_id)

    def heartbeat(self, operation_id: str) -> None:
        """Liveness only. Never changes state, so it cannot mask a stall."""
        with self._tx() as conn:
            conn.execute("UPDATE operations SET heartbeat_at=? WHERE operation_id=?",
                         (self._now(), operation_id))

    def resolve_reconciliation(self, operation_id: str, *, resolution: str,
                                proof: str, user_message: str = "") -> dict:
        """Close an ambiguous operation **explicitly**, with recorded proof.

        ``needs_reconciliation`` holds the tablet slot on purpose so no second
        writer touches a device whose state is unknown. That protection must not
        become a permanent deadlock, so this is the only way out — and it
        demands ``proof`` describing what was observed (LUN, lock, artifacts).
        It is never automatic: neither a clock nor a heartbeat releases a slot.
        """
        if resolution not in ("applied", "not_applied"):
            raise OperationsError("resolution must be 'applied' or 'not_applied'")
        if not isinstance(proof, str) or len(proof.strip()) < 8:
            raise OperationsError("reconciliation requires recorded proof")
        now = self._now()
        final_state = "succeeded" if resolution == "applied" else "failed_safe"
        message = user_message or (
            "Estado da TV reconciliado: operacao aplicada." if resolution == "applied"
            else "Estado da TV reconciliado: operacao nao aplicada.")
        with self._tx() as conn:
            row = conn.execute(
                "SELECT state FROM operations WHERE operation_id=?",
                (operation_id,)).fetchone()
            if row is None:
                raise UnknownOperation(operation_id)
            if row["state"] not in ("needs_reconciliation", "interrupted"):
                raise OperationsError("operation is not awaiting reconciliation")
            conn.execute(
                "UPDATE operations SET state=?, updated_at=?, heartbeat_at=?, "
                "user_message=?, retryable=?, expires_at=?, pendency_closed=1 "
                "WHERE operation_id=?",
                (final_state, now, now, message,
                 1 if resolution == "not_applied" else 0,
                 now + self.retention_seconds, operation_id))
            conn.execute(
                "INSERT INTO events (operation_id, kind, state, user_message, "
                "created_at) VALUES (?,?,?,?,?)",
                (operation_id, "reconciled", final_state, message, now))
        self._record_proof(operation_id, proof)
        return self.get(operation_id)

    def _record_proof(self, operation_id: str, proof: str) -> None:
        """Persist the sanitized reason a reconciliation was accepted.

        Kept out of the public row so it never reaches the panel by accident,
        but durable so the decision can be audited later.
        """
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("reconcile_proof:%s" % operation_id, proof[:1000]))

    def reconciliation_proof(self, operation_id: str) -> str | None:
        with self._ro() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?",
                               ("reconcile_proof:%s" % operation_id,)).fetchone()
        return row["value"] if row else None

    def bump_epoch(self, tablet_id: str) -> int:
        """Advance the tablet's fencing epoch and return the new value.

        The epoch is the arbitration token: a command carrying an older epoch
        belongs to a superseded worker and must be refused.
        """
        key = "epoch:%s" % tablet_id
        with self._tx() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            epoch = int(row["value"]) + 1 if row else 1
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(epoch)))
            conn.execute(
                "UPDATE operations SET fencing_epoch=? WHERE tablet_id=? "
                "AND state IN ('queued','preparing','transferring','validating',"
                "'ready_to_activate','activating','verifying')",
                (epoch, tablet_id))
        return epoch

    def set_epoch(self, operation_id: str, epoch: int) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE operations SET fencing_epoch=? WHERE operation_id=?",
                         (int(epoch), operation_id))

    # ------------------------------------------------------------------- reads
    def identity(self) -> dict:
        """Which FILE this store actually is: path, dev:inode and instance id.

        Two sides that believe they share a database while pointing at different
        files is the defect this method exists to make visible: the path alone
        proves nothing (the host and the container legitimately see different
        prefixes for the same inode), so the device:inode pair is reported, plus a
        random id recorded once at creation. Comparing either across the two
        sides is a fact; comparing paths is an assumption.
        """
        info = {"path": str(self.path), "device": None, "inode": None,
                "instance_id": None, "schema_version": SCHEMA_VERSION}
        if str(self.path) != ":memory:":
            try:
                stat = self.path.stat()
                info["device"] = int(stat.st_dev)
                info["inode"] = int(stat.st_ino)
            except OSError:
                return info
        with self._tx() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='db_instance_id'")
            found = row.fetchone()
            if found is None:
                value = uuid.uuid4().hex
                conn.execute("INSERT INTO meta (key, value) VALUES "
                             "('db_instance_id', ?)", (value,))
                info["instance_id"] = value
            else:
                info["instance_id"] = found["value"]
        return info

    def get(self, operation_id: str) -> dict:
        with self._ro() as conn:
            row = conn.execute("SELECT * FROM operations WHERE operation_id=?",
                               (operation_id,)).fetchone()
        if row is None:
            raise UnknownOperation(operation_id)
        return self._row_to_public(row)

    def find_by_key(self, *, principal: str, tablet_id: str,
                    idempotency_key: str) -> dict | None:
        with self._ro() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE principal=? AND tablet_id=? "
                "AND idempotency_key=?", (principal, tablet_id,
                                          idempotency_key)).fetchone()
        return self._row_to_public(row) if row else None

    def active_for_tablet(self, tablet_id: str) -> dict | None:
        with self._ro() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE tablet_id=? AND state IN "
                "('queued','preparing','transferring','validating',"
                "'ready_to_activate','activating','verifying',"
                "'needs_reconciliation','interrupted') ORDER BY created_at LIMIT 1",
                (tablet_id,)).fetchone()
        return self._row_to_public(row) if row else None

    def events_since(self, cursor: int = 0, *, limit: int = 200) -> list[dict]:
        with self._ro() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?",
                (int(cursor), int(limit))).fetchall()
        return [dict(r) for r in rows]

    def unresolved(self) -> list[dict]:
        """Problems still pending, independent of any client cursor.

        A client that reconnects with a stale cursor must still learn about an
        unresolved problem, so this is merged into the initial snapshot rather
        than left to the event window. An operation whose pendency was closed by
        an explicit reconciliation is no longer pending — otherwise proving the
        resolution would change nothing on screen.
        """
        with self._ro() as conn:
            rows = conn.execute(
                "SELECT * FROM operations WHERE pendency_closed=0 AND state IN "
                "('needs_reconciliation','interrupted','failed_safe','blocked') "
                "ORDER BY updated_at DESC").fetchall()
        return [self._row_to_public(r) for r in rows]

    def snapshot(self, cursor: int = 0, *, limit: int = 200) -> dict:
        """What a freshly opened panel needs: cursor + pending problems.

        Read on one connection inside a single read transaction, so the event
        window and the unresolved list describe the same instant. Reading them
        separately could show an operation as unresolved while its terminal
        event is already past the cursor, or the reverse.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            rows = conn.execute(
                "SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?",
                (int(cursor), int(limit))).fetchall()
            events = [dict(r) for r in rows]
            next_cursor = events[-1]["seq"] if events else int(cursor)
            unresolved_rows = conn.execute(
                "SELECT * FROM operations WHERE pendency_closed=0 AND state IN "
                "('needs_reconciliation','interrupted','failed_safe','blocked') "
                "ORDER BY updated_at DESC").fetchall()
            unresolved = [self._row_to_public(r) for r in unresolved_rows]
            seen = {op["operation_id"] for op in unresolved}
            operations = list(unresolved)
            for event in events:
                op_id = event["operation_id"]
                if op_id in seen:
                    continue
                row = conn.execute("SELECT * FROM operations WHERE operation_id=?",
                                   (op_id,)).fetchone()
                if row is not None:
                    operations.append(self._row_to_public(row))
                    seen.add(op_id)
            conn.execute("COMMIT")
        finally:
            conn.close()
        return {"cursor": next_cursor,
                "events": events,
                "operations": operations,
                "unresolved": [o["operation_id"] for o in unresolved]}

    @staticmethod
    def _row_to_public(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["retryable"] = bool(data.get("retryable"))
        data["reconnected"] = bool(data.get("reconnected"))
        # Totals that were never measured stay null: never invent a percentage.
        return data

    # ---------------------------------------------------------------- outbox
    def enqueue_notification(self, operation_id: str, *, channel: str, kind: str,
                             payload: dict) -> None:
        """Queue an optional notification. Never blocks the publication."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO outbox (operation_id, channel, kind, payload, created_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(operation_id, channel, kind) "
                "DO NOTHING",
                (operation_id, channel, kind,
                 json.dumps(payload, sort_keys=True, ensure_ascii=False),
                 self._now()))

    def pending_notifications(self, *, channel: str, limit: int = 50) -> list[dict]:
        with self._ro() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE channel=? AND sent_at IS NULL "
                "ORDER BY seq LIMIT ?", (channel, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def mark_notification(self, seq: int, *, sent: bool, error: str = "") -> None:
        with self._tx() as conn:
            if sent:
                conn.execute(
                    "UPDATE outbox SET sent_at=?, attempts=attempts+1, last_error=NULL "
                    "WHERE seq=?", (self._now(), int(seq)))
            else:
                conn.execute(
                    "UPDATE outbox SET attempts=attempts+1, last_error=? WHERE seq=?",
                    (error[:200], int(seq)))

    # ------------------------------------------------------------- maintenance
    def expire_old_keys(self) -> int:
        """Drop idempotency keys past retention so they cannot be reused.

        The row itself stays (the operation is history); only the key is
        released, and the operation keeps its own ``expires_at``.
        """
        now = self._now()
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE operations SET idempotency_key=NULL "
                "WHERE idempotency_key IS NOT NULL AND expires_at IS NOT NULL "
                "AND expires_at <= ?", (now,))
            return cur.rowcount
