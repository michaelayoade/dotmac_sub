"""Preview or apply one reviewed ticket-pause prepaid reconciliation.

This is a thin operator adapter. The support pause coordinator owns the
fingerprint-bound transaction; this script never edits billing or lifecycle
rows directly.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from uuid import UUID

from app.db import SessionLocal
from app.services.owner_commands import CommandContext
from app.services.ticket_sla_service_automation import (
    ReconcileTicketPausedPrepaidServiceCommand,
    TicketServicePauseResumePreviewQuery,
    preview_ticket_paused_prepaid_reconciliation,
    reconcile_ticket_paused_prepaid_service,
)


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("instant must include a timezone")
    return parsed.astimezone(UTC)


def _emit(value: object) -> None:
    print(json.dumps(value, indent=2, default=str))


def _preview(db, args):
    preview = preview_ticket_paused_prepaid_reconciliation(
        db,
        TicketServicePauseResumePreviewQuery(
            subscription_id=UUID(args.subscription),
            proposed_resumed_at=args.resumed_at,
        ),
    )
    _emit(
        {
            "subscription_id": str(preview.resume_preview.subscription_id),
            "cause_id": str(preview.resume_preview.cause_id),
            "ticket_id": str(preview.resume_preview.ticket_id),
            "renewal_starts_at": preview.renewal_starts_at,
            "renewal_ends_at": preview.renewal_ends_at,
            "renewal_amount": preview.renewal_amount,
            "renewal_currency": preview.renewal_currency,
            "renewal_preview_fingerprint": preview.renewal_preview_fingerprint,
            "eligible": preview.eligible,
            "blocking_reasons": preview.blocking_reasons,
            "preview_fingerprint": preview.fingerprint,
        }
    )
    return 0


def _apply(db, args):
    preview = preview_ticket_paused_prepaid_reconciliation(
        db,
        TicketServicePauseResumePreviewQuery(
            subscription_id=UUID(args.subscription),
            proposed_resumed_at=args.resumed_at,
        ),
    )
    db.rollback()
    result = reconcile_ticket_paused_prepaid_service(
        db,
        ReconcileTicketPausedPrepaidServiceCommand(
            subscription_id=UUID(args.subscription),
            cause_id=preview.resume_preview.cause_id,
            preview_fingerprint=args.preview_fingerprint,
            resumed_at=args.resumed_at,
            actor=args.actor,
            reason=args.reason,
            evidence_ref=args.evidence_ref,
            context=CommandContext.system(
                actor=args.actor,
                scope=f"subscription:{args.subscription}",
                reason=args.reason,
                idempotency_key=args.idempotency_key,
            ),
        ),
    )
    _emit(
        {
            "ticket_id": str(result.resume.ticket_id),
            "subscription_id": str(result.resume.subscription_id),
            "resulting_status": result.resume.resulting_status,
            "access_restored": result.resume.access_restored,
            "paused_seconds": result.resume.paused_seconds,
            "resulting_next_billing_at": result.resume.resulting_next_billing_at,
            "renewal_entitlement_id": str(result.renewal_entitlement_id),
            "renewal_invoice_id": (
                str(result.renewal_invoice_id) if result.renewal_invoice_id else None
            ),
            "renewal_ledger_entry_id": (
                str(result.renewal_ledger_entry_id)
                if result.renewal_ledger_entry_id
                else None
            ),
            "renewal_amount": result.renewal_amount,
            "renewal_currency": result.renewal_currency,
        }
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preview", "apply"):
        command = sub.add_parser(name)
        command.add_argument("--subscription", required=True)
        command.add_argument("--resumed-at", type=_instant, default=datetime.now(UTC))
        if name == "preview":
            command.set_defaults(func=_preview)
        else:
            command.add_argument("--preview-fingerprint", required=True)
            command.add_argument("--actor", required=True)
            command.add_argument("--reason", required=True)
            command.add_argument("--evidence-ref", required=True)
            command.add_argument("--idempotency-key", required=True)
            command.set_defaults(func=_apply)

    args = parser.parse_args()
    with SessionLocal() as db:
        return args.func(db, args)


if __name__ == "__main__":
    raise SystemExit(main())
