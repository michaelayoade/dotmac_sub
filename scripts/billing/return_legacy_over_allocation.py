#!/usr/bin/env python
"""Finance-reviewed return of a legacy over-allocation to account credit.

Adapter for ``financial.legacy_over_allocation_correction``; the owner makes
every decision. Use it when a PAID invoice carries a legacy (Splynx-era)
payment allocation that exceeds its total and the existing reviewed
payment-allocation reversal refuses it ("Allocation lacks paired ledger
evidence"). See docs/runbooks/LEGACY_OVER_ALLOCATION_RETURN.md.

Two steps: a read-only preview, then a confirmation bound to its fingerprint.
The operator restates the exact amounts Finance approved.

    # 1. Read-only preview (writes nothing; exit 2 while blockers remain):
    poetry run python -m scripts.billing.return_legacy_over_allocation preview \\
        --allocation-id <id> --amount <over-allocation> \\
        --invoice-total <invoice total> --remaining-settlement <total>

    # 2. Confirm (same arguments, plus the preview fingerprint):
    ... confirm <same arguments> --fingerprint <sha256> --reason <text> \\
        --evidence-ref <ref> --evidence-sha256 <sha256> \\
        --actor <system-user-uuid> --idempotency-key <key>

The actor must hold ``billing:payment:update``.
Exit codes: 0 success, 2 preview has blockers, 3 the owner refused the command.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.billing.legacy_over_allocation_correction import (
    CORRECTION_PERMISSION,
    LegacyOverAllocationPreview,
    LegacyOverAllocationQuery,
    LegacyOverAllocationResult,
    ReturnLegacyOverAllocationCommand,
    preview_legacy_over_allocation_return,
    return_legacy_over_allocation,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

_EXIT_BLOCKED = 2
_EXIT_REFUSED = 3


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("identifier must be a UUID") from exc


def _money(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("amount must be a decimal") from exc
    if not amount.is_finite() or amount < 0:
        raise argparse.ArgumentTypeError("amount must be a non-negative decimal")
    return amount


def _emit(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _query(args: argparse.Namespace) -> LegacyOverAllocationQuery:
    return LegacyOverAllocationQuery(
        allocation_id=args.allocation_id,
        expected_amount=args.amount,
        expected_invoice_total=args.invoice_total,
        expected_remaining_settlement=args.remaining_settlement,
    )


def _preview_payload(preview: LegacyOverAllocationPreview) -> dict[str, object]:
    return {
        "financial_state_changed": False,
        "actionable": preview.actionable,
        "fingerprint": preview.fingerprint,
        "allocation_id": str(preview.allocation_id),
        "payment_id": str(preview.payment_id),
        "invoice_id": str(preview.invoice_id),
        "invoice_number": preview.invoice_number,
        "account_id": str(preview.account_id),
        "currency": preview.currency,
        "allocation_amount": str(preview.allocation_amount),
        "payment_amount": str(preview.payment_amount),
        "invoice": {
            "status": preview.invoice_status,
            "total": str(preview.invoice_total),
            "balance_due": str(preview.invoice_balance_due),
            "settled_before": str(preview.settled_before),
            "settled_after": str(preview.settled_after),
            "applied_credit_notes": str(preview.applied_credit_notes),
            "stays_paid": preview.settled_after >= preview.invoice_total,
        },
        "remaining_allocations": [
            {
                "allocation_id": str(row.allocation_id),
                "payment_id": str(row.payment_id),
                "amount": str(row.amount),
                "has_ledger_evidence": row.has_ledger_evidence,
            }
            for row in preview.remaining_allocations
        ],
        "payment_unallocated": {
            "before": str(preview.payment_unallocated_before),
            "after": str(preview.payment_unallocated_after),
        },
        "ledger": {
            "account_credit_ledger_entry_id": (
                str(preview.ledger.ledger_entry_id)
                if preview.ledger.ledger_entry_id
                else None
            ),
            "account_credit_amount": (
                str(preview.ledger.amount) if preview.ledger.amount else None
            ),
            "active_payment_entry_count": preview.ledger.active_payment_entry_count,
            "consumption_debit_count": preview.ledger.consumption_debit_count,
            "account_credit_before": str(preview.account_credit_before),
            "account_credit_after": str(preview.account_credit_after),
            "postings": list(preview.ledger_postings),
        },
        "blockers": [value.value for value in preview.blockers],
    }


def _result_payload(result: LegacyOverAllocationResult) -> dict[str, object]:
    return {
        "correction_id": str(result.correction_id),
        "allocation_id": str(result.allocation_id),
        "payment_id": str(result.payment_id),
        "invoice_id": str(result.invoice_id),
        "account_id": str(result.account_id),
        "amount": str(result.amount),
        "currency": result.currency,
        "account_credit_ledger_entry_id": str(result.account_credit_ledger_entry_id),
        "preview_fingerprint": result.preview_fingerprint,
        "replayed": result.replayed,
        "economic_delta": "0.00",
    }


def _permission_granted(db: Session, *, system_user_id: UUID) -> bool:
    """Resolve a real staff principal's RBAC grant (mirrors other repair CLIs)."""
    from app.models.system_user import SystemUser
    from app.services.auth_dependencies import has_permission
    from app.services.system_user_assignments import system_user_role_names

    system_user = db.get(SystemUser, system_user_id)
    if system_user is None or not system_user.is_active:
        return False
    auth = {
        "principal_id": str(system_user_id),
        "principal_type": "system_user",
        "roles": set(system_user_role_names(db, system_user_id)),
    }
    return bool(has_permission(auth, db, CORRECTION_PERMISSION))


