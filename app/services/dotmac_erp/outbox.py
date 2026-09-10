"""Delivery substrate for the ``field_erp_sync_events`` outbox.

Field-service money-path actions ``enqueue`` an intent here with a stable
idempotency key; ``deliver_pending`` posts each pending row to ERP's existing
``/sync/sub/*`` API and records the terminal outcome.

Two invariants make this safe on the money path:

1. **Single writer per flow.** ``deliver_pending`` refuses to send any flow sub
   does not own in ``sync_flow_ownership`` (it logs and skips — never errors).
   ERP idempotency keys are per-originator-id and CRM/sub use different UUID
   spaces, so a mis-sequenced cutover that let both push would double-post; the
   ownership gate is the control that prevents it.
2. **Idempotent re-delivery.** The idempotency key is stored on the row and sent
   as the ``Idempotency-Key`` header, so re-posting a row ERP already saw is a
   no-op on the ERP side.

The substrate is inert while each flow remains CRM-owned. A flow begins
delivery only after its explicit single-writer cutover assigns ownership to Sub.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    FieldErpSyncStatus,
    flow_owned_by_sub,
)
from app.services.dotmac_erp.client import (
    DotMacERPClient,
    DotMacERPError,
    DotMacERPTransientError,
)
from app.services.integrations.erp_capability import (
    ErpCapabilityClient,
    capability_client,
)

logger = logging.getLogger(__name__)

# Default retry budget before a still-transient row is dead-lettered. Kept small;
# the beat re-runs deliver_pending, so this bounds attempts across runs.
DEFAULT_MAX_ATTEMPTS = 8

# Neutral ERP endpoint used by each Sub-owned flow.
FLOW_ENDPOINTS: dict[str, str] = {
    FieldErpSyncFlow.expense_claim.value: "/api/v1/sync/sub/expense-claims",
    FieldErpSyncFlow.material_request.value: "/api/v1/sync/sub/material-requests",
    FieldErpSyncFlow.purchase_order.value: "/api/v1/sync/sub/purchase-orders",
    FieldErpSyncFlow.purchase_invoice.value: "/api/v1/sync/sub/purchase-invoices",
}

# Response signals that mean ERP made a terminal REJECT decision.
_REJECTED_STATUSES = frozenset(
    {"rejected", "declined", "cancelled", "canceled", "denied"}
)
# Response signals that mean ERP accepted/created the record.
_ACCEPTED_STATUSES = frozenset(
    {"accepted", "approved", "created", "ok", "success", "paid"}
)
# Response keys that carry an ERP-side id (its presence confirms acceptance).
_ERP_ID_KEYS = (
    "id",
    "claim_id",
    "expense_claim_id",
    "request_id",
    "material_request_id",
    "purchase_order_id",
    "purchase_invoice_id",
    "payment_intent_id",
)

_EXPENSE_ACTION_ENDPOINTS = {
    "submit": "/api/v1/sync/sub/expense-claims",
    "approve": "/api/v1/sync/sub/expense-claims/{entity_id}/approve",
    "reject": "/api/v1/sync/sub/expense-claims/{entity_id}/reject",
    "initiate_payment": "/api/v1/sync/sub/expense-claims/{entity_id}/payments",
}


@dataclass
class DeliveryResult:
    processed: int = 0
    accepted: int = 0
    rejected: int = 0
    sent: int = 0
    retried: int = 0
    dead: int = 0
    skipped_not_owned: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "processed": self.processed,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "sent": self.sent,
            "retried": self.retried,
            "dead": self.dead,
            "skipped_not_owned": self.skipped_not_owned,
            "errors": self.errors,
        }


def run_deliver_pending() -> dict[str, object]:
    """Own the background session used by the ERP outbox delivery sweep."""
    from app.db import task_session

    with task_session() as db:
        return deliver_pending(db).as_dict()


def enqueue(
    db: Session,
    *,
    flow: FieldErpSyncFlow | str,
    entity_type: str,
    entity_id: object,
    idempotency_key: str,
    payload: dict,
    isolate: bool = True,
) -> FieldErpSyncEvent:
    """Enqueue (or return the existing) outbox row for ``idempotency_key``.

    Idempotent by the unique idempotency key: a second enqueue with the same key
    returns the row already on file rather than creating a duplicate money-path
    intent. Does NOT deliver — the worker owns delivery/retry state.
    """
    flow_value = flow.value if isinstance(flow, FieldErpSyncFlow) else str(flow)

    existing = (
        db.query(FieldErpSyncEvent)
        .filter(FieldErpSyncEvent.idempotency_key == idempotency_key)
        .first()
    )
    if existing is not None:
        return existing

    event = FieldErpSyncEvent(
        flow=flow_value,
        entity_type=entity_type,
        entity_id=entity_id,
        idempotency_key=idempotency_key,
        payload=payload,
        status=FieldErpSyncStatus.pending.value,
        attempts=0,
    )
    if not isolate:
        # Inside an owner command a helper savepoint is forbidden; the
        # pre-check above dedupes, and a true concurrent race fails the
        # command, whose redelivery returns the winner idempotently.
        db.add(event)
        db.flush()
        return event
    try:
        # Isolate the unique-key race to a savepoint.  A full session rollback
        # here would also discard the source business transition that is meant
        # to commit atomically with this outbox row.
        with db.begin_nested():
            db.add(event)
            db.flush()
    except IntegrityError:
        # Concurrent enqueue of the same key — return the winner (which exists,
        # since the unique-constraint violation means a row is already there).
        winner = (
            db.query(FieldErpSyncEvent)
            .filter(FieldErpSyncEvent.idempotency_key == idempotency_key)
            .first()
        )
        if winner is None:
            raise
        return winner
    return event


def deliver_pending(
    db: Session,
    *,
    client: DotMacERPClient | ErpCapabilityClient | None = None,
    limit: int = 100,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> DeliveryResult:
    """Deliver pending outbox rows to ERP, enforcing single-writer ownership.

    For each pending row whose flow sub owns:
      * increments ``attempts`` and POSTs the payload with the stored key;
      * accepted / rejected / sent are read from the ERP response;
      * a transient error leaves the row ``pending`` (retry next run) until the
        attempt budget is spent, then ``dead``;
      * a permanent error dead-letters the row immediately.

    Rows for flows sub does not own are skipped (logged), never delivered.
    """
    result = DeliveryResult()

    rows = (
        db.query(FieldErpSyncEvent)
        .filter(FieldErpSyncEvent.status == FieldErpSyncStatus.pending.value)
        .order_by(FieldErpSyncEvent.created_at.asc())
        .limit(limit)
        .all()
    )
    if not rows:
        return result

    owned_cache: dict[str, bool] = {}
    owned_client = client
    created_client = False

    try:
        for row in rows:
            owned = owned_cache.get(row.flow)
            if owned is None:
                owned = flow_owned_by_sub(db, row.flow)
                owned_cache[row.flow] = owned
            if not owned:
                # Single-writer guard: CRM (or nobody) still owns this flow.
                # Refuse to send — log and skip, do not error the whole run.
                result.skipped_not_owned += 1
                logger.info(
                    "field_erp_sync: skipping %s event %s — sub does not own flow "
                    "'%s' (sync_flow_ownership)",
                    row.entity_type,
                    row.id,
                    row.flow,
                )
                continue

            endpoint = _endpoint_for(row)
            if endpoint is None:
                _mark_dead(row, f"No ERP endpoint mapped for flow '{row.flow}'")
                result.processed += 1
                result.dead += 1
                continue

            if row.flow == FieldErpSyncFlow.purchase_invoice.value:
                from app.services.dotmac_erp.purchase_invoice_sync import event_ready

                if not event_ready(db, row):
                    # A PO is an ordering prerequisite, not a failed delivery.
                    # Leave the event pending without consuming retry budget.
                    db.commit()
                    continue

            if not _event_prerequisite_ready(db, row):
                # Ordered money-path action: keep it pending without consuming
                # retry budget until the earlier claim action is accepted.
                db.commit()
                continue

            if owned_client is None:
                owned_client = capability_client(db)
                created_client = True

            result.processed += 1
            row.attempts += 1
            try:
                response = owned_client.post(
                    endpoint,
                    _transport_payload(row),
                    idempotency_key=row.idempotency_key,
                    expected_status_codes={200, 201},
                )
            except DotMacERPTransientError as exc:
                _mark_transient(row, exc, max_attempts=max_attempts, result=result)
                db.commit()
                continue
            except DotMacERPError as exc:
                _mark_dead(row, str(exc))
                result.dead += 1
                result.errors.append(f"{row.id}: {exc}")
                db.commit()
                continue

            _apply_response(row, response, result)
            _dispatch_flow_writeback(db, row)
            db.commit()
    finally:
        if created_client and owned_client is not None:
            owned_client.close()

    return result


def _endpoint_for(row: FieldErpSyncEvent) -> str | None:
    if row.flow != FieldErpSyncFlow.expense_claim.value:
        return FLOW_ENDPOINTS.get(row.flow)
    action = str((row.payload or {}).get("_expense_action") or "submit")
    template = _EXPENSE_ACTION_ENDPOINTS.get(action)
    return template.format(entity_id=row.entity_id) if template else None


def _transport_payload(row: FieldErpSyncEvent) -> dict:
    """Remove Sub-only ordering metadata from the typed ERP payload."""
    return {
        key: value
        for key, value in (row.payload or {}).items()
        if not str(key).startswith("_")
    }


def _event_prerequisite_ready(db: Session, row: FieldErpSyncEvent) -> bool:
    prerequisite_key = (row.payload or {}).get("_depends_on_idempotency_key")
    if not prerequisite_key:
        return True
    prerequisite = (
        db.query(FieldErpSyncEvent)
        .filter(FieldErpSyncEvent.idempotency_key == str(prerequisite_key))
        .one_or_none()
    )
    return bool(
        prerequisite and prerequisite.status == FieldErpSyncStatus.accepted.value
    )


def _dispatch_flow_writeback(db: Session, row: FieldErpSyncEvent) -> None:
    """Write a delivered event's ERP response back onto its source row, per flow.

    The outbox stays flow-agnostic: it classifies the response and stores it on
    the event, then dispatches to the owning flow module so the money link (ERP
    claim id / number / status) lands on the source entity. Handlers are looked up
    lazily to keep the outbox free of flow-module import cycles. The material
    support flow is fail-atomic: its source projection must succeed in the same
    commit as the delivered outcome, otherwise the idempotent ERP request is
    retried. Legacy flows retain their existing logged best-effort behavior until
    their own ownership slices migrate.
    Rejected and dead responses are durable failure evidence, never accepted
    provider links. Only accepted responses and non-terminal ``sent`` responses
    may reach a flow-specific write-back owner.
    """
    if row.status not in {
        FieldErpSyncStatus.accepted.value,
        FieldErpSyncStatus.sent.value,
    }:
        return

    if row.flow == FieldErpSyncFlow.expense_claim.value:
        try:
            from app.services.dotmac_erp.expense_sync import apply_erp_response

            apply_erp_response(db, row)
        except Exception:  # noqa: BLE001 — write-back must not fail delivery
            logger.exception(
                "field_erp_sync: write-back failed for %s event %s",
                row.flow,
                row.id,
            )
    elif row.flow == FieldErpSyncFlow.material_request.value:
        from app.services.dotmac_erp.material_sync import apply_erp_response

        apply_erp_response(db, row)
    elif row.flow == FieldErpSyncFlow.purchase_order.value:
        try:
            from app.services.dotmac_erp.purchase_order_sync import apply_erp_response

            apply_erp_response(db, row)
        except Exception:  # noqa: BLE001 — write-back must not fail delivery
            logger.exception(
                "field_erp_sync: write-back failed for %s event %s",
                row.flow,
                row.id,
            )
    elif row.flow == FieldErpSyncFlow.purchase_invoice.value:
        try:
            from app.services.dotmac_erp.purchase_invoice_sync import apply_erp_response

            apply_erp_response(db, row)
        except Exception:  # noqa: BLE001 — write-back must not fail delivery
            logger.exception(
                "field_erp_sync: write-back failed for %s event %s",
                row.flow,
                row.id,
            )


def unlinked_delivered_events(
    db: Session,
    *,
    flow: FieldErpSyncFlow | str,
    limit: int = 100,
) -> list[FieldErpSyncEvent]:
    """Delivered outbox rows for one flow that never reached a status poll.

    A ``sent`` row is, by construction, one whose original POST response
    carried no terminal id — so a poll gated on "the source entity already has
    an ERP reference" can never select it (the dead-end this module's
    docstring warns about). An ``accepted`` row can also belong here: its
    write-back ran in the same transaction as delivery
    (``_dispatch_flow_writeback``) but that call is wrapped in a
    logged-not-raised guard for several flows, so a write-back exception
    leaves the outbox row terminal while the source entity's reference stays
    null.

    Returns rows keyed by SUB'S OWN ``entity_id`` — every ``get_*_status``
    function on the ERP client takes Sub's source id, not an ERP id, so a
    caller never needs the (possibly still-missing) reference column to ask
    ERP for status. The outbox stays flow-agnostic: it hands back rows, not
    an opinion on what a source entity's "reference" field is named — each
    flow module resolves its own source entity and decides whether the
    reference is still null before spending a poll call on it.
    """
    flow_value = flow.value if isinstance(flow, FieldErpSyncFlow) else str(flow)
    limit = max(1, min(int(limit or 100), 500))
    return (
        db.query(FieldErpSyncEvent)
        .filter(FieldErpSyncEvent.flow == flow_value)
        .filter(
            FieldErpSyncEvent.status.in_(
                (FieldErpSyncStatus.sent.value, FieldErpSyncStatus.accepted.value)
            )
        )
        .order_by(FieldErpSyncEvent.created_at.asc())
        .limit(limit)
        .all()
    )


def record_polled_outcome(db: Session, row: FieldErpSyncEvent, response: dict) -> str:
    """Classify a LATER status-poll response through the same path a delivery uses.

    Reuses ``_apply_response`` (the exact classifier ``deliver_pending`` applies
    to the original POST's 2xx body) so a ``sent`` row drains to
    ``accepted``/``rejected`` from exactly one place, then reuses
    ``_dispatch_flow_writeback`` so the source-entity projection is the same
    code whether it runs right after delivery or later from a poll. Does not
    commit — the caller owns the transaction boundary. Returns the row's
    resulting status.
    """
    _apply_response(row, response, DeliveryResult())
    _dispatch_flow_writeback(db, row)
    return row.status


def _apply_response(
    row: FieldErpSyncEvent, response: dict, result: DeliveryResult
) -> None:
    """Classify a 2xx ERP response into accepted / rejected / sent."""
    row.erp_response = response if isinstance(response, dict) else {"raw": response}
    # A poll re-classifying an already-``sent`` row must not overwrite the
    # timestamp of the original delivery with the (later) poll time.
    row.sent_at = row.sent_at or datetime.now(UTC)
    row.last_error = None

    status_signal = _extract_status(response)
    expected_rejection = (
        row.flow == FieldErpSyncFlow.expense_claim.value
        and str((row.payload or {}).get("_expense_action")) == "reject"
        and status_signal in _REJECTED_STATUSES
    )
    if status_signal in _REJECTED_STATUSES and not expected_rejection:
        row.status = FieldErpSyncStatus.rejected.value
        result.rejected += 1
        return
    if (
        expected_rejection
        or status_signal in _ACCEPTED_STATUSES
        or _has_erp_id(response)
    ):
        row.status = FieldErpSyncStatus.accepted.value
        result.accepted += 1
        return
    # Delivered (2xx) but ERP has not returned a terminal decision yet.
    row.status = FieldErpSyncStatus.sent.value
    result.sent += 1


def _mark_transient(
    row: FieldErpSyncEvent,
    exc: Exception,
    *,
    max_attempts: int,
    result: DeliveryResult,
) -> None:
    row.last_error = str(exc)[:2000]
    if row.attempts >= max_attempts:
        row.status = FieldErpSyncStatus.dead.value
        result.dead += 1
        logger.error(
            "field_erp_sync: event %s dead-lettered after %d transient attempts: %s",
            row.id,
            row.attempts,
            exc,
        )
    else:
        # Stays pending for the next worker pass.
        result.retried += 1


def _mark_dead(row: FieldErpSyncEvent, error: str) -> None:
    row.status = FieldErpSyncStatus.dead.value
    row.last_error = error[:2000]
    logger.error(
        "field_erp_sync: event %s dead-lettered (permanent): %s", row.id, error
    )


def _extract_status(response: dict | None) -> str | None:
    if not isinstance(response, dict):
        return None
    if response.get("rejected") is True:
        return "rejected"
    if response.get("accepted") is True:
        return "accepted"
    raw = response.get("status") or response.get("claim_status")
    if not raw:
        return None
    return str(raw).strip().lower().replace("-", "_").replace(" ", "_")


def _has_erp_id(response: dict | None) -> bool:
    if not isinstance(response, dict):
        return False
    return any(response.get(key) for key in _ERP_ID_KEYS)


# ---------------------------------------------------------------------------
# Operator-visible diagnostics — the "nothing alerts" gap this module closes
# ---------------------------------------------------------------------------


def _source_reference_is_null(db: Session, row: FieldErpSyncEvent) -> bool | None:
    """Return True when ``row``'s source entity still has no ERP reference.

    Lazily resolves the flow-specific source model and its reference column —
    mirrors the lazy per-flow lookup already used by ``_dispatch_flow_writeback``
    so the outbox module stays free of feature-module imports at load time.
    Returns ``None`` (excluded from diagnostics) for an unrecognised flow or a
    source row that no longer exists.
    """
    if row.flow == FieldErpSyncFlow.expense_claim.value:
        from app.models.field_expense import FieldExpenseRequest

        expense_request = db.get(FieldExpenseRequest, row.entity_id)
        return (
            expense_request is not None and not expense_request.expense_claim_reference
        )
    if row.flow == FieldErpSyncFlow.material_request.value:
        from app.models.field_material import FieldMaterialRequest

        material_request = db.get(FieldMaterialRequest, row.entity_id)
        return material_request is not None and not material_request.support_reference
    if row.flow == FieldErpSyncFlow.purchase_invoice.value:
        from app.models.vendor_routes import VendorPurchaseInvoice

        invoice = db.get(VendorPurchaseInvoice, row.entity_id)
        return invoice is not None and not invoice.payables_document_reference
    if row.flow == FieldErpSyncFlow.purchase_order.value:
        from app.models.vendor_routes import InstallationProject

        project = db.get(InstallationProject, row.entity_id)
        return project is not None and not project.procurement_order_reference
    return None


def delivered_unlinked_diagnostics(
    db: Session,
    *,
    limit_per_flow: int = 500,
) -> dict[str, dict[str, object]]:
    """Per-flow count + oldest age of delivered outbox rows never linked to their source.

    A row counts here when it is ``sent``/``accepted`` (delivered) AND its
    source entity's own ERP-reference field is still null — the exact
    condition ``unlinked_delivered_events`` selects for repair, surfaced here
    for observability instead of action.

    INFORMATIONAL ONLY — not an alert or an SLA. This reports the raw count
    and the age (in hours) of the oldest delivered-but-unlinked row for EVERY
    flow, regardless of how old that row is. There is deliberately no
    threshold, "stale"/"breach" boolean, or severity here: purchase-order and
    purchase-invoice write-backs affect accounts-payable and expense-claim
    write-backs are payroll-adjacent, so a shared numeric cutoff would be an
    invented, unowned SLA. A human — or a later, separately reviewed change
    that gives each flow its own explicitly owned threshold — decides what
    age is concerning.
    """
    now = datetime.now(UTC)
    report: dict[str, dict[str, object]] = {}
    for flow in FieldErpSyncFlow:
        rows = unlinked_delivered_events(db, flow=flow, limit=limit_per_flow)
        ages_hours: list[float] = []
        for row in rows:
            if not _source_reference_is_null(db, row):
                continue
            created_at = row.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            ages_hours.append((now - created_at).total_seconds() / 3600)
        oldest_age = max(ages_hours) if ages_hours else 0.0
        report[flow.value] = {
            "count": len(ages_hours),
            "oldest_age_hours": round(oldest_age, 2),
        }
    return report
