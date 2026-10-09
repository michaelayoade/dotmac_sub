#!/usr/bin/env python
"""Preview or apply one reviewed stranded account-credit reconciliation."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.system_user import SystemUser
from app.services.account_credit_invoice_reconciliation import (
    RECONCILIATION_SCOPE,
    AccountCreditInvoiceReconciliationPreview,
    AccountCreditInvoiceReconciliationQuery,
    ReconcileAccountCreditInvoiceCommand,
    preview_account_credit_invoice_reconciliation,
    reconcile_account_credit_invoice,
)
from app.services.auth_dependencies import has_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.system_user_assignments import system_user_role_names


def _money(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("amount must be decimal") from exc
    if not parsed.is_finite():
        raise argparse.ArgumentTypeError("amount must be finite")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=UUID, required=True)
    parser.add_argument("--invoice-id", type=UUID, required=True)
    parser.add_argument("--payment-id", type=UUID, required=True)
    parser.add_argument("--topup-intent-id", type=UUID, required=True)
    parser.add_argument("--expected-amount", type=_money, required=True)
    parser.add_argument("--currency", default="NGN")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-preview-fingerprint")
    parser.add_argument("--command-id", type=UUID)
    parser.add_argument("--actor")
    parser.add_argument("--actor-system-user-id", type=UUID)
    parser.add_argument("--idempotency-key")
    return parser


def _permission_granted(db: Session, actor_id: UUID | None) -> bool:
    if actor_id is None:
        return False
    user = db.get(SystemUser, actor_id)
    if user is None or not user.is_active:
        return False
    return has_permission(
        {
            "principal_id": str(actor_id),
            "principal_type": "system_user",
            "roles": set(system_user_role_names(db, actor_id)),
        },
        db,
        RECONCILIATION_SCOPE,
    )


def _payload(value: AccountCreditInvoiceReconciliationPreview) -> dict[str, object]:
    return {
        "disposition": value.disposition.value,
        "actionable": value.actionable,
        "reason": value.reason,
        "account_id": str(value.account_id),
        "invoice_id": str(value.invoice_id),
        "invoice_number": value.invoice_number,
        "payment_id": str(value.payment_id),
        "settlement_id": str(value.settlement_id) if value.settlement_id else None,
        "topup_intent_id": str(value.topup_intent_id),
        "currency": value.currency,
        "expected_amount": str(value.expected_amount),
        "invoice_balance": str(value.invoice_balance),
        "account_credit": str(value.account_credit),
        "payment_available": str(value.payment_available),
        "allocation_id": str(value.allocation_id) if value.allocation_id else None,
        "preview_fingerprint": value.fingerprint,
    }


def main() -> int:
    args = _parser().parse_args()
    query = AccountCreditInvoiceReconciliationQuery(
        account_id=args.account_id,
        invoice_id=args.invoice_id,
        payment_id=args.payment_id,
        topup_intent_id=args.topup_intent_id,
        expected_amount=args.expected_amount,
        currency=args.currency,
    )
    try:
        with db_session_adapter.read_session() as db:
            preview = preview_account_credit_invoice_reconciliation(db, query)
            permission_granted = _permission_granted(db, args.actor_system_user_id)
        if not args.apply:
            print(
                json.dumps(
                    {"applied": False, "preview": _payload(preview)}, sort_keys=True
                )
            )
            return 0
        required = (
            args.expected_preview_fingerprint,
            args.command_id,
            args.actor,
            args.actor_system_user_id,
            args.idempotency_key,
        )
        if not all(required):
            print(
                json.dumps(
                    {
                        "applied": False,
                        "error": (
                            "--apply requires fingerprint, command ID, actor, "
                            "staff ID, and idempotency key"
                        ),
                        "preview": _payload(preview),
                    },
                    sort_keys=True,
                )
            )
            return 2
        with db_session_adapter.owner_command_session() as db:
            result = reconcile_account_credit_invoice(
                db,
                ReconcileAccountCreditInvoiceCommand(
                    query=query,
                    expected_preview_fingerprint=args.expected_preview_fingerprint,
                    permission_granted=permission_granted,
                    authorized_system_user_id=args.actor_system_user_id,
                ),
                context=CommandContext.system(
                    actor=args.actor,
                    scope=RECONCILIATION_SCOPE,
                    reason=args.reason,
                    command_id=args.command_id,
                    correlation_id=args.command_id,
                    idempotency_key=args.idempotency_key,
                ),
            )
    except DomainError as exc:
        print(
            json.dumps(
                {
                    "applied": False,
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "applied": True,
                "account_id": str(result.account_id),
                "invoice_id": str(result.invoice_id),
                "payment_id": str(result.payment_id),
                "settlement_id": str(result.settlement_id),
                "topup_intent_id": str(result.topup_intent_id),
                "allocation_id": str(result.allocation_id),
                "invoice_ledger_entry_id": str(result.invoice_ledger_entry_id),
                "credit_consumption_ledger_entry_id": str(
                    result.credit_consumption_ledger_entry_id
                ),
                "amount": str(result.amount),
                "currency": result.currency,
                "preview_fingerprint": result.preview_fingerprint,
                "replayed": result.replayed,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
