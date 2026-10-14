#!/usr/bin/env python
"""Preview or settle one Finance-approved historical prepaid draft.

Preview is read-only. Apply requires the exact fingerprint, a real RBAC
principal, immutable Finance approval evidence, and a stable idempotency key.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from app.models.system_user import SystemUser
from app.services.auth_dependencies import has_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.prepaid_draft_reconciliation import (
    REPAIR_SCOPE,
    ReviewedExistingDraftSettlementApproval,
    ReviewedExistingDraftSettlementQuery,
    SettleReviewedExistingPrepaidDraftCommand,
    preview_reviewed_existing_prepaid_draft_settlement,
    settle_reviewed_existing_prepaid_draft,
)
from app.services.system_user_assignments import system_user_role_names
from scripts.billing._mutation_policy import refuse_apply


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("identifier must be a UUID") from exc


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone offset")
    return parsed


def _money(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("money must be a decimal amount") from exc
    if not parsed.is_finite():
        raise argparse.ArgumentTypeError("money must be finite")
    return parsed


def _permission_granted(db, actor_system_user_id: UUID) -> bool:
    system_user = db.get(SystemUser, actor_system_user_id)
    if system_user is None or not system_user.is_active:
        return False
    auth = {
        "principal_id": str(actor_system_user_id),
        "principal_type": "system_user",
        "roles": set(system_user_role_names(db, actor_system_user_id)),
    }
    return has_permission(auth, db, REPAIR_SCOPE)


def _query(args: argparse.Namespace) -> ReviewedExistingDraftSettlementQuery:
    return ReviewedExistingDraftSettlementQuery(
        invoice_id=args.invoice_id,
        subscription_id=args.subscription_id,
        payment_id=args.payment_id,
        service_start_on=args.service_start_on,
        next_billing_on=args.next_billing_on,
        expected_total=args.expected_total,
        expected_remaining_credit=args.expected_remaining_credit,
        payment_reference=args.payment_reference,
        superseded_invoice_id=args.superseded_invoice_id,
        superseded_payment_id=args.superseded_payment_id,
        fund_next_continuous_period=args.fund_next_continuous_period,
        approval=ReviewedExistingDraftSettlementApproval(
            approver_system_user_id=args.approver_system_user_id,
            approver_name=args.approver_name,
            approved_at=args.approved_at,
            ticket_reference=args.ticket_reference,
            evidence_sha256=args.evidence_sha256,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--invoice-id", type=_uuid, required=True)
    parser.add_argument("--subscription-id", type=_uuid, required=True)
    parser.add_argument("--payment-id", type=_uuid, required=True)
    parser.add_argument("--service-start-on", type=_date, required=True)
    parser.add_argument("--next-billing-on", type=_date, required=True)
    parser.add_argument("--expected-total", type=_money, required=True)
    parser.add_argument("--expected-remaining-credit", type=_money, required=True)
    parser.add_argument("--payment-reference", required=True)
    parser.add_argument("--superseded-invoice-id", type=_uuid)
    parser.add_argument("--superseded-payment-id", type=_uuid)
    parser.add_argument("--fund-next-continuous-period", action="store_true")
    parser.add_argument("--approver-system-user-id", type=_uuid, required=True)
    parser.add_argument("--approver-name", required=True)
    parser.add_argument("--approved-at", type=_timestamp, required=True)
    parser.add_argument("--ticket-reference", required=True)
    parser.add_argument("--evidence-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--actor")
    parser.add_argument("--actor-system-user-id", type=_uuid)
    parser.add_argument("--reason")
    args = parser.parse_args()
    refuse_apply(args.apply)

    query = _query(args)
    if not args.apply:
        with db_session_adapter.read_session() as db:
            preview = preview_reviewed_existing_prepaid_draft_settlement(db, query)
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "operation": "settle_reviewed_existing_prepaid_draft",
                    "invoice_id": str(preview.invoice_id),
                    "invoice_number": preview.invoice_number,
                    "account_id": str(preview.account_id),
                    "subscription_id": str(preview.subscription_id),
                    "line_id": str(preview.line_id) if preview.line_id else None,
                    "payment_id": str(preview.payment_id),
                    "settlement_id": (
                        str(preview.settlement_id) if preview.settlement_id else None
                    ),
                    "service_period_start": preview.service_period_start.isoformat(),
                    "service_period_end": preview.service_period_end.isoformat(),
                    "next_billing_at": preview.service_period_end.isoformat(),
                    "invoice_total": str(preview.invoice_total),
                    "account_credit_before": str(preview.account_credit_before),
                    "selected_payment_available": str(
                        preview.selected_payment_available
                    ),
                    "expected_remaining_credit": str(preview.expected_remaining_credit),
                    "payment_reference": preview.payment_reference,
                    "superseded_invoice_id": (
                        str(preview.superseded_invoice_id)
                        if preview.superseded_invoice_id
                        else None
                    ),
                    "superseded_payment_id": (
                        str(preview.superseded_payment_id)
                        if preview.superseded_payment_id
                        else None
                    ),
                    "superseded_allocation_id": (
                        str(preview.superseded_allocation_id)
                        if preview.superseded_allocation_id
                        else None
                    ),
                    "superseded_entitlement_id": (
                        str(preview.superseded_entitlement_id)
                        if preview.superseded_entitlement_id
                        else None
                    ),
                    "superseded_void_fingerprint": (
                        preview.superseded_void_fingerprint
                    ),
                    "successor_payment_id": (
                        str(preview.successor_payment_id)
                        if preview.successor_payment_id
                        else None
                    ),
                    "successor_period_start": (
                        preview.successor_period_start.isoformat()
                        if preview.successor_period_start
                        else None
                    ),
                    "successor_period_end": (
                        preview.successor_period_end.isoformat()
                        if preview.successor_period_end
                        else None
                    ),
                    "successor_amount": (
                        str(preview.successor_amount)
                        if preview.successor_amount is not None
                        else None
                    ),
                    "successor_currency": preview.successor_currency,
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
    with db_session_adapter.owner_command_session() as db:
        permission_granted = _permission_granted(db, args.actor_system_user_id)
        db_session_adapter.release_read_transaction(db)
        result = settle_reviewed_existing_prepaid_draft(
            db,
            SettleReviewedExistingPrepaidDraftCommand(
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
    print(
        json.dumps(
            {
                "invoice_id": str(result.invoice_id),
                "invoice_number": result.invoice_number,
                "subscription_id": str(result.subscription_id),
                "line_id": str(result.line_id),
                "payment_id": str(result.payment_id),
                "allocation_id": str(result.allocation_id),
                "entitlement_id": str(result.entitlement_id),
                "access_consequence_id": (
                    str(result.access_consequence_id)
                    if result.access_consequence_id
                    else None
                ),
                "service_period_start": result.service_period_start.isoformat(),
                "service_period_end": result.service_period_end.isoformat(),
                "next_billing_at": result.next_billing_at.isoformat(),
                "remaining_credit": str(result.remaining_credit),
                "payment_reference": result.payment_reference,
                "preview_fingerprint": result.preview_fingerprint,
                "replayed": result.replayed,
                "superseded_invoice_id": (
                    str(result.superseded_invoice_id)
                    if result.superseded_invoice_id
                    else None
                ),
                "superseded_payment_id": (
                    str(result.superseded_payment_id)
                    if result.superseded_payment_id
                    else None
                ),
                "superseded_allocation_id": (
                    str(result.superseded_allocation_id)
                    if result.superseded_allocation_id
                    else None
                ),
                "superseded_entitlement_id": (
                    str(result.superseded_entitlement_id)
                    if result.superseded_entitlement_id
                    else None
                ),
                "successor_invoice_id": (
                    str(result.successor_invoice_id)
                    if result.successor_invoice_id
                    else None
                ),
                "successor_invoice_line_id": (
                    str(result.successor_invoice_line_id)
                    if result.successor_invoice_line_id
                    else None
                ),
                "successor_allocation_id": (
                    str(result.successor_allocation_id)
                    if result.successor_allocation_id
                    else None
                ),
                "successor_entitlement_id": (
                    str(result.successor_entitlement_id)
                    if result.successor_entitlement_id
                    else None
                ),
                "successor_period_start": (
                    result.successor_period_start.isoformat()
                    if result.successor_period_start
                    else None
                ),
                "successor_period_end": (
                    result.successor_period_end.isoformat()
                    if result.successor_period_end
                    else None
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
