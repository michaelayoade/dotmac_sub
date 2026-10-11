"""Read-only discovery of expiry-cleared prepaid locks needing review."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from app.db import SessionLocal
from app.services.prepaid_expiry_lock_repair import (
    PrepaidExpiryLockDiscoveryQuery,
    discover_prepaid_expiry_lock_repairs,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolved-since", required=True, type=datetime.fromisoformat)
    parser.add_argument("--resolved-before", required=True, type=datetime.fromisoformat)
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    query = PrepaidExpiryLockDiscoveryQuery(
        resolved_since=args.resolved_since,
        resolved_before=args.resolved_before,
        as_of=args.as_of or datetime.now(UTC),
        limit=args.limit,
    )
    with SessionLocal() as db:
        previews = discover_prepaid_expiry_lock_repairs(db, query)
    print(
        json.dumps(
            {
                "as_of": query.as_of.isoformat(),
                "count": len(previews),
                "limit": query.limit,
                "candidates": [
                    {
                        "subscription_id": str(item.query.subscription_id),
                        "historical_lock_id": str(item.query.historical_lock_id),
                        "fingerprint": item.fingerprint,
                        "repairable": item.repairable,
                        "blockers": [blocker.value for blocker in item.blockers],
                    }
                    for item in previews
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
