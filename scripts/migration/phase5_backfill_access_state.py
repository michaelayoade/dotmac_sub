"""Phase 5 backfill — populate subscription.access_state + radusergroup
for every subscription.

Reads each subscription's status + subscriber.captive_redirect_enabled,
computes the AccessState via derive_access_state, and writes via
set_subscription_access_state (which handles both the app DB column and
the external RADIUS radusergroup mirror).

Resumable: filters `WHERE access_state IS NULL` by default, so a re-run
picks up only what's left.

Idempotent: each call to set_subscription_access_state is idempotent.
Re-running with --include-migrated does no harm.

Bounded: commits per subscription so a mid-run failure leaves a
deterministic checkpoint. --limit N caps how many rows to process per
invocation; useful for canary runs.

Usage:

    # Print what would happen to the first 10 subs:
    PYTHONPATH=/app python scripts/migration/phase5_backfill_access_state.py --dry-run --limit 10

    # Canary: backfill 10 real subs:
    PYTHONPATH=/app python scripts/migration/phase5_backfill_access_state.py --limit 10

    # Full backfill:
    PYTHONPATH=/app python scripts/migration/phase5_backfill_access_state.py

    # Force re-process all rows (even if already migrated):
    PYTHONPATH=/app python scripts/migration/phase5_backfill_access_state.py --include-migrated
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter

from sqlalchemy import select

from app.db import SessionLocal
from app.models.catalog import Subscription
from app.models.subscriber import Subscriber
from app.services import radius_access_state as ras
from app.services.radius_access_state import (
    derive_access_state,
    set_subscription_access_state,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("phase5_backfill")


def _iter_subscriptions(db, batch_size: int, include_migrated: bool):
    """Yield (subscription, captive_flag) one row at a time.

    Walks the table in pages of ``batch_size``. When ``include_migrated``
    is False (default), the WHERE filter naturally shrinks the query
    set as rows are populated, so paging via repeated LIMIT (no OFFSET)
    is correct. When ``include_migrated`` is True, we order by id and
    track the last-seen id to advance.
    """
    last_id = None
    while True:
        stmt = (
            select(Subscription, Subscriber.captive_redirect_enabled)
            .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
            .order_by(Subscription.id)
            .limit(batch_size)
        )
        if not include_migrated:
            stmt = stmt.where(Subscription.access_state.is_(None))
        if last_id is not None:
            stmt = stmt.where(Subscription.id > last_id)
        rows = list(db.execute(stmt).all())
        if not rows:
            return
        for sub, captive in rows:
            yield sub, bool(captive)
            last_id = sub.id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print intended writes without calling set_subscription_access_state.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum subscriptions to process this run (canary mode).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="DB page size for the select cursor.",
    )
    parser.add_argument(
        "--commit-every",
        type=int,
        default=50,
        help="Commit the app-DB transaction every N successful rows. "
        "Larger = faster (per-row commit forces an fsync). On a batch "
        "failure we rollback the whole batch and resume; the helper is "
        "idempotent so redoing the lost rows is safe. Set to 1 for the "
        "old per-row commit behavior.",
    )
    parser.add_argument(
        "--sleep-between-batches",
        type=float,
        default=0.0,
        help="Seconds to pause between commit batches (for backpressure).",
    )
    parser.add_argument(
        "--log-every",
        type=int,
        default=100,
        help="Log a progress checkpoint every N processed rows.",
    )
    parser.add_argument(
        "--include-migrated",
        action="store_true",
        help="Re-process subscriptions whose access_state is already set. "
        "Idempotent, but useful only for a full re-sync (e.g., the "
        "external radusergroup table was wiped).",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        # Pre-compute external sync configs once and monkey-patch the
        # lookup so set_subscription_access_state doesn't repeat the N+1
        # connector_config query on every row. The configs are stable
        # for the duration of this run.
        cached_configs = ras._active_external_sync_configs(db)
        ras._active_external_sync_configs = lambda _db: cached_configs
        logger.info(
            "cached external sync configs at startup: %d target(s)",
            len(cached_configs),
        )

        counts: Counter[str] = Counter()
        errors: list[tuple[str, str]] = []
        n_done = 0
        n_written = 0
        page_n = 0
        seen_in_page = 0

        logger.info(
            "phase5 backfill starting — dry_run=%s limit=%s batch_size=%d include_migrated=%s",
            args.dry_run,
            args.limit,
            args.batch_size,
            args.include_migrated,
        )

        for sub, captive in _iter_subscriptions(
            db, args.batch_size, args.include_migrated
        ):
            seen_in_page += 1
            if seen_in_page == 1:
                page_n += 1

            state = derive_access_state(
                sub.status, captive_redirect_enabled=captive
            )
            state_label = state.value if state else "none"
            counts[state_label] += 1

            if not args.dry_run:
                try:
                    result = set_subscription_access_state(
                        db, str(sub.id), state
                    )
                    n_written += result.get("external_rows_written", 0)
                except Exception as exc:
                    logger.warning(
                        "failed sub=%s status=%s: %s",
                        sub.id,
                        sub.status.value,
                        exc,
                    )
                    errors.append((str(sub.id), str(exc)))
                    # Rollback the in-flight batch — idempotency on
                    # resume catches any partial work lost here.
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    continue

            n_done += 1

            # Commit boundary
            if not args.dry_run and n_done % args.commit_every == 0:
                try:
                    db.commit()
                except Exception as exc:
                    logger.warning(
                        "batch commit failed at n_done=%d: %s", n_done, exc
                    )
                    db.rollback()
                if args.sleep_between_batches > 0:
                    time.sleep(args.sleep_between_batches)

            if n_done % args.log_every == 0:
                logger.info(
                    "checkpoint — processed=%d errors=%d external_rows_written=%d",
                    n_done,
                    len(errors),
                    n_written,
                )

            if args.limit and n_done >= args.limit:
                logger.info("--limit reached, stopping")
                break

            if seen_in_page >= args.batch_size:
                seen_in_page = 0

        # Final commit for any partial last batch
        if not args.dry_run:
            try:
                db.commit()
            except Exception as exc:
                logger.warning("final commit failed: %s", exc)
                db.rollback()

        logger.info("=== Summary ===")
        logger.info("Total processed: %d", n_done)
        for state_label in sorted(counts.keys()):
            logger.info("  %-12s : %d", state_label, counts[state_label])
        logger.info("External radusergroup rows written: %d", n_written)
        logger.info("Errors: %d", len(errors))
        for sub_id, err in errors[:10]:
            logger.info("  sub=%s err=%s", sub_id, err)
        if len(errors) > 10:
            logger.info("  ... and %d more", len(errors) - 10)
    finally:
        db.close()

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
