"""Dry-run or apply one evidence-backed omitted Sub-native prepaid opening."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from uuid import UUID

from app.services.billing.subledger_opening import (
    NATIVE_REPAIR_SCOPE,
    NativePrepaidOpeningApproval,
    PreviewNativePrepaidOpeningRepairQuery,
    RepairNativePrepaidOpeningCommand,
    preview_native_prepaid_opening_repair,
    repair_native_prepaid_opening,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone offset")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=UUID, required=True)
    parser.add_argument("--currency", default="NGN")
    parser.add_argument("--finance-approver-system-user-id", type=UUID, required=True)
    parser.add_argument("--finance-approver-name", required=True)
    parser.add_argument("--approved-at", type=_timestamp, required=True)
    parser.add_argument("--ticket-reference", required=True)
    parser.add_argument("--evidence-ref", required=True)
    parser.add_argument("--evidence-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint")
    parser.add_argument("--operator-system-user-id", type=UUID)
    parser.add_argument("--reason")
    parser.add_argument("--idempotency-key")
    return parser


def _query(args: argparse.Namespace) -> PreviewNativePrepaidOpeningRepairQuery:
    return PreviewNativePrepaidOpeningRepairQuery(
        account_id=args.account_id,
        currency=args.currency,
        approval=NativePrepaidOpeningApproval(
            finance_approver_system_user_id=args.finance_approver_system_user_id,
            finance_approver_name=args.finance_approver_name,
            approved_at=args.approved_at,
            ticket_reference=args.ticket_reference,
            evidence_ref=args.evidence_ref,
            evidence_sha256=args.evidence_sha256,
        ),
    )


def _preview(value) -> dict[str, object]:  # noqa: ANN001
    return {
        "account_id": str(value.account_id),
        "currency": value.currency,
        "account_created_at": value.account_created_at.isoformat(),
        "legacy_handoff_at": value.legacy_handoff_at.isoformat(),
        "original_cutover_batch_id": str(value.original_cutover_batch_id),
        "original_cutover_at": value.original_cutover_at.isoformat(),
        "cutover_evidence_fingerprint": value.cutover_evidence_fingerprint,
        "source_classification": value.source_classification,
        "splynx_transaction_count": value.splynx_transaction_count,
        "calculated_amount": str(value.calculated_amount),
        "native_event_count": value.native_event_count,
        "shadow_position_before": str(value.shadow_position_before),
        "opening_delta": str(value.opening_delta),
        "source_identity_fingerprint": value.source_identity_fingerprint,
        "native_evidence_fingerprint": value.native_evidence_fingerprint,
        "shadow_evidence_fingerprint": value.shadow_evidence_fingerprint,
        "fingerprint": value.fingerprint,
    }


def main() -> int:
    args = _parser().parse_args()
    query = _query(args)
    try:
        with db_session_adapter.read_session() as db:
            preview = preview_native_prepaid_opening_repair(db, query)
        if not args.apply:
            print(
                json.dumps(
                    {"applied": False, "preview": _preview(preview)}, sort_keys=True
                )
            )
            return 0
        if not all(
            (
                args.fingerprint,
                args.operator_system_user_id,
                args.reason,
                args.idempotency_key,
            )
        ):
            print(
                json.dumps(
                    {
                        "applied": False,
                        "error": (
                            "--apply requires the exact fingerprint, operator "
                            "system-user ID, reason, and idempotency key"
                        ),
                        "preview": _preview(preview),
                    },
                    sort_keys=True,
                )
            )
            return 2
        with db_session_adapter.owner_command_session() as db:
            result = repair_native_prepaid_opening(
                db,
                RepairNativePrepaidOpeningCommand(
                    context=CommandContext.system(
                        actor=f"system_user:{args.operator_system_user_id}",
                        scope=NATIVE_REPAIR_SCOPE,
                        reason=args.reason,
                        idempotency_key=args.idempotency_key,
                    ),
                    query=query,
                    expected_preview_fingerprint=args.fingerprint,
                    operator_system_user_id=args.operator_system_user_id,
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
                "repair_id": str(result.repair_id),
                "opening_position_id": str(result.opening_position_id),
                "posting_group_id": str(result.posting_group_id),
                "account_id": str(result.account_id),
                "currency": result.currency,
                "calculated_amount": str(result.calculated_amount),
                "original_cutover_batch_id": str(result.original_cutover_batch_id),
                "original_cutover_at": result.original_cutover_at.isoformat(),
                "preview_fingerprint": result.preview_fingerprint,
                "replayed": result.replayed,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
