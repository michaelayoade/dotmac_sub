#!/usr/bin/env python
"""Preview or apply exact-evidence prepaid coverage reconciliation.

Preview is the default and performs no writes. Apply requires the exact
preview ``--as-of`` and ``--fingerprint`` plus an idempotency key, actor, and
review reason. A full-cohort preview is the canonical operational evidence;
``--subscription-id`` is for bounded investigation and staged repair.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from uuid import UUID

from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.prepaid_coverage_reconciliation import (
    ReconcilePrepaidCoverageCommand,
    preview_prepaid_coverage_reconciliation,
    reconcile_prepaid_service_coverage,
)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone offset")
    return parsed


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("identifier must be a UUID") from exc


def _preview_payload(preview) -> dict[str, object]:
    return {
        "as_of": preview.as_of.isoformat(),
        "fingerprint": preview.fingerprint,
        "subscription_count": len(preview.subscription_ids),
        "repairable_count": preview.repairable_count,
        "quarantined_count": preview.quarantined_count,
        "blocker_count": preview.blocker_count,
        "items": [
            {
                "subscription_id": str(item.subscription_id),
                "account_id": str(item.account_id),
                "decision": item.decision.value,
                "reason": item.reason.value,
                "source": item.source.value,
                "source_id": str(item.source_id) if item.source_id else None,
                "starts_at": item.starts_at.isoformat() if item.starts_at else None,
                "ends_at": item.ends_at.isoformat() if item.ends_at else None,
                "amount": str(item.amount) if item.amount is not None else None,
                "currency": item.currency,
                "evidence_fingerprint": item.evidence_fingerprint,
            }
            for item in preview.items
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=_timestamp)
    parser.add_argument("--subscription-id", action="append", type=_uuid, default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    args = parser.parse_args()
    selected = tuple(args.subscription_id) or None

    if args.apply:
        missing = [
            name
            for name, value in (
                ("--as-of", args.as_of),
                ("--fingerprint", args.fingerprint),
                ("--idempotency-key", args.idempotency_key),
                ("--actor", args.actor),
                ("--reason", args.reason),
            )
            if not value
        ]
        if missing:
            parser.error("--apply requires " + ", ".join(missing))
        with db_session_adapter.owner_command_session() as db:
            context = CommandContext.system(
                actor=args.actor,
                scope="prepaid_service_coverage",
                reason=args.reason,
                idempotency_key=args.idempotency_key,
            )
            result = reconcile_prepaid_service_coverage(
                db,
                ReconcilePrepaidCoverageCommand(
                    context=context,
                    as_of=args.as_of,
                    preview_fingerprint=args.fingerprint,
                    subscription_ids=selected,
                ),
            )
        print(
            json.dumps(
                {
                    "run_id": str(result.run_id),
                    "preview_fingerprint": result.preview_fingerprint,
                    "entitlement_created_count": result.entitlement_created_count,
                    "already_covered_count": result.already_covered_count,
                    "no_repair_required_count": result.no_repair_required_count,
                    "quarantined_count": result.quarantined_count,
                    "replayed": result.replayed,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    with db_session_adapter.read_session() as db:
        preview = preview_prepaid_coverage_reconciliation(
            db,
            as_of=args.as_of,
            subscription_ids=selected,
        )
        payload = _preview_payload(preview)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 2 if preview.blocker_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
