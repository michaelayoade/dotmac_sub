#!/usr/bin/env python
"""Preview or apply an exact reviewed prepaid sequence funding correction."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TypeAlias, cast
from uuid import UUID

from app.models.billing import InvoiceStatus
from app.models.system_user import SystemUser
from app.services.auth_dependencies import has_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.prepaid_draft_reconciliation import (
    REPAIR_SCOPE,
    CorrectReviewedPrepaidSequenceFundingCommand,
    PrepaidDraftReconciliationError,
    ReviewedPrepaidInvoiceSequenceApproval,
    ReviewedPrepaidSequenceCorrectionDocument,
    ReviewedPrepaidSequenceFundingCorrectionQuery,
    correct_reviewed_prepaid_sequence_funding,
    preview_reviewed_prepaid_sequence_funding_correction,
)
from app.services.system_user_assignments import system_user_role_names

JsonObject: TypeAlias = dict[str, object]


def _uuid(value: object, field: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _timestamp(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone offset")
    return parsed


def _optional_timestamp(value: object, field: str) -> datetime | None:
    return None if value is None else _timestamp(value, field)


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
        raise ValueError(f"{field} must be an array of objects")
    return tuple(cast(JsonObject, item) for item in value)


def _manifest(path: Path) -> ReviewedPrepaidSequenceFundingCorrectionQuery:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest root must be an object")
    data = cast(JsonObject, payload)
    approval_value = data.get("approval")
    if not isinstance(approval_value, dict):
        raise ValueError("approval must be an object")
    approval = cast(JsonObject, approval_value)
    documents = tuple(
        ReviewedPrepaidSequenceCorrectionDocument(
            invoice_id=_uuid(item.get("invoice_id"), "documents.invoice_id"),
            line_id=_uuid(item.get("line_id"), "documents.line_id"),
            service_period_start=_timestamp(
                item.get("service_period_start"), "documents.service_period_start"
            ),
            service_period_end=_timestamp(
                item.get("service_period_end"), "documents.service_period_end"
            ),
            expected_total=_money(
                item.get("expected_total"), "documents.expected_total"
            ),
            expected_status=InvoiceStatus(str(item.get("expected_status"))),
            expected_current_period_start=_optional_timestamp(
                item.get("expected_current_period_start"),
                "documents.expected_current_period_start",
            ),
            expected_current_period_end=_optional_timestamp(
                item.get("expected_current_period_end"),
                "documents.expected_current_period_end",
            ),
        )
        for item in _objects(data.get("documents"), "documents")
    )
    return ReviewedPrepaidSequenceFundingCorrectionQuery(
        subscription_id=_uuid(data.get("subscription_id"), "subscription_id"),
        documents=documents,
        historical_payment_id=_uuid(
            data.get("historical_payment_id"), "historical_payment_id"
        ),
        splynx_transaction_id=_uuid(
            data.get("splynx_transaction_id"), "splynx_transaction_id"
        ),
        historical_existing_allocation_id=_uuid(
            data.get("historical_existing_allocation_id"),
            "historical_existing_allocation_id",
        ),
        opening_position_id=_uuid(
            data.get("opening_position_id"), "opening_position_id"
        ),
        displaced_payment_id=_uuid(
            data.get("displaced_payment_id"), "displaced_payment_id"
        ),
        displaced_allocation_id=_uuid(
            data.get("displaced_allocation_id"), "displaced_allocation_id"
        ),
        credit_target_invoice_id=_uuid(
            data.get("credit_target_invoice_id"), "credit_target_invoice_id"
        ),
        duplicate_void_invoice_id=_uuid(
            data.get("duplicate_void_invoice_id"), "duplicate_void_invoice_id"
        ),
        expected_historical_payment_amount=_money(
            data.get("expected_historical_payment_amount"),
            "expected_historical_payment_amount",
        ),
        expected_displaced_payment_amount=_money(
            data.get("expected_displaced_payment_amount"),
            "expected_displaced_payment_amount",
        ),
        expected_opening_credit=_money(
            data.get("expected_opening_credit"), "expected_opening_credit"
        ),
        expected_post_repair_credit=_money(
            data.get("expected_post_repair_credit"), "expected_post_repair_credit"
        ),
        approval=ReviewedPrepaidInvoiceSequenceApproval(
            approver_system_user_id=_uuid(
                approval.get("approver_system_user_id"),
                "approval.approver_system_user_id",
            ),
            approver_name=str(approval.get("approver_name") or ""),
            ticket_reference=str(approval.get("ticket_reference") or ""),
            approved_at=(
                _timestamp(approval["approved_at"], "approval.approved_at")
                if approval.get("approved_at")
                else None
            ),
            evidence_sha256=(
                str(approval["evidence_sha256"])
                if approval.get("evidence_sha256")
                else None
            ),
        ),
    )


def _permission_granted(db, actor_system_user_id: UUID) -> bool:  # noqa: ANN001
    user = db.get(SystemUser, actor_system_user_id)
    if user is None or not user.is_active:
        return False
    auth = {
        "principal_id": str(actor_system_user_id),
        "principal_type": "system_user",
        "roles": set(system_user_role_names(db, actor_system_user_id)),
    }
    return has_permission(auth, db, REPAIR_SCOPE)


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
    try:
        query = _manifest(args.manifest)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))

    if not args.apply:
        with db_session_adapter.read_session() as db:
            preview = preview_reviewed_prepaid_sequence_funding_correction(db, query)
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "account_id": str(preview.account_id),
                    "subscription_id": str(preview.subscription_id),
                    "invoice_ids": [str(item) for item in preview.invoice_ids],
                    "historical_payment_id": str(preview.historical_payment_id),
                    "displaced_payment_id": str(preview.displaced_payment_id),
                    "credit_target_invoice_id": str(preview.credit_target_invoice_id),
                    "funding_position_at": (
                        preview.funding_position_at.isoformat()
                        if preview.funding_position_at
                        else None
                    ),
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
                    "expected_post_repair_credit": str(
                        preview.expected_post_repair_credit
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
            permission = _permission_granted(db, args.actor_system_user_id)
            db_session_adapter.release_read_transaction(db)
            result = correct_reviewed_prepaid_sequence_funding(
                db,
                CorrectReviewedPrepaidSequenceFundingCommand(
                    context=CommandContext.system(
                        actor=args.actor,
                        scope=REPAIR_SCOPE,
                        reason=args.reason,
                        idempotency_key=args.idempotency_key,
                    ),
                    query=query,
                    preview_fingerprint=args.fingerprint,
                    permission_granted=permission,
                    actor_system_user_id=args.actor_system_user_id,
                ),
            )
    except PrepaidDraftReconciliationError as exc:
        print(
            json.dumps(
                {"error": {"code": exc.code, "message": exc.message}},
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "account_id": str(result.account_id),
                "subscription_id": str(result.subscription_id),
                "invoice_ids": [str(item) for item in result.invoice_ids],
                "entitlement_ids": [str(item) for item in result.entitlement_ids],
                "released_allocation_id": str(result.released_allocation_id),
                "historical_allocation_id": str(result.historical_allocation_id),
                "credit_target_allocation_id": str(result.credit_target_allocation_id),
                "next_billing_at": result.next_billing_at.isoformat(),
                "remaining_credit": str(result.remaining_credit),
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
