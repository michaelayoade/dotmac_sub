#!/usr/bin/env python
"""Preview or apply one legacy payment-backed prepaid invoice repair.

Preview is the default and writes nothing. Apply requires the exact preview
fingerprint, a real authorized user, non-secret approval evidence, a reason
reference, and a bounded idempotency key.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from app.models.system_user import SystemUser
from app.services.auth_dependencies import has_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.prepaid_service_renewals import (
    LEGACY_PAYMENT_BACKED_RENEWAL_INVOICE_REPAIR_SCOPE,
    LegacyPaymentBackedRenewalInvoiceRepairQuery,
    RepairLegacyPaymentBackedRenewalInvoiceCommand,
    preview_legacy_payment_backed_renewal_invoice_repair,
    repair_legacy_payment_backed_renewal_invoice,
)
from app.services.system_user_assignments import system_user_role_names
from scripts.billing._mutation_policy import refuse_apply

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,119}$")


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("identifier must be a UUID") from exc


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


def _query(args: argparse.Namespace) -> LegacyPaymentBackedRenewalInvoiceRepairQuery:
    return LegacyPaymentBackedRenewalInvoiceRepairQuery(
        account_id=args.account_id,
        subscription_id=args.subscription_id,
        payment_id=args.payment_id,
        ledger_entry_id=args.ledger_entry_id,
        entitlement_id=args.entitlement_id,
        expected_period_start=args.expected_period_start,
        expected_period_end=args.expected_period_end,
        expected_gross_total=args.expected_gross_total,
        expected_currency=args.expected_currency,
        expected_available_funding_after_repair=(
            args.expected_available_funding_after_repair
        ),
    )


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
        LEGACY_PAYMENT_BACKED_RENEWAL_INVOICE_REPAIR_SCOPE,
    )


def _preview_payload(preview) -> dict[str, object]:
    return {
        "dry_run": True,
        "operation": "repair_legacy_payment_backed_renewal_invoice",
        "account_id": str(preview.account_id),
        "subscription_id": str(preview.subscription_id),
        "payment_id": str(preview.payment_id),
        "ledger_entry_id": str(preview.ledger_entry_id),
        "entitlement_id": str(preview.entitlement_id),
        "disposition": preview.disposition.value,
        "actionable": preview.actionable,
        "period_start": preview.period_start.isoformat(),
        "period_end": preview.period_end.isoformat(),
        "currency": preview.currency,
        "ledger_category": (
            preview.ledger_category.value if preview.ledger_category else None
        ),
        "subtotal": str(preview.subtotal),
        "tax_total": str(preview.tax_total),
        "tax_rate_id": str(preview.tax_rate_id) if preview.tax_rate_id else None,
        "tax_application": preview.tax_application.value,
        "gross_total": str(preview.gross_total),
        "ledger_amount": str(preview.ledger_amount),
        "entitlement_amount": str(preview.entitlement_amount),
        "available_funding_before": str(preview.available_funding_before),
        "available_funding_after": str(preview.available_funding_after),
        "selected_payment_available": str(preview.selected_payment_available),
        "settlement_id": str(preview.settlement_id) if preview.settlement_id else None,
        "billing_anchor_before": (
            preview.billing_anchor_before.isoformat()
            if preview.billing_anchor_before
            else None
        ),
        "existing_invoice_id": (
            str(preview.existing_invoice_id) if preview.existing_invoice_id else None
        ),
        "reason": preview.reason,
        "fingerprint": preview.fingerprint,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=_uuid, required=True)
    parser.add_argument("--subscription-id", type=_uuid, required=True)
    parser.add_argument("--payment-id", type=_uuid, required=True)
    parser.add_argument("--ledger-entry-id", type=_uuid, required=True)
    parser.add_argument("--entitlement-id", type=_uuid, required=True)
    parser.add_argument("--expected-period-start", type=_timestamp, required=True)
    parser.add_argument("--expected-period-end", type=_timestamp, required=True)
    parser.add_argument("--expected-gross-total", type=_money, required=True)
    parser.add_argument("--expected-currency", required=True)
    parser.add_argument(
        "--expected-available-funding-after-repair", type=_money, required=True
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--preview-fingerprint")
    parser.add_argument("--actor-user-id", type=_uuid)
    parser.add_argument("--permission-evidence")
    parser.add_argument("--reason-evidence-reference")
    parser.add_argument("--idempotency-key")
    args = parser.parse_args()
    refuse_apply(args.apply)
    query = _query(args)

    if not args.apply:
        with db_session_adapter.read_session() as db:
            preview = preview_legacy_payment_backed_renewal_invoice_repair(db, query)
        print(json.dumps(_preview_payload(preview), indent=2, sort_keys=True))
        return 0

    required = {
        "--preview-fingerprint": args.preview_fingerprint,
        "--actor-user-id": args.actor_user_id,
        "--permission-evidence": args.permission_evidence,
        "--reason-evidence-reference": args.reason_evidence_reference,
        "--idempotency-key": args.idempotency_key,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        parser.error("--apply requires " + ", ".join(missing))
    if not _KEY_RE.fullmatch(args.idempotency_key):
        parser.error(
            "--idempotency-key must be 16-120 safe characters "
            "(letters, digits, dot, underscore, colon, or hyphen)"
        )

    assert args.actor_user_id is not None
    with db_session_adapter.owner_command_session() as db:
        permission_granted = _permission_granted(db, args.actor_user_id)
        db_session_adapter.release_read_transaction(db)
        result = repair_legacy_payment_backed_renewal_invoice(
            db,
            RepairLegacyPaymentBackedRenewalInvoiceCommand(
                context=CommandContext.system(
                    actor=f"user:{args.actor_user_id}",
                    scope=LEGACY_PAYMENT_BACKED_RENEWAL_INVOICE_REPAIR_SCOPE,
                    reason=args.reason_evidence_reference,
                    idempotency_key=args.idempotency_key,
                ),
                query=query,
                preview_fingerprint=args.preview_fingerprint,
                actor_user_id=args.actor_user_id,
                permission_granted=permission_granted,
                permission_evidence=args.permission_evidence,
            ),
        )
    print(
        json.dumps(
            {
                "invoice_id": str(result.invoice_id),
                "invoice_number": result.invoice_number,
                "invoice_line_id": str(result.invoice_line_id),
                "payment_allocation_id": str(result.payment_allocation_id),
                "replacement_entitlement_id": str(result.replacement_entitlement_id),
                "replaced_entitlement_id": str(result.replaced_entitlement_id),
                "legacy_ledger_entry_id": str(result.legacy_ledger_entry_id),
                "reversal_ledger_entry_id": str(result.reversal_ledger_entry_id),
                "audit_event_id": str(result.audit_event_id),
                "domain_event_id": str(result.domain_event_id),
                "period_start": result.period_start.isoformat(),
                "period_end": result.period_end.isoformat(),
                "gross_total": str(result.gross_total),
                "currency": result.currency,
                "available_funding_before": str(result.available_funding_before),
                "available_funding_after": str(result.available_funding_after),
                "billing_anchor_before": result.billing_anchor_before.isoformat(),
                "billing_anchor_after": result.billing_anchor_after.isoformat(),
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
