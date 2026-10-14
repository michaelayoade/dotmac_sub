#!/usr/bin/env python
"""Preview or apply one named-approval prepaid invoice-sequence repair.

The manifest contains only explicit identifiers, dates, amounts, ledger
selections, and named approval reference. Preview is the default and is
read-only.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TypeAlias, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from app.models.system_user import SystemUser
from app.services.auth_dependencies import has_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.prepaid_calendar_contracts import (
    ReviewedPrepaidCalendarBasis,
    ReviewedPrepaidCalendarSelection,
)
from app.services.prepaid_draft_reconciliation import (
    REPAIR_SCOPE,
    PrepaidDraftReconciliationError,
    ReconstructReviewedPrepaidInvoiceSequenceCommand,
    ReviewedPrepaidExistingAllocationEvidence,
    ReviewedPrepaidInvoiceSequenceAllocationSelection,
    ReviewedPrepaidInvoiceSequenceApproval,
    ReviewedPrepaidInvoiceSequenceDocumentSelection,
    ReviewedPrepaidInvoiceSequenceQuery,
    ReviewedPrepaidSettlementEvidenceSelection,
    preview_reviewed_prepaid_invoice_sequence_reconstruction,
    reconstruct_reviewed_prepaid_invoice_sequence,
)
from app.services.system_user_assignments import system_user_role_names
from scripts.billing._mutation_policy import refuse_apply

JsonObject: TypeAlias = dict[str, object]

_SEQUENCE_INCOMPLETE_REPAIR_CODE = (
    "financial.prepaid_draft_reconciliation.incomplete_repair"
)
_SAFE_SEQUENCE_POSTCONDITION_DETAILS = frozenset(
    {
        "remaining_credit",
        "expected_remaining_credit",
        "authoritative_prepaid_funding",
        "expected_authoritative_prepaid_funding",
        "customer_position_delta",
        "service_period_end",
    }
)


def _uuid(value: object, field: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _date(value: object, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def _timestamp(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone offset")
    return parsed


def _money(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a decimal amount") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return parsed


def _objects(value: object, field: str) -> tuple[JsonObject, ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{field} must be a JSON array of objects")
    return tuple(cast(JsonObject, item) for item in value)


def _calendar(value: object) -> ReviewedPrepaidCalendarSelection:
    if not isinstance(value, dict):
        raise ValueError("calendar must be a JSON object")
    data = cast(JsonObject, value)
    if set(data) - {"basis", "expected_initial_anchor_at"}:
        raise ValueError("calendar contains unsupported fields")
    try:
        basis = ReviewedPrepaidCalendarBasis(str(data.get("basis")))
    except ValueError as exc:
        raise ValueError(
            "calendar.basis must be business_midnight or documented_anniversary"
        ) from exc
    anchor = data.get("expected_initial_anchor_at")
    if basis is ReviewedPrepaidCalendarBasis.documented_anniversary and anchor is None:
        raise ValueError(
            "documented_anniversary requires calendar.expected_initial_anchor_at"
        )
    if basis is ReviewedPrepaidCalendarBasis.business_midnight and anchor is not None:
        raise ValueError("business_midnight does not accept an anchor override")
    return ReviewedPrepaidCalendarSelection(
        basis=basis,
        expected_initial_anchor_at=(
            _timestamp(anchor, "calendar.expected_initial_anchor_at")
            if anchor is not None
            else None
        ),
    )


def _manifest(path: Path) -> ReviewedPrepaidInvoiceSequenceQuery:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest root must be a JSON object")
    data = cast(JsonObject, payload)
    approval_value = data.get("approval")
    if not isinstance(approval_value, dict):
        raise ValueError("approval must be a JSON object")
    approval = cast(JsonObject, approval_value)
    return ReviewedPrepaidInvoiceSequenceQuery(
        calendar=(
            _calendar(data["calendar"])
            if "calendar" in data
            else ReviewedPrepaidCalendarSelection()
        ),
        subscription_id=_uuid(data.get("subscription_id"), "subscription_id"),
        documents=tuple(
            ReviewedPrepaidInvoiceSequenceDocumentSelection(
                invoice_id=_uuid(item.get("invoice_id"), "documents.invoice_id"),
                line_id=_uuid(item.get("line_id"), "documents.line_id"),
                service_start_on=_date(
                    item.get("service_start_on"), "documents.service_start_on"
                ),
                next_billing_on=_date(
                    item.get("next_billing_on"), "documents.next_billing_on"
                ),
                expected_total=_money(
                    item.get("expected_total"), "documents.expected_total"
                ),
            )
            for item in _objects(data.get("documents"), "documents")
        ),
        allocations=tuple(
            ReviewedPrepaidInvoiceSequenceAllocationSelection(
                payment_id=_uuid(item.get("payment_id"), "allocations.payment_id"),
                invoice_id=_uuid(item.get("invoice_id"), "allocations.invoice_id"),
                amount=_money(item.get("amount"), "allocations.amount"),
            )
            for item in _objects(data.get("allocations"), "allocations")
        ),
        settlement_evidence=tuple(
            ReviewedPrepaidSettlementEvidenceSelection(
                payment_id=_uuid(
                    item.get("payment_id"), "settlement_evidence.payment_id"
                ),
                unallocated_ledger_entry_id=_uuid(
                    item.get("unallocated_ledger_entry_id"),
                    "settlement_evidence.unallocated_ledger_entry_id",
                ),
            )
            for item in _objects(data.get("settlement_evidence"), "settlement_evidence")
        ),
        existing_allocation_evidence=tuple(
            ReviewedPrepaidExistingAllocationEvidence(
                allocation_id=_uuid(
                    item.get("allocation_id"),
                    "existing_allocation_evidence.allocation_id",
                ),
                invoice_ledger_entry_id=_uuid(
                    item.get("invoice_ledger_entry_id"),
                    "existing_allocation_evidence.invoice_ledger_entry_id",
                ),
                balancing_ledger_entry_id=_uuid(
                    item.get("balancing_ledger_entry_id"),
                    "existing_allocation_evidence.balancing_ledger_entry_id",
                ),
            )
            for item in _objects(
                data.get("existing_allocation_evidence"),
                "existing_allocation_evidence",
            )
        ),
        expected_opening_credit=_money(
            data.get("expected_opening_credit"), "expected_opening_credit"
        ),
        expected_post_repair_credit=_money(
            data.get("expected_post_repair_credit"), "expected_post_repair_credit"
        ),
        expected_authoritative_prepaid_funding=_money(
            data.get("expected_authoritative_prepaid_funding"),
            "expected_authoritative_prepaid_funding",
        ),
        expected_opening_funding_consumption=_money(
            data.get("expected_opening_funding_consumption", "0.00"),
            "expected_opening_funding_consumption",
        ),
        approval=ReviewedPrepaidInvoiceSequenceApproval(
            approver_system_user_id=_uuid(
                approval.get("approver_system_user_id"),
                "approval.approver_system_user_id",
            ),
            approver_name=str(approval.get("approver_name") or ""),
            approved_at=(
                _timestamp(approval["approved_at"], "approval.approved_at")
                if approval.get("approved_at")
                else None
            ),
            ticket_reference=str(approval.get("ticket_reference") or ""),
            evidence_sha256=(
                str(approval["evidence_sha256"])
                if approval.get("evidence_sha256")
                else None
            ),
        ),
    )


def _permission_granted(db, actor_system_user_id: UUID) -> bool:  # noqa: ANN001
    system_user = db.get(SystemUser, actor_system_user_id)
    if system_user is None or not system_user.is_active:
        return False
    auth = {
        "principal_id": str(actor_system_user_id),
        "principal_type": "system_user",
        "roles": set(system_user_role_names(db, actor_system_user_id)),
    }
    return has_permission(auth, db, REPAIR_SCOPE)


def _operator_error_payload(
    error: PrepaidDraftReconciliationError,
) -> dict[str, object]:
    """Serialize safe owner error context for this command's operator adapter."""

    details: dict[str, object] = {}
    if error.code == _SEQUENCE_INCOMPLETE_REPAIR_CODE:
        details = {
            key: value
            for key, value in error.details.items()
            if key in _SAFE_SEQUENCE_POSTCONDITION_DETAILS and isinstance(value, str)
        }
    return {
        "error": {
            "code": error.code,
            "message": error.message,
            "details": details,
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--actor")
    parser.add_argument("--actor-system-user-id", type=UUID)
    parser.add_argument("--reason")
    args = parser.parse_args()
    refuse_apply(args.apply)
    try:
        query = _manifest(args.manifest)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))

    if not args.apply:
        with db_session_adapter.read_session() as db:
            preview = preview_reviewed_prepaid_invoice_sequence_reconstruction(
                db, query
            )
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "operation": "reconstruct_reviewed_prepaid_invoice_sequence",
                    "account_id": str(preview.account_id),
                    "subscription_id": str(preview.subscription_id),
                    "invoice_ids": [str(item) for item in preview.invoice_ids],
                    "payment_ids": [str(item) for item in preview.payment_ids],
                    "service_period_start": (
                        preview.service_period_start.isoformat()
                        if preview.service_period_start
                        else None
                    ),
                    "service_period_end": (
                        preview.service_period_end.isoformat()
                        if preview.service_period_end
                        else None
                    ),
                    "calendar_basis": preview.calendar_basis.value,
                    "timezone_name": preview.timezone_name,
                    "initial_anchor_at": (
                        preview.initial_anchor_at.isoformat()
                        if preview.initial_anchor_at
                        else None
                    ),
                    "service_periods": [
                        {
                            "invoice_id": str(selection.invoice_id),
                            "line_id": str(selection.line_id),
                            "expected_total": str(selection.expected_total),
                            "starts_at": period.starts_at.isoformat(),
                            "ends_at": period.ends_at.isoformat(),
                            "starts_at_local": period.starts_at.astimezone(
                                ZoneInfo(period.timezone_name)
                            ).isoformat(),
                            "ends_at_local": period.ends_at.astimezone(
                                ZoneInfo(period.timezone_name)
                            ).isoformat(),
                        }
                        for selection, period in zip(
                            query.documents, preview.service_periods
                        )
                    ],
                    "reviewed_allocation_plan": [
                        {
                            "payment_id": str(item.payment_id),
                            "invoice_id": str(item.invoice_id),
                            "amount": str(item.amount),
                        }
                        for item in query.allocations
                    ],
                    "settlement_evidence": [
                        {
                            "payment_id": str(item.payment_id),
                            "unallocated_ledger_entry_id": str(
                                item.unallocated_ledger_entry_id
                            ),
                        }
                        for item in query.settlement_evidence
                    ],
                    "existing_allocation_evidence": [
                        {
                            "allocation_id": str(item.allocation_id),
                            "invoice_ledger_entry_id": str(
                                item.invoice_ledger_entry_id
                            ),
                            "balancing_ledger_entry_id": str(
                                item.balancing_ledger_entry_id
                            ),
                        }
                        for item in query.existing_allocation_evidence
                    ],
                    "expected_post_repair_credit": str(
                        preview.expected_post_repair_credit
                    ),
                    "funding_position_at": (
                        preview.funding_position_at.isoformat()
                        if preview.funding_position_at
                        else None
                    ),
                    "invoice_total": str(preview.invoice_total),
                    "existing_allocation_total": str(preview.existing_allocation_total),
                    "selected_payment_total": str(preview.selected_payment_total),
                    "selected_payment_allocation_total": str(
                        preview.selected_payment_allocation_total
                    ),
                    "selected_payment_residual": str(preview.selected_payment_residual),
                    "opening_credit": str(preview.opening_credit),
                    "opening_funding_consumption": str(
                        preview.opening_funding_consumption
                    ),
                    "post_boundary_credit": str(preview.post_boundary_credit),
                    "authoritative_prepaid_funding": str(
                        preview.authoritative_prepaid_funding
                    ),
                    "disposition": preview.disposition.value,
                    "actionable": preview.actionable,
                    "reason": preview.reason,
                    "fingerprint": preview.fingerprint,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    required = {
        "--fingerprint": args.fingerprint,
        "--idempotency-key": args.idempotency_key,
        "--actor": args.actor,
        "--actor-system-user-id": args.actor_system_user_id,
        "--reason": args.reason,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        parser.error("--apply requires " + ", ".join(missing))
    assert args.actor_system_user_id is not None
    try:
        with db_session_adapter.owner_command_session() as db:
            permission_granted = _permission_granted(db, args.actor_system_user_id)
            db_session_adapter.release_read_transaction(db)
            result = reconstruct_reviewed_prepaid_invoice_sequence(
                db,
                ReconstructReviewedPrepaidInvoiceSequenceCommand(
                    context=CommandContext.system(
                        actor=args.actor,
                        scope=REPAIR_SCOPE,
                        reason=args.reason,
                        idempotency_key=args.idempotency_key,
                    ),
                    query=query,
                    preview_fingerprint=args.fingerprint,
                    permission_granted=permission_granted,
                    actor_system_user_id=args.actor_system_user_id,
                ),
            )
    except PrepaidDraftReconciliationError as exc:
        print(
            json.dumps(_operator_error_payload(exc), indent=2, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "account_id": str(result.account_id),
                "subscription_id": str(result.subscription_id),
                "invoice_ids": [str(item) for item in result.invoice_ids],
                "allocation_ids": [str(item) for item in result.allocation_ids],
                "entitlement_ids": [str(item) for item in result.entitlement_ids],
                "next_billing_at": result.next_billing_at.isoformat(),
                "remaining_credit": str(result.remaining_credit),
                "authoritative_prepaid_funding": str(
                    result.authoritative_prepaid_funding
                ),
                "access_restored": result.access_restored,
                "customer_position_delta": str(result.customer_position_delta),
                "preview_fingerprint": result.preview_fingerprint,
                "replayed": result.replayed,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
