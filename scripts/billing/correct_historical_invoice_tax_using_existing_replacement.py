"""Preview or apply an approved VAT correction using an existing draft invoice."""

from __future__ import annotations

import argparse
import json
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models.system_user import SystemUser
from app.services.auth_dependencies import has_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.historical_invoice_tax_corrections import (
    CORRECTION_SCOPE,
    CorrectExistingReplacementTaxInvoiceCommand,
    ExistingReplacementTaxCorrectionPreview,
    ExistingReplacementTaxCorrectionQuery,
    correct_historical_invoice_tax_using_existing_replacement,
    preview_existing_replacement_tax_correction,
)
from app.services.owner_commands import CommandContext
from app.services.system_user_assignments import system_user_role_names


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=UUID, required=True)
    parser.add_argument("--source-invoice-id", type=UUID, required=True)
    parser.add_argument("--source-invoice-line-id", type=UUID, required=True)
    parser.add_argument("--replacement-invoice-id", type=UUID, required=True)
    parser.add_argument("--payment-id", type=UUID, required=True)
    parser.add_argument("--tax-rate-id", type=UUID, required=True)
    parser.add_argument("--ticket-reference", required=True)
    parser.add_argument("--approver-name", required=True)
    parser.add_argument("--currency", default="NGN")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-preview-fingerprint")
    parser.add_argument("--command-id", type=UUID)
    parser.add_argument("--actor")
    parser.add_argument("--actor-system-user-id", type=UUID)
    parser.add_argument("--idempotency-key")
    return parser


def _query(args: argparse.Namespace) -> ExistingReplacementTaxCorrectionQuery:
    return ExistingReplacementTaxCorrectionQuery(
        account_id=args.account_id,
        source_invoice_id=args.source_invoice_id,
        source_invoice_line_id=args.source_invoice_line_id,
        replacement_invoice_id=args.replacement_invoice_id,
        payment_id=args.payment_id,
        tax_rate_id=args.tax_rate_id,
        ticket_reference=args.ticket_reference,
        approver_name=args.approver_name,
        currency=args.currency,
    )


def _preview_dict(value: ExistingReplacementTaxCorrectionPreview) -> dict[str, object]:
    return {
        "disposition": value.disposition.value,
        "actionable": value.actionable,
        "reason": value.reason,
        "account_id": str(value.account_id),
        "source_invoice_id": str(value.source_invoice_id),
        "source_invoice_line_id": str(value.source_invoice_line_id),
        "replacement_invoice_id": str(value.replacement_invoice_id),
        "payment_id": str(value.payment_id),
        "payment_reference": value.payment_reference,
        "source_payment_allocation_id": (
            str(value.source_payment_allocation_id)
            if value.source_payment_allocation_id
            else None
        ),
        "source_invoice_ledger_entry_id": (
            str(value.source_invoice_ledger_entry_id)
            if value.source_invoice_ledger_entry_id
            else None
        ),
        "unallocated_credit_ledger_entry_id": (
            str(value.unallocated_credit_ledger_entry_id)
            if value.unallocated_credit_ledger_entry_id
            else None
        ),
        "subtotal": str(value.subtotal),
        "tax_amount": str(value.tax_amount),
        "replacement_total": str(value.replacement_total),
        "payment_amount": str(value.payment_amount),
        "current_account_credit": str(value.current_account_credit),
        "projected_remaining_credit": str(value.projected_remaining_credit),
        "reconstruct_consumption_evidence": value.reconstruct_consumption_evidence,
        "preview_fingerprint": value.fingerprint,
    }


def _permission_granted(db: Session, actor_system_user_id: UUID | None) -> bool:
    if actor_system_user_id is None:
        return False
    user = db.get(SystemUser, actor_system_user_id)
    if user is None or not user.is_active:
        return False
    return has_permission(
        {
            "principal_id": str(actor_system_user_id),
            "principal_type": "system_user",
            "roles": set(system_user_role_names(db, actor_system_user_id)),
        },
        db,
        CORRECTION_SCOPE,
    )


def main() -> int:
    args = _parser().parse_args()
    query = _query(args)
    try:
        with db_session_adapter.read_session() as db:
            preview = preview_existing_replacement_tax_correction(db, query)
            permission_granted = _permission_granted(db, args.actor_system_user_id)
        if not args.apply:
            print(
                json.dumps(
                    {"applied": False, "preview": _preview_dict(preview)},
                    sort_keys=True,
                )
            )
            return 0
        if not all(
            (
                args.expected_preview_fingerprint,
                args.actor,
                args.actor_system_user_id,
                args.idempotency_key,
            )
        ):
            print(
                json.dumps(
                    {
                        "applied": False,
                        "error": (
                            "--apply requires preview fingerprint, actor, staff ID, "
                            "and idempotency key"
                        ),
                        "preview": _preview_dict(preview),
                    },
                    sort_keys=True,
                )
            )
            return 2
        command_id = args.command_id or uuid4()
        with db_session_adapter.owner_command_session() as db:
            outcome = correct_historical_invoice_tax_using_existing_replacement(
                db,
                CorrectExistingReplacementTaxInvoiceCommand(
                    query=query,
                    expected_preview_fingerprint=args.expected_preview_fingerprint,
                    permission_granted=permission_granted,
                    authorized_system_user_id=args.actor_system_user_id,
                ),
                context=CommandContext.system(
                    actor=args.actor,
                    scope=CORRECTION_SCOPE,
                    reason=args.reason,
                    command_id=command_id,
                    correlation_id=command_id,
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
                "source_invoice_id": str(outcome.source_invoice_id),
                "source_invoice_closure_id": str(outcome.source_invoice_closure_id),
                "source_payment_allocation_id": str(
                    outcome.source_payment_allocation_id
                ),
                "replacement_invoice_id": str(outcome.replacement_invoice_id),
                "replacement_payment_allocation_id": str(
                    outcome.replacement_payment_allocation_id
                ),
                "payment_id": str(outcome.payment_id),
                "subtotal": str(outcome.subtotal),
                "tax_amount": str(outcome.tax_amount),
                "replacement_total": str(outcome.replacement_total),
                "remaining_credit": str(outcome.remaining_credit),
                "currency": outcome.currency,
                "approval_ticket": outcome.approval_ticket,
                "approver_name": outcome.approver_name,
                "approval_recorded_at": outcome.approval_recorded_at.isoformat(),
                "evidence_fingerprint": outcome.preview_fingerprint,
                "replayed": outcome.replayed,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
