#!/usr/bin/env python
"""Finance-reviewed repair of a paid prepaid invoice's service period.

Adapter for ``financial.prepaid_paid_invoice_period_repair``; the owner makes
every decision. Use it for a ``malformed_paid_invoice_period`` prepaid coverage
quarantine (docs/runbooks/PREPAID_COVERAGE_QUARANTINE_FINANCE_REVIEW.md).

Four-eyes flow; requester and approver must be DIFFERENT active staff who both
hold ``billing:prepaid_reconciliation:repair``:

    # 1. Read-only preview (writes nothing; exit 2 while blockers remain):
    poetry run python -m scripts.billing.repair_prepaid_paid_invoice_period preview \\
        --invoice-id <id> --line-id <id> --subscription-id <id> \\
        --period-start <ISO-8601 with offset> --period-end <ISO-8601 with offset> \\
        [--adopt-entitlement-id <id>] [--acknowledge-overlap <entitlement-id> ...] \\
        [--acknowledge-warning <warning> ...]

    # 2. Request (same proposal arguments, plus the preview fingerprint):
    ... request <proposal args> --fingerprint <sha256> --reason <text> \\
        --evidence-ref <ref> --evidence-sha256 <sha256> \\
        --actor <system-user-uuid> --idempotency-key <key>

    # 3. Approve (a different staff member restates the fingerprint):
    ... approve --request <request-id> --fingerprint <sha256> \\
        --approver <different-system-user-uuid> --idempotency-key <key> \\
        [--sole-approver-justification <text>]

    # Pending (or all) requests:
    ... list [--invoice-id <id>] [--include-applied]

Exit codes: 0 success, 2 preview has blockers, 3 the owner refused the command.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.prepaid_paid_invoice_period_repair import (
    REPAIR_PERMISSION,
    ApprovePaidInvoicePeriodRepairCommand,
    EntitlementDisposition,
    PaidInvoicePeriodRepairPreview,
    PaidInvoicePeriodRepairQuery,
    PaidInvoicePeriodRepairResult,
    PaidInvoicePeriodRepairWarning,
    RequestPaidInvoicePeriodRepairCommand,
    approve_paid_invoice_period_repair,
    list_paid_invoice_period_repair_requests,
    preview_paid_invoice_period_repair,
    request_paid_invoice_period_repair,
)

_EXIT_BLOCKED = 2
_EXIT_REFUSED = 3


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


def _warning(value: str) -> PaidInvoicePeriodRepairWarning:
    try:
        return PaidInvoicePeriodRepairWarning(value)
    except ValueError as exc:
        choices = ", ".join(item.value for item in PaidInvoicePeriodRepairWarning)
        raise argparse.ArgumentTypeError(f"warning must be one of: {choices}") from exc


def _emit(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _query(args: argparse.Namespace) -> PaidInvoicePeriodRepairQuery:
    adopted: UUID | None = args.adopt_entitlement_id
    return PaidInvoicePeriodRepairQuery(
        invoice_id=args.invoice_id,
        line_id=args.line_id,
        subscription_id=args.subscription_id,
        period_start=args.period_start,
        period_end=args.period_end,
        disposition=(
            EntitlementDisposition.existing_entitlement_funds_line
            if adopted is not None
            else EntitlementDisposition.create_from_paid_line
        ),
        adopted_entitlement_id=adopted,
        acknowledged_overlapping_entitlement_ids=tuple(args.acknowledge_overlap),
        acknowledged_warnings=tuple(args.acknowledge_warning),
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _preview_payload(preview: PaidInvoicePeriodRepairPreview) -> dict[str, object]:
    effect = preview.quarantine_effect
    planned = preview.planned_entitlement
    return {
        "financial_state_changed": False,
        "actionable": preview.actionable,
        "fingerprint": preview.fingerprint,
        "invoice_id": str(preview.query.invoice_id),
        "invoice_number": preview.invoice_number,
        "account_id": str(preview.account_id),
        "currency": preview.currency,
        "before": {
            "billing_period_start": _iso(preview.before.billing_period_start),
            "billing_period_end": _iso(preview.before.billing_period_end),
            "line_subscription_id": (
                str(preview.before.line_subscription_id)
                if preview.before.line_subscription_id
                else None
            ),
            "line_period_start": _iso(preview.before.line_period_start),
            "line_period_end": _iso(preview.before.line_period_end),
        },
        "after": {
            "billing_period_start": _iso(preview.after.billing_period_start),
            "billing_period_end": _iso(preview.after.billing_period_end),
            "line_subscription_id": str(preview.after.line_subscription_id),
        },
        "planned_entitlement": {
            "disposition": planned.disposition.value,
            "existing_entitlement_id": (
                str(planned.existing_entitlement_id)
                if planned.existing_entitlement_id
                else None
            ),
            "starts_at": planned.starts_at.isoformat(),
            "ends_at": planned.ends_at.isoformat(),
            "amount_funded": str(planned.amount_funded),
            "currency": planned.currency,
        },
        "overlapping_entitlements": [
            {
                "entitlement_id": str(row.entitlement_id),
                "starts_at": row.starts_at.isoformat(),
                "ends_at": row.ends_at.isoformat(),
                "amount_funded": str(row.amount_funded),
                "source_invoice_line_id": (
                    str(row.source_invoice_line_id)
                    if row.source_invoice_line_id
                    else None
                ),
                "source_ledger_entry_id": (
                    str(row.source_ledger_entry_id)
                    if row.source_ledger_entry_id
                    else None
                ),
                "acknowledged": row.acknowledged,
                "adopted": row.adopted,
            }
            for row in preview.overlapping_entitlements
        ],
        "terms": {
            "line_amount": str(preview.terms.line_amount),
            "line_kind": preview.terms.line_kind,
            "subscription_unit_price": (
                str(preview.terms.subscription_unit_price)
                if preview.terms.subscription_unit_price is not None
                else None
            ),
            "billing_cycle": preview.terms.billing_cycle,
            "one_cycle_end": preview.terms.one_cycle_end.isoformat(),
        },
        "settlement": {
            "invoice_total": str(preview.settlement.invoice_total),
            "balance_due": str(preview.settlement.balance_due),
            "allocated_payments": str(preview.settlement.allocated_payments),
            "applied_credit_notes": str(preview.settlement.applied_credit_notes),
        },
        "warnings": [value.value for value in preview.warnings],
        "blockers": [value.value for value in preview.blockers],
        "quarantine_effect": {
            "as_of": effect.as_of.isoformat(),
            "work_item_open": effect.work_item_open,
            "current_blocking_reasons": [
                value.value for value in effect.current_blocking_reasons
            ],
            "target_reason_before": (
                effect.target_reason_before.value
                if effect.target_reason_before
                else None
            ),
            "target_reason_after": (
                effect.target_reason_after.value if effect.target_reason_after else None
            ),
            "other_malformed_invoice_ids": [
                str(value) for value in effect.other_malformed_invoice_ids
            ],
            "projected_blocking_reasons": [
                value.value for value in effect.projected_blocking_reasons
            ],
            "work_item_resolves_on_next_sweep": (
                effect.work_item_resolves_on_next_sweep
            ),
        },
    }


def _result_payload(result: PaidInvoicePeriodRepairResult) -> dict[str, object]:
    return {
        "request_id": str(result.request_id),
        "status": result.status.value,
        "invoice_id": str(result.invoice_id),
        "line_id": str(result.line_id),
        "subscription_id": str(result.subscription_id),
        "preview_fingerprint": result.preview_fingerprint,
        "billing_period_start": result.billing_period_start.isoformat(),
        "billing_period_end": result.billing_period_end.isoformat(),
        "disposition": result.disposition.value,
        "entitlement_id": str(result.entitlement_id) if result.entitlement_id else None,
        "projected_blocking_reasons": [
            value.value for value in result.projected_blocking_reasons
        ],
        "replayed": result.replayed,
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
    return bool(has_permission(auth, db, REPAIR_PERMISSION))


def _staff_context(
    *, reason: str, idempotency_key: str, system_user_id: UUID
) -> CommandContext:
    return CommandContext.system(
        actor=f"user:{system_user_id}",
        scope=REPAIR_PERMISSION,
        reason=reason,
        idempotency_key=idempotency_key,
    )


def _cmd_preview(args: argparse.Namespace) -> int:
    with db_session_adapter.read_session() as db:
        preview = preview_paid_invoice_period_repair(db, _query(args))
        _emit(_preview_payload(preview))
    return 0 if preview.actionable else _EXIT_BLOCKED


def _cmd_request(args: argparse.Namespace) -> int:
    with db_session_adapter.owner_command_session() as db:
        granted = _permission_granted(db, system_user_id=args.actor)
        db_session_adapter.release_read_transaction(db)
        result = request_paid_invoice_period_repair(
            db,
            RequestPaidInvoicePeriodRepairCommand(
                query=_query(args),
                preview_fingerprint=args.fingerprint,
                reason=args.reason,
                evidence_reference=args.evidence_ref,
                evidence_sha256=args.evidence_sha256,
                requested_by=args.actor,
                permission_granted=granted,
            ),
            context=_staff_context(
                reason=args.reason,
                idempotency_key=args.idempotency_key,
                system_user_id=args.actor,
            ),
        )
    _emit({**_result_payload(result), "financial_state_changed": False})
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    with db_session_adapter.owner_command_session() as db:
        granted = _permission_granted(db, system_user_id=args.approver)
        db_session_adapter.release_read_transaction(db)
        result = approve_paid_invoice_period_repair(
            db,
            ApprovePaidInvoicePeriodRepairCommand(
                request_id=args.request,
                preview_fingerprint=args.fingerprint,
                approved_by=args.approver,
                permission_granted=granted,
                sole_approver_justification=args.sole_approver_justification,
            ),
            context=_staff_context(
                reason=f"four-eyes approval of paid invoice period repair {args.request}",
                idempotency_key=args.idempotency_key,
                system_user_id=args.approver,
            ),
        )
    _emit(_result_payload(result))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    with db_session_adapter.read_session() as db:
        rows = list_paid_invoice_period_repair_requests(
            db, invoice_id=args.invoice_id, include_applied=args.include_applied
        )
        _emit(
            {
                "count": len(rows),
                "requests": [
                    {
                        "request_id": str(row.request_id),
                        "status": row.status.value,
                        "invoice_id": str(row.query.invoice_id),
                        "line_id": str(row.query.line_id),
                        "subscription_id": str(row.query.subscription_id),
                        "period_start": row.query.period_start.isoformat(),
                        "period_end": row.query.period_end.isoformat(),
                        "disposition": row.query.disposition.value,
                        "preview_fingerprint": row.preview_fingerprint,
                        "evidence_reference": row.evidence_reference,
                        "requested_by": str(row.requested_by),
                        "requested_at": row.requested_at.isoformat(),
                        "approved_by": str(row.approved_by)
                        if row.approved_by
                        else None,
                        "entitlement_id": (
                            str(row.entitlement_id) if row.entitlement_id else None
                        ),
                    }
                    for row in rows
                ],
            }
        )
    return 0


def _add_proposal_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--invoice-id", type=_uuid, required=True)
    parser.add_argument("--line-id", type=_uuid, required=True)
    parser.add_argument("--subscription-id", type=_uuid, required=True)
    parser.add_argument("--period-start", type=_timestamp, required=True)
    parser.add_argument("--period-end", type=_timestamp, required=True)
    parser.add_argument(
        "--adopt-entitlement-id",
        type=_uuid,
        default=None,
        help=(
            "an existing entitlement that already funds this payment; no new "
            "entitlement is created"
        ),
    )
    parser.add_argument(
        "--acknowledge-overlap",
        type=_uuid,
        action="append",
        default=[],
        help="retain this overlapping entitlement (repeatable)",
    )
    parser.add_argument(
        "--acknowledge-warning",
        type=_warning,
        action="append",
        default=[],
        help="acknowledge a previewed warning (repeatable)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    preview = sub.add_parser("preview", help="read-only validation and fingerprint")
    _add_proposal_arguments(preview)
    preview.set_defaults(func=_cmd_preview)

    request = sub.add_parser("request", help="step 1: record a reviewed proposal")
    _add_proposal_arguments(request)
    request.add_argument("--fingerprint", required=True)
    request.add_argument("--reason", required=True)
    request.add_argument("--evidence-ref", required=True)
    request.add_argument("--evidence-sha256", required=True)
    request.add_argument("--actor", type=_uuid, required=True)
    request.add_argument("--idempotency-key", required=True)
    request.set_defaults(func=_cmd_request)

    approve = sub.add_parser("approve", help="step 2: a different staff member applies")
    approve.add_argument("--request", type=_uuid, required=True)
    approve.add_argument("--fingerprint", required=True)
    approve.add_argument("--approver", type=_uuid, required=True)
    approve.add_argument("--idempotency-key", required=True)
    approve.add_argument(
        "--sole-approver-justification",
        default=None,
        help="justification for approving your own request under the governed sole-approver exception (see docs/runbooks/SOLE_APPROVER_EXCEPTION.md); refused unless the exception is enabled, unexpired and names you",
    )
    approve.set_defaults(func=_cmd_approve)

    listing = sub.add_parser("list", help="read-only: pending (or all) requests")
    listing.add_argument("--invoice-id", type=_uuid, default=None)
    listing.add_argument("--include-applied", action="store_true")
    listing.set_defaults(func=_cmd_list)
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
