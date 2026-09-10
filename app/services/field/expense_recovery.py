"""Explicit, append-only recovery for dead approved-expense deliveries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    FieldErpSyncStatus,
)
from app.models.field_expense import FieldExpenseRequest
from app.services.domain_errors import DomainError
from app.services.dotmac_erp.client import DotMacERPError
from app.services.dotmac_erp.outbox import enqueue
from app.services.field.expense_requests import (
    resolve_authoritative_expense_category_rules,
    validate_expense_receipt_delivery,
)
from app.services.integrations.erp_capability import capability_client
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

RECOVERY_CONTRACT_VERSION = "expense-delivery-recovery.v1"


class ExpenseDeliveryRecoveryError(DomainError):
    pass


@dataclass(frozen=True, slots=True)
class PreviewExpenseDeliveryRecovery:
    dead_event_id: UUID


@dataclass(frozen=True, slots=True)
class ExpenseDeliveryRecoveryPreview:
    dead_event_id: UUID
    expense_request_id: UUID
    replacement_idempotency_key: str
    fingerprint: str
    erp_claim_status: str | None


@dataclass(frozen=True, slots=True)
class RecoverExpenseDelivery:
    context: CommandContext
    dead_event_id: UUID
    preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class ExpenseDeliveryRecoveryOutcome:
    original_event_id: UUID
    replacement_event_id: UUID
    replacement_idempotency_key: str
    replayed: bool


_RECOVER_EXPENSE_DELIVERY = OwnerCommandDefinition(
    owner="operations.expense_requests",
    concern="dead expense delivery recovery",
    name="recover_dead_expense_delivery",
)


def _replacement_key(event: FieldErpSyncEvent) -> str:
    return f"{event.idempotency_key}:{RECOVERY_CONTRACT_VERSION}"


def _load_recoverable(
    db: Session,
    event_id: UUID,
    *,
    lock: bool,
) -> tuple[FieldErpSyncEvent, FieldExpenseRequest]:
    query = db.query(FieldErpSyncEvent).filter(FieldErpSyncEvent.id == event_id)
    if lock:
        query = query.with_for_update()
    event = query.one_or_none()
    if (
        event is None
        or event.flow != FieldErpSyncFlow.expense_claim.value
        or event.status != FieldErpSyncStatus.dead.value
        or str((event.payload or {}).get("_expense_action")) != "release_approved_v2"
    ):
        raise ExpenseDeliveryRecoveryError(
            code="operations.expense_requests.recovery_not_available",
            message="The event is not a recoverable dead expense delivery.",
        )
    request = db.get(FieldExpenseRequest, event.entity_id)
    if (
        request is None
        or request.status != "approved"
        or request.approved_at is None
        or request.work_order_mirror is None
        or not request.work_order_mirror.is_active
    ):
        raise ExpenseDeliveryRecoveryError(
            code="operations.expense_requests.recovery_state_invalid",
            message="The expense or work-order approval evidence is no longer valid.",
        )
    return event, request


def _erp_status(db: Session, request_id: UUID) -> str | None:
    try:
        with capability_client(db) as client:
            observed = client.get_expense_claim_status(str(request_id))
    except DotMacERPError as exc:
        raise ExpenseDeliveryRecoveryError(
            code="operations.expense_requests.recovery_erp_unavailable",
            message="ERP state could not be verified for recovery.",
        ) from exc
    if observed is None:
        return None
    raw = observed.get("claim_status") or observed.get("status")
    status = str(raw or "").strip().lower()
    if status not in {"draft", "submitted", "pending_approval"}:
        raise ExpenseDeliveryRecoveryError(
            code="operations.expense_requests.recovery_ambiguous",
            message="ERP state does not permit an unambiguous expense recovery.",
        )
    return status


def _preview(
    db: Session,
    event_id: UUID,
    *,
    lock: bool,
) -> ExpenseDeliveryRecoveryPreview:
    event, request = _load_recoverable(db, event_id, lock=lock)
    rules = resolve_authoritative_expense_category_rules(db)
    validate_expense_receipt_delivery(db, request, category_rules=rules)
    erp_status = _erp_status(db, request.id)
    approved_at = request.approved_at
    if approved_at is None:
        raise ExpenseDeliveryRecoveryError(
            code="operations.expense_requests.recovery_ambiguous",
            message="Only an approved expense can be recovered.",
        )
    evidence = {
        "contract_version": RECOVERY_CONTRACT_VERSION,
        "event_id": str(event.id),
        "event_updated_at": event.updated_at.isoformat(),
        "expense_request_id": str(request.id),
        "expense_updated_at": request.updated_at.isoformat(),
        "approved_at": approved_at.isoformat(),
        "work_order_id": request.work_order_mirror.public_id,
        "erp_claim_status": erp_status,
        "lines": [
            {
                "id": str(item.id),
                "category": item.category_code,
                "attachment_id": (
                    str(item.receipt_attachment_id)
                    if item.receipt_attachment_id is not None
                    else None
                ),
                "receipt_url": item.receipt_url,
            }
            for item in request.items
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    replacement_key = _replacement_key(event)
    return ExpenseDeliveryRecoveryPreview(
        dead_event_id=event.id,
        expense_request_id=request.id,
        replacement_idempotency_key=replacement_key,
        fingerprint=fingerprint,
        erp_claim_status=erp_status,
    )


def preview_expense_delivery_recovery(
    db: Session,
    query: PreviewExpenseDeliveryRecovery,
) -> ExpenseDeliveryRecoveryPreview:
    return _preview(db, query.dead_event_id, lock=False)


def recover_expense_delivery(
    db: Session,
    *,
    command: RecoverExpenseDelivery,
) -> ExpenseDeliveryRecoveryOutcome:
    def operation() -> ExpenseDeliveryRecoveryOutcome:
        preview = _preview(db, command.dead_event_id, lock=True)
        if preview.fingerprint != command.preview_fingerprint:
            raise ExpenseDeliveryRecoveryError(
                code="operations.expense_requests.recovery_preview_stale",
                message="Recovery evidence changed; preview it again.",
            )
        original, request = _load_recoverable(db, command.dead_event_id, lock=False)
        existing = (
            db.query(FieldErpSyncEvent)
            .filter(
                FieldErpSyncEvent.idempotency_key == preview.replacement_idempotency_key
            )
            .one_or_none()
        )
        replayed = existing is not None
        replacement = existing
        if replacement is None:
            payload = dict(original.payload or {})
            payload.update(
                {
                    "_replaces_event_id": str(original.id),
                    "_recovery_contract_version": RECOVERY_CONTRACT_VERSION,
                }
            )
            replacement = enqueue(
                db,
                flow=FieldErpSyncFlow.expense_claim,
                entity_type="field_expense_request",
                entity_id=request.id,
                idempotency_key=preview.replacement_idempotency_key,
                payload=payload,
                isolate=False,
            )
            if isinstance(original.erp_response, dict):
                replacement.erp_response = dict(original.erp_response)

        metadata = dict(request.metadata_ or {})
        recoveries = list(metadata.get("expense_delivery_recoveries") or [])
        evidence = {
            "contract_version": RECOVERY_CONTRACT_VERSION,
            "original_event_id": str(original.id),
            "replacement_event_id": str(replacement.id),
            "command_id": str(command.context.command_id),
            "actor": command.context.actor,
            "occurred_at": datetime.now(UTC).isoformat(),
        }
        if not any(
            item.get("replacement_event_id") == str(replacement.id)
            for item in recoveries
            if isinstance(item, dict)
        ):
            recoveries.append(evidence)
            metadata["expense_delivery_recoveries"] = recoveries[-100:]
            request.metadata_ = metadata
        db.flush()
        return ExpenseDeliveryRecoveryOutcome(
            original_event_id=original.id,
            replacement_event_id=replacement.id,
            replacement_idempotency_key=replacement.idempotency_key,
            replayed=replayed,
        )

    return execute_owner_command(
        db,
        definition=_RECOVER_EXPENSE_DELIVERY,
        context=command.context,
        operation=operation,
    )
