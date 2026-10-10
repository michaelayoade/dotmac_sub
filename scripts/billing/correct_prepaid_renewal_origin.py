#!/usr/bin/env python
"""Finance-reviewed correction of a malformed prepaid-renewal adjustment reference.

Adapter for ``financial.prepaid_renewal_origin_correction``; the owner makes
every decision. Use it for a ``malformed_renewal_origin`` prepaid coverage
quarantine when Finance has decided the renewal debit is legitimate and only
its ``origin_ref`` is wrong
(docs/runbooks/PREPAID_COVERAGE_QUARANTINE_FINANCE_REVIEW.md). No money moves.

Two steps: a read-only preview, then a confirmation bound to its fingerprint.

    # 1. Read-only preview (writes nothing; exit 2 while blockers remain):
    poetry run python -m scripts.billing.correct_prepaid_renewal_origin preview \\
        --adjustment-id <id> \\
        --disposition entitlement_already_linked|link_existing_entitlement \\
        --entitlement-id <id> [--acknowledge-warning <warning> ...]
    poetry run python -m scripts.billing.correct_prepaid_renewal_origin preview \\
        --adjustment-id <id> --disposition create_entitlement_from_debit \\
        --subscription-id <id> --period-start <ISO-8601 with offset> \\
        --period-end <ISO-8601 with offset> \\
        [--acknowledge-overlap <entitlement-id> ...] \\
        [--acknowledge-warning <warning> ...]

    # 2. Confirm (same proposal arguments, plus the preview fingerprint):
    ... confirm <proposal args> --fingerprint <sha256> --reason <text> \\
        --evidence-ref <ref> --evidence-sha256 <sha256> \\
        --actor <system-user-uuid> --idempotency-key <key>

The actor must hold ``billing:prepaid_reconciliation:repair``.
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
from app.services.prepaid_renewal_origin_correction import (
    CORRECTION_PERMISSION,
    CorrectRenewalOriginCommand,
    RenewalOriginCorrectionPreview,
    RenewalOriginCorrectionQuery,
    RenewalOriginCorrectionResult,
    RenewalOriginDisposition,
    RenewalOriginWarning,
    correct_renewal_origin,
    preview_renewal_origin_correction,
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


def _warning(value: str) -> RenewalOriginWarning:
    try:
        return RenewalOriginWarning(value)
    except ValueError as exc:
        choices = ", ".join(item.value for item in RenewalOriginWarning)
        raise argparse.ArgumentTypeError(f"warning must be one of: {choices}") from exc


def _disposition(value: str) -> RenewalOriginDisposition:
    try:
        return RenewalOriginDisposition(value)
    except ValueError as exc:
        choices = ", ".join(item.value for item in RenewalOriginDisposition)
        raise argparse.ArgumentTypeError(
            f"disposition must be one of: {choices}"
        ) from exc


def _emit(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _query(args: argparse.Namespace) -> RenewalOriginCorrectionQuery:
    return RenewalOriginCorrectionQuery(
        adjustment_id=args.adjustment_id,
        disposition=args.disposition,
        entitlement_id=args.entitlement_id,
        subscription_id=args.subscription_id,
        period_start=args.period_start,
        period_end=args.period_end,
        acknowledged_overlapping_entitlement_ids=tuple(args.acknowledge_overlap),
        acknowledged_warnings=tuple(args.acknowledge_warning),
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _entitlement_payload(state: object) -> dict[str, object] | None:
    from app.services.prepaid_renewal_origin_correction import EntitlementState

    if not isinstance(state, EntitlementState):
        return None
    return {
        "entitlement_id": str(state.entitlement_id),
        "subscription_id": str(state.subscription_id),
        "status": state.status,
        "starts_at": state.starts_at.isoformat(),
        "ends_at": state.ends_at.isoformat(),
        "amount_funded": str(state.amount_funded),
        "currency": state.currency,
        "source_invoice_id": (
            str(state.source_invoice_id) if state.source_invoice_id else None
        ),
        "source_ledger_entry_id": (
            str(state.source_ledger_entry_id) if state.source_ledger_entry_id else None
        ),
    }


def _preview_payload(preview: RenewalOriginCorrectionPreview) -> dict[str, object]:
    effect = preview.quarantine_effect
    planned = preview.planned
    impact = preview.position_impact
    return {
        "financial_state_changed": False,
        "actionable": preview.actionable,
        "fingerprint": preview.fingerprint,
        "adjustment_id": str(preview.adjustment.adjustment_id),
        "account_id": str(preview.account_id),
        "subscription_id": (
            str(preview.subscription_id) if preview.subscription_id else None
        ),
        "amount": str(preview.adjustment.amount),
        "currency": preview.adjustment.currency,
        "ledger_entry_id": str(preview.adjustment.ledger_entry_id),
        "origin_ref_before": preview.origin_ref_before,
        "origin_ref_after": preview.origin_ref_after,
        "entitlement": _entitlement_payload(preview.entitlement),
        "planned_entitlement_action": {
            "action": planned.action.value,
            "entitlement_id": (
                str(planned.entitlement_id) if planned.entitlement_id else None
            ),
            "subscription_id": (
                str(planned.subscription_id) if planned.subscription_id else None
            ),
            "starts_at": _iso(planned.starts_at),
            "ends_at": _iso(planned.ends_at),
            "amount_funded": (
                str(planned.amount_funded)
                if planned.amount_funded is not None
                else None
            ),
            "currency": planned.currency,
        },
        "overlapping_entitlements": [
            _entitlement_payload(row) for row in preview.overlapping_entitlements
        ],
        "warnings": [value.value for value in preview.warnings],
        "blockers": [value.value for value in preview.blockers],
        "position_impact": {
            "invoices_made_documentary": [
                str(value) for value in impact.invoices_made_documentary
            ],
            "prepaid_available_balance_before": (
                str(impact.prepaid_available_balance_before)
                if impact.prepaid_available_balance_before is not None
                else None
            ),
            "prepaid_available_balance_after": (
                str(impact.prepaid_available_balance_after)
                if impact.prepaid_available_balance_after is not None
                else None
            ),
            "coverage_end_before": _iso(impact.coverage_end_before),
            "coverage_end_after": _iso(impact.coverage_end_after),
        },
        "quarantine_effect": {
            "as_of": effect.as_of.isoformat(),
            "work_item_open": effect.work_item_open,
            "current_blocking_reasons": [
                value.value for value in effect.current_blocking_reasons
            ],
            "malformed_adjustment_ids_before": [
                str(value) for value in effect.malformed_adjustment_ids_before
            ],
            "malformed_adjustment_ids_after": [
                str(value) for value in effect.malformed_adjustment_ids_after
            ],
            "corrected_period_is_current": effect.corrected_period_is_current,
            "projected_blocking_reasons": [
                value.value for value in effect.projected_blocking_reasons
            ],
            "work_item_resolves_on_next_sweep": (
                effect.work_item_resolves_on_next_sweep
            ),
        },
    }


def _result_payload(result: RenewalOriginCorrectionResult) -> dict[str, object]:
    return {
        "correction_id": str(result.correction_id),
        "adjustment_id": str(result.adjustment_id),
        "account_id": str(result.account_id),
        "disposition": result.disposition.value,
        "origin_ref_before": result.origin_ref_before,
        "origin_ref_after": result.origin_ref_after,
        "entitlement_id": str(result.entitlement_id) if result.entitlement_id else None,
        "entitlement_action": result.entitlement_action.value,
        "preview_fingerprint": result.preview_fingerprint,
        "projected_blocking_reasons": [
            value.value for value in result.projected_blocking_reasons
        ],
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
        preview = preview_renewal_origin_correction(db, _query(args))
        _emit(_preview_payload(preview))
    return 0 if preview.actionable else _EXIT_BLOCKED


def _cmd_confirm(args: argparse.Namespace) -> int:
    with db_session_adapter.owner_command_session() as db:
        granted = _permission_granted(db, system_user_id=args.actor)
        db_session_adapter.release_read_transaction(db)
        result = correct_renewal_origin(
            db,
            CorrectRenewalOriginCommand(
                query=_query(args),
                preview_fingerprint=args.fingerprint,
                reason=args.reason,
                evidence_reference=args.evidence_ref,
                evidence_sha256=args.evidence_sha256,
                corrected_by=args.actor,
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
    parser.add_argument("--adjustment-id", type=_uuid, required=True)
    parser.add_argument("--disposition", type=_disposition, required=True)
    parser.add_argument(
        "--entitlement-id",
        type=_uuid,
        default=None,
        help="the entitlement that proves the period (linked, or Finance-named)",
    )
    parser.add_argument("--subscription-id", type=_uuid, default=None)
    parser.add_argument("--period-start", type=_timestamp, default=None)
    parser.add_argument("--period-end", type=_timestamp, default=None)
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

    confirm = sub.add_parser("confirm", help="apply the previewed correction")
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
