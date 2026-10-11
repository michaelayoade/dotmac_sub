#!/usr/bin/env python
"""Preview or apply one tax-inclusive prepaid renewal document correction."""

from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal, InvalidOperation
from uuid import UUID

from app.models.system_user import SystemUser
from app.services.auth_dependencies import has_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.prepaid_service_renewals import (
    TAX_INCLUSIVE_RENEWAL_DOCUMENT_CORRECTION_SCOPE,
    CorrectTaxInclusiveRenewalDocumentCommand,
    TaxInclusiveRenewalDocumentCorrectionQuery,
    correct_tax_inclusive_renewal_document,
    preview_tax_inclusive_renewal_document_correction,
)
from app.services.system_user_assignments import system_user_role_names

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,119}$")


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("identifier must be a UUID") from exc


def _money(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("money must be a decimal amount") from exc
    if not parsed.is_finite():
        raise argparse.ArgumentTypeError("money must be finite")
    return parsed


def _permission_granted(db, actor_user_id: UUID) -> bool:
    user = db.get(SystemUser, actor_user_id)
    if user is None or not user.is_active:
        return False
    auth = {
        "principal_id": str(actor_user_id),
        "principal_type": "system_user",
        "roles": set(system_user_role_names(db, actor_user_id)),
    }
    return has_permission(
        auth,
        db,
        TAX_INCLUSIVE_RENEWAL_DOCUMENT_CORRECTION_SCOPE,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=_uuid, required=True)
    parser.add_argument("--subscription-id", type=_uuid, required=True)
    parser.add_argument("--adjustment-id", type=_uuid, required=True)
    parser.add_argument("--entitlement-id", type=_uuid, required=True)
    parser.add_argument("--payment-id", type=_uuid, required=True)
    parser.add_argument("--expected-invoice-total", type=_money, required=True)
    parser.add_argument("--expected-remaining-credit", type=_money, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--preview-fingerprint")
    parser.add_argument("--actor-user-id", type=_uuid)
    parser.add_argument("--reason-evidence-reference")
    parser.add_argument("--permission-evidence")
    parser.add_argument("--idempotency-key")
    args = parser.parse_args()
    query = TaxInclusiveRenewalDocumentCorrectionQuery(
        account_id=args.account_id,
        subscription_id=args.subscription_id,
        adjustment_id=args.adjustment_id,
        entitlement_id=args.entitlement_id,
        payment_id=args.payment_id,
        expected_invoice_total=args.expected_invoice_total,
        expected_remaining_credit=args.expected_remaining_credit,
    )

    if not args.apply:
        with db_session_adapter.read_session() as db:
            preview = preview_tax_inclusive_renewal_document_correction(db, query)
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "operation": "correct_tax_inclusive_renewal_document",
                    "disposition": preview.disposition.value,
                    "actionable": preview.actionable,
                    "period_start": (
                        preview.period_start.isoformat()
                        if preview.period_start is not None
                        else None
                    ),
                    "period_end": (
                        preview.period_end.isoformat()
                        if preview.period_end is not None
                        else None
                    ),
                    "currency": preview.currency,
                    "subtotal": str(preview.subtotal),
                    "tax_total": str(preview.tax_total),
                    "invoice_total": str(preview.invoice_total),
                    "credit_before": str(preview.credit_before),
                    "temporary_credit_after_reversal": str(
                        preview.temporary_credit_after_reversal
                    ),
                    "selected_payment_available": str(
                        preview.selected_payment_available
                    ),
                    "reason": preview.reason,
                    "fingerprint": preview.fingerprint,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    required = {
        "--preview-fingerprint": args.preview_fingerprint,
        "--actor-user-id": args.actor_user_id,
        "--reason-evidence-reference": args.reason_evidence_reference,
        "--permission-evidence": args.permission_evidence,
        "--idempotency-key": args.idempotency_key,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        parser.error("--apply requires " + ", ".join(missing))
    if not _KEY_RE.fullmatch(args.idempotency_key):
        parser.error("--idempotency-key must be 16-120 safe characters")

    assert args.actor_user_id is not None
    with db_session_adapter.owner_command_session() as db:
        if not _permission_granted(db, args.actor_user_id):
            parser.error("selected active user lacks billing:ledger:write")
        db_session_adapter.release_read_transaction(db)
        result = correct_tax_inclusive_renewal_document(
            db,
            CorrectTaxInclusiveRenewalDocumentCommand(
                context=CommandContext.system(
                    actor=f"user:{args.actor_user_id}",
                    scope=TAX_INCLUSIVE_RENEWAL_DOCUMENT_CORRECTION_SCOPE,
                    reason=args.reason_evidence_reference,
                    idempotency_key=args.idempotency_key,
                ),
                query=query,
                preview_fingerprint=args.preview_fingerprint,
                permission_evidence=args.permission_evidence,
            ),
        )
    print(
        json.dumps(
            {
                "invoice_id": str(result.invoice_id),
                "invoice_number": result.invoice_number,
                "payment_allocation_id": str(result.payment_allocation_id),
                "replacement_entitlement_id": str(result.replacement_entitlement_id),
                "replaced_entitlement_id": str(result.replaced_entitlement_id),
                "reversal_ledger_entry_id": str(result.reversal_ledger_entry_id),
                "invoice_total": str(result.invoice_total),
                "remaining_credit": str(result.remaining_credit),
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
