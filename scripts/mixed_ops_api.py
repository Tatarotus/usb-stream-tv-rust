#!/usr/bin/env python3
"""Thin HTTP-facing adapter over the operation store (Gerações V2, fase 1).

Why a separate module
=====================

``server.py`` is a large, load-bearing file. Wiring the new operation model into
it directly would mix two risks: the model itself, and the surgery on the
handler. This module is the model's *edge*: it turns an authenticated request
into an operation and an operation into a public payload, and it is fully
testable without a socket, a tablet or ``server.py``.

What it guarantees
==================

* **202 with a durable id.** A new request gets HTTP 202 and an
  ``operation_id`` that was persisted *before* anything is executed. The caller
  launches the work afterwards; the response never waits on adb.
* **Idempotency by key, conflict by intention.** The same key with the same
  intention returns the *same* operation (200, ``duplicate: true``). The same key
  with a different intention returns 409 naming the original. A different
  intention while another operation is active returns 409 with
  ``active_operation_id`` and no inherited authorisation.
* **No secrets on the wire.** The public payload carries no PIN, token, private
  URL or local path. ``operation_id`` is opaque and derived from nothing.
* **Unknown totals stay null.** Never ``0/0`` pretending a progress bar.

What it deliberately does NOT do
================================

It does not decide whether an interruption is allowed, does not talk to the
tablet and does not launch the publisher. Those stay where they are today, so a
bug here cannot change publication behaviour — it can only change what the panel
is told.
"""

from __future__ import annotations

import re

import mixed_operations as ops

# HTTP statuses this adapter can return. Kept explicit so the caller cannot
# invent one by accident.
HTTP_OK = 200
HTTP_ACCEPTED = 202
HTTP_CONFLICT = 409
HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404

# Fields of the stored row that are safe to hand to the panel. Anything else
# (idempotency_key, principal, intention_hash, expires_at, fencing_epoch) is
# internal and must not leak by accident.
PUBLIC_FIELDS = (
    "operation_id",
    "state",
    "stage",
    "stage_started_at",
    "created_at",
    "updated_at",
    "heartbeat_at",
    "bytes_done",
    "bytes_total",
    "desired_generation",
    "observed_generation",
    "error_code",
    "user_message",
    "retryable",
    "reconnected",
    "next_action",
)

_TASK_RE = r"^[0-9a-f]{8}$"


def public_operation(row: dict) -> dict:
    """Strip an operation row down to what the panel may see."""
    payload = {key: row.get(key) for key in PUBLIC_FIELDS}
    # Totals that were never measured must not read as a real 0/0 bar.
    if payload.get("bytes_total") in (0, None):
        payload["percent"] = None
    else:
        done = payload.get("bytes_done") or 0
        payload["percent"] = round(100.0 * done / payload["bytes_total"], 1)
    return payload


def _intention_from(body: dict) -> dict:
    """The part of a request that decides *what* will be done.

    Two requests with the same key but different values here are a conflict, so
    this must contain everything that changes the outcome and nothing that does
    not (a timestamp or a retry counter would break idempotency).
    """
    intention = {}
    for key in ("task_id", "operation", "desired_generation", "inventory_hash",
                "confirm_interrupt"):
        if key in body:
            intention[key] = body[key]
    return intention


def request_apply(store: ops.OperationStore, *, tablet_id: str,
                  principal: str, body: dict) -> tuple[int, dict]:
    """Map a manual apply request to an operation.

    Returns ``(status, payload)``. The caller performs the actual publication
    with the returned ``operation_id``; this function never starts it.
    """
    if not isinstance(body, dict):
        return HTTP_BAD_REQUEST, {"ok": False, "error": "corpo inválido"}

    task_id = body.get("task_id")
    # Default to *apply*: this adapter serves the apply endpoint. A refresh must
    # say so explicitly. Defaulting on the truthiness of task_id made an empty
    # or null id silently become a refresh, which is not what the caller asked
    # for and hid a malformed request.
    operation = body.get("operation") or "apply"
    if operation == "apply":
        if not isinstance(task_id, str) or not re.fullmatch(_TASK_RE, task_id):
            return HTTP_BAD_REQUEST, {"ok": False, "error": "Vídeo inválido"}
    elif operation != "refresh":
        return HTTP_BAD_REQUEST, {"ok": False, "error": "Operação inválida"}

    key = body.get("idempotency_key")
    if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 128):
        return HTTP_BAD_REQUEST, {"ok": False, "error": "Chave inválida"}

    intention = _intention_from(body)
    try:
        op_id, created = store.begin(
            tablet_id=tablet_id, principal=principal, intention=intention,
            idempotency_key=key,
            desired_generation=body.get("desired_generation"),
            user_message="Atualização solicitada")
    except ops.IdempotencyConflict as exc:
        return HTTP_CONFLICT, {
            "ok": False,
            "error": "idempotency_key_reused",
            "operation_id": exc.operation_id,
            "message": "Esta chave já foi usada para outra atualização. "
                       "Use uma chave nova.",
        }
    except ops.TabletBusy as exc:
        return HTTP_CONFLICT, {
            "ok": False,
            "error": "operation_in_progress",
            "active_operation_id": exc.operation_id,
            "message": "Outra atualização está em andamento para esta TV.",
        }

    row = store.get(op_id)
    payload = {
        "ok": True,
        "available": True,
        "operation_id": op_id,
        "duplicate": not created,
        "state": row["state"],
        "message": ("Atualização já em andamento" if not created
                    else "Atualização iniciada"),
    }
    # 202 for a new operation (accepted, not done); 200 for the duplicate that
    # names the same one. The panel distinguishes them without guessing.
    return (HTTP_ACCEPTED if created else HTTP_OK), payload


def request_status(store: ops.OperationStore, *, operation_id: str) -> tuple[int, dict]:
    """Status of a single operation, by id."""
    if not isinstance(operation_id, str) or not operation_id:
        return HTTP_BAD_REQUEST, {"ok": False, "error": "operation_id ausente"}
    try:
        row = store.get(operation_id)
    except ops.UnknownOperation:
        return HTTP_NOT_FOUND, {"ok": False, "error": "operation_not_found"}
    return HTTP_OK, {"ok": True, "operation": public_operation(row)}


def request_events(store: ops.OperationStore, *, cursor: int = 0,
                   limit: int = 200) -> tuple[int, dict]:
    """Events after ``cursor`` plus any problem still pending.

    Pending problems are included regardless of the cursor: a panel that
    reconnects with a stale cursor must still learn about them.
    """
    try:
        cursor = int(cursor or 0)
    except (TypeError, ValueError):
        return HTTP_BAD_REQUEST, {"ok": False, "error": "cursor inválido"}
    snap = store.snapshot(cursor, limit=limit)
    return HTTP_OK, {
        "ok": True,
        "cursor": snap["cursor"],
        "events": [
            {"operation_id": e["operation_id"], "kind": e["kind"],
             "state": e["state"], "stage": e.get("stage"),
             "user_message": e.get("user_message") or "",
             "error_code": e.get("error_code"), "created_at": e["created_at"]}
            for e in snap["events"]
        ],
        "operations": [public_operation(o) for o in snap["operations"]],
        "unresolved": snap["unresolved"],
    }


def request_active(store: ops.OperationStore, *, tablet_id: str) -> tuple[int, dict]:
    """Whatever currently holds the tablet slot, if anything."""
    row = store.active_for_tablet(tablet_id)
    return HTTP_OK, {"ok": True,
                     "operation": public_operation(row) if row else None}
