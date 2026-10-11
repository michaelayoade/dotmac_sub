"""Preview or apply one reviewed prepaid lock lost to subscription expiry.

Run a preview first. Apply requires its exact fingerprint and as-of instant.
This tool changes no payment, entitlement, billing anchor, or terminal service.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from uuid import UUID

from app.db import SessionLocal
from app.services.owner_commands import CommandContext
from app.services.prepaid_expiry_lock_repair import (
    PrepaidExpiryLockRepairCommand,
    PrepaidExpiryLockRepairQuery,
    apply_prepaid_expiry_lock_repair,
    preview_prepaid_expiry_lock_repair,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription-id", required=True, type=UUID)
    parser.add_argument("--historical-lock-id", required=True, type=UUID)
    parser.add_argument("--as-of", required=True, type=datetime.fromisoformat)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    args = parser.parse_args()

    query = PrepaidExpiryLockRepairQuery(
        subscription_id=args.subscription_id,
        historical_lock_id=args.historical_lock_id,
        as_of=args.as_of,
    )
    if not args.apply:
        with SessionLocal() as db:
            preview = preview_prepaid_expiry_lock_repair(db, query)
        print(
            json.dumps(
                {
                    "subscription_id": str(query.subscription_id),
                    "historical_lock_id": str(query.historical_lock_id),
                    "as_of": query.as_of.isoformat(),
                    "fingerprint": preview.fingerprint,
                    "repairable": preview.repairable,
                    "blockers": [blocker.value for blocker in preview.blockers],
                    "active_lock_ids": [
                        str(value) for value in preview.active_lock_ids
                    ],
                    "coverage_status": (
                        preview.coverage_status.value
                        if preview.coverage_status
                        else None
                    ),
                    "access_mode": (
                        preview.access_mode.value if preview.access_mode else None
                    ),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if not all((args.fingerprint, args.idempotency_key, args.actor, args.reason)):
        parser.error(
            "--apply requires --fingerprint, --idempotency-key, --actor, and --reason"
        )
    context = CommandContext.system(
        actor=args.actor,
        scope=f"subscription:{query.subscription_id}",
        reason=args.reason,
        idempotency_key=args.idempotency_key,
    )
    with SessionLocal() as db:
        outcome = apply_prepaid_expiry_lock_repair(
            db,
            PrepaidExpiryLockRepairCommand(
                query=query,
                expected_fingerprint=args.fingerprint,
                context=context,
            ),
        )
    print(
        json.dumps(
            {
                "subscription_id": str(outcome.subscription_id),
                "active_lock_id": str(outcome.active_lock_id),
                "created": outcome.created,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
