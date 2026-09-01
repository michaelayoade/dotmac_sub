#!/usr/bin/env python3
"""Preview or safely retry failed Facebook and Instagram replies."""

from __future__ import annotations

import argparse
import json

from app.db import SessionLocal, finish_read_transaction
from app.services import team_inbox_maintenance
from app.services.owner_commands import CommandContext


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-digest", default="")
    parser.add_argument("--max-retry-count", type=int, default=5)
    args = parser.parse_args()
    with SessionLocal() as db:
        preview = team_inbox_maintenance.preview_failed_meta_deliveries(
            db, limit=args.limit
        )
        result: dict[str, object] = {
            "candidate_count": len(preview.candidates),
            "digest": preview.digest,
            "applied": 0,
            "skipped": 0,
        }
        if not args.apply:
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if not args.expected_digest or args.expected_digest != preview.digest:
            result["error"] = "Preview digest is missing or changed; run preview again."
            print(json.dumps(result, indent=2, sort_keys=True))
            return 2
        finish_read_transaction(db)
        outcome = team_inbox_maintenance.retry_failed_meta_deliveries(
            db,
            team_inbox_maintenance.RetryFailedMetaDeliveriesCommand(
                context=CommandContext.system(
                    actor="operator.meta_delivery_retry",
                    scope="communications:team-inbox-maintenance",
                    reason="Retry previewed failed Meta replies",
                    idempotency_key=f"meta-delivery-retry:{preview.digest}",
                ),
                message_ids=tuple(row.message_id for row in preview.candidates),
                max_retry_count=max(1, args.max_retry_count),
            ),
        )
        result["applied"] = outcome.changed
        result["skipped"] = outcome.skipped
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