def _cmd_preview(args: argparse.Namespace) -> int:
    with db_session_adapter.read_session() as db:
        preview = preview_legacy_over_allocation_return(db, _query(args))
        _emit(_preview_payload(preview))
    return 0 if preview.actionable else _EXIT_BLOCKED


def _cmd_confirm(args: argparse.Namespace) -> int:
    with db_session_adapter.owner_command_session() as db:
        granted = _permission_granted(db, system_user_id=args.actor)
        db_session_adapter.release_read_transaction(db)
        result = return_legacy_over_allocation(
            db,
            ReturnLegacyOverAllocationCommand(
                query=_query(args),
                preview_fingerprint=args.fingerprint,
                reason=args.reason,
                evidence_reference=args.evidence_ref,
                evidence_sha256=args.evidence_sha256,
                reviewed_by=args.actor,
                permission_granted=granted,
            ),
            context=CommandContext.system(
                actor=f"user:{args.actor}",
                scope=CORRECTION_PERMISSION,
                reason=args.reason,
                idempotency_key=args.idempotency_key,
            ),
        )
    _emit(_result_payload(result))
    return 0


def _add_proposal_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--allocation-id", type=_uuid, required=True)
    parser.add_argument(
        "--amount",
        type=_money,
        required=True,
        help="the allocation being returned to account credit",
    )
    parser.add_argument("--invoice-total", type=_money, required=True)
    parser.add_argument(
        "--remaining-settlement",
        type=_money,
        required=True,
        help="active allocations excluding this one, plus applied credit notes",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    preview = sub.add_parser("preview", help="read-only validation and fingerprint")
    _add_proposal_arguments(preview)
    preview.set_defaults(func=_cmd_preview)

    confirm = sub.add_parser("confirm", help="apply the previewed return")
    _add_proposal_arguments(confirm)
    confirm.add_argument("--fingerprint", required=True)
    confirm.add_argument("--reason", required=True)
    confirm.add_argument("--evidence-ref", required=True)
    confirm.add_argument("--evidence-sha256", required=True)
    confirm.add_argument("--actor", type=_uuid, required=True)
    confirm.add_argument("--idempotency-key", required=True)
    confirm.set_defaults(func=_cmd_confirm)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except DomainError as exc:
        _emit(
            {
                "error": exc.code,
                "message": exc.message,
                "details": dict(exc.details),
                "financial_state_changed": False,
            }
        )
        return _EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
