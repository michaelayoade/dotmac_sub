#!/usr/bin/env python
"""Preview or apply one finance-reviewed Paystack reference recovery.

Preview is the default and is read-only. Apply is deliberately singular and
requires the exact preview fingerprint plus explicit finance review evidence.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.payment_reconciliation import (
    PAYSTACK_OUTSIDE_WINDOW_RECOVERY_SCOPE,
    ConfirmPaystackOutsideWindowRecoveryCommand,
    PaystackOutsideWindowRecoveryPreview,
    PaystackOutsideWindowRecoveryResult,
    PreviewPaystackOutsideWindowRecoveryQuery,
    confirm_paystack_outside_window_recovery,
    preview_paystack_outside_window_recovery,
)

_APPLY_CONFIRMATION = "APPLY_REVIEWED_PAYSTACK_RECOVERY"


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("identifier must be a UUID") from exc


def _enum_value(value: object | None) -> object | None:
    return value.value if isinstance(value, Enum) else value


def _uuid_text(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _preview_payload(
    preview: PaystackOutsideWindowRecoveryPreview,
) -> dict[str, object]:
    """Serialize only the public typed preview contract."""

    return {
        "intent_id": str(preview.intent_id),
        "reference": preview.reference,
        "disposition": _enum_value(preview.disposition),
        "actionable": preview.actionable,
        "fingerprint": preview.fingerprint,
        "intent_status": _enum_value(preview.intent_status),
        "intent_created_at": preview.intent_created_at.isoformat(),
        "provider_id": _uuid_text(preview.provider_id),
        "checkout_binding_id": _uuid_text(preview.checkout_binding_id),
        "provider_external_id": preview.provider_external_id,
        "gross_amount": _decimal_text(preview.gross_amount),
        "provider_fee": _decimal_text(preview.provider_fee),
        "authorized_net_amount": _decimal_text(preview.authorized_net_amount),
        "currency": preview.currency,
        "provider_status": _enum_value(preview.provider_status),
        "reason_code": _enum_value(preview.reason_code),
        "existing_payment_id": _uuid_text(preview.existing_payment_id),
    }


def _result_payload(result: PaystackOutsideWindowRecoveryResult) -> dict[str, object]:
    """Serialize only the public typed confirmation result contract."""

    return {
        "recovery_run_id": str(result.recovery_run_id),
        "intent_id": str(result.intent_id),
        "disposition": _enum_value(result.disposition),
        "payment_id": _uuid_text(result.payment_id),
        "preview_fingerprint": result.preview_fingerprint,
        "replayed": result.replayed,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intent-id", required=True, type=_uuid)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    parser.add_argument("--review-reference")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--confirm")
    return parser


def _missing_apply_arguments(args: argparse.Namespace) -> list[str]:
    return [
        name
        for name, value in (
            ("--fingerprint", args.fingerprint),
            ("--actor", args.actor),
            ("--reason", args.reason),
            ("--review-reference", args.review_reference),
            ("--idempotency-key", args.idempotency_key),
        )
        if value is None or not value.strip()
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if args.apply:
        missing = _missing_apply_arguments(args)
        if missing:
            parser.error("--apply requires " + ", ".join(missing))
        if args.confirm != _APPLY_CONFIRMATION:
            parser.error(f"--apply requires --confirm {_APPLY_CONFIRMATION}")

        with db_session_adapter.owner_command_session() as db:
            result = confirm_paystack_outside_window_recovery(
                db,
                ConfirmPaystackOutsideWindowRecoveryCommand(
                    intent_id=args.intent_id,
                    reference=args.reference,
                    preview_fingerprint=args.fingerprint,
                    review_reference=args.review_reference,
                    confirmed=True,
                    context=CommandContext.system(
                        actor=args.actor,
                        scope=PAYSTACK_OUTSIDE_WINDOW_RECOVERY_SCOPE,
                        reason=args.reason,
                        idempotency_key=args.idempotency_key,
                    ),
                ),
            )
        print(json.dumps(_result_payload(result), indent=2, sort_keys=True))
        return 0

    with db_session_adapter.read_session() as db:
        preview = preview_paystack_outside_window_recovery(
            db,
            PreviewPaystackOutsideWindowRecoveryQuery(
                intent_id=args.intent_id,
                reference=args.reference,
                observed_at=datetime.now(UTC),
            ),
        )
    print(json.dumps(_preview_payload(preview), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
