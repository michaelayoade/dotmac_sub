"""Celery task for Splynx incremental sync during dual-run period."""

from __future__ import annotations

import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.splynx_sync.run_incremental_sync")
def run_incremental_sync(hours_back: int = 2) -> dict[str, int]:
    """Pull recent changes from Splynx into DotMac Sub.

    Syncs new invoices, payments, and status changes created within
    the last ``hours_back`` hours.  Designed to run every 15-30 min
    via Celery beat during the Splynx dual-run period.

    Args:
        hours_back: How many hours of history to look back.

    Returns:
        Statistics dict with counts of synced records.
    """
    from datetime import UTC, datetime, timedelta

    from scripts.migration.db_connections import (
        dotmac_session,
        splynx_connection,
    )
    from scripts.migration.incremental_sync import (
        sync_new_invoices,
        sync_new_payments,
        sync_status_changes,
    )

    since = datetime.now(UTC) - timedelta(hours=hours_back)
    logger.info("Splynx incremental sync starting (since %s)", since.isoformat())

    stats: dict[str, int] = {
        "invoices_created": 0,
        "payments_created": 0,
        "status_updated": 0,
        "errors": 0,
    }

    try:
        with splynx_connection() as conn:
            with dotmac_session() as db:
                inv_result = sync_new_invoices(conn, db, since)
                db.commit()
                stats["invoices_created"] = inv_result.get("created", 0)

                pay_result = sync_new_payments(conn, db, since)
                db.commit()
                stats["payments_created"] = pay_result.get("created", 0)

                status_result = sync_status_changes(conn, db)
                db.commit()
                stats["status_updated"] = status_result.get("updated", 0)

    except Exception as exc:
        logger.error("Splynx incremental sync failed: %s", exc)
        stats["errors"] = 1
        raise

    total = stats["invoices_created"] + stats["payments_created"] + stats["status_updated"]
    logger.info(
        "Splynx incremental sync complete: %d invoices, %d payments, %d status changes",
        stats["invoices_created"],
        stats["payments_created"],
        stats["status_updated"],
    )
    if total > 0:
        logger.info("Sync stats: %s", stats)

    return stats
