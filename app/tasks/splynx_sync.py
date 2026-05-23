"""Celery task for Splynx incremental sync during dual-run period."""

from __future__ import annotations

import logging
import sys

# scripts/ is a top-level dir alongside app/ but Celery forked workers may
# not have /app on sys.path. Insert it so "from scripts.migration.X" works.
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.splynx_sync.run_incremental_sync", soft_time_limit=600, time_limit=900)
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

    from app.services.migrations.db_connections import (
        dotmac_session,
        splynx_connection,
    )
    from app.services.migrations.incremental_sync import (
        sync_deleted_customers,
        sync_deleted_services,
        sync_new_credit_notes,
        sync_new_invoices,
        sync_new_payments,
        sync_status_changes,
    )

    since = datetime.now(UTC) - timedelta(hours=hours_back)
    logger.info("Splynx incremental sync starting (since %s)", since.isoformat())

    stats: dict[str, int] = {
        "invoices_created": 0,
        "payments_created": 0,
        "credit_notes_created": 0,
        "status_updated": 0,
        "customers_deleted": 0,
        "services_canceled": 0,
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

                cn_result = sync_new_credit_notes(conn, db, since)
                db.commit()
                stats["credit_notes_created"] = cn_result.get("created", 0)

                status_result = sync_status_changes(conn, db)
                db.commit()
                stats["status_updated"] = status_result.get("updated", 0)

                del_cust_result = sync_deleted_customers(conn, db)
                db.commit()
                stats["customers_deleted"] = del_cust_result.get("soft_deleted", 0)

                del_svc_result = sync_deleted_services(conn, db)
                db.commit()
                stats["services_canceled"] = del_svc_result.get("canceled", 0)

    except Exception as exc:
        logger.error("Splynx incremental sync failed: %s", exc)
        stats["errors"] = 1
        raise

    total = (
        stats["invoices_created"]
        + stats["payments_created"]
        + stats["credit_notes_created"]
        + stats["status_updated"]
        + stats["customers_deleted"]
        + stats["services_canceled"]
    )
    logger.info(
        "Splynx incremental sync complete: %d invoices, %d payments, %d credit notes, "
        "%d status changes, %d deleted customers, %d canceled services",
        stats["invoices_created"],
        stats["payments_created"],
        stats["credit_notes_created"],
        stats["status_updated"],
        stats["customers_deleted"],
        stats["services_canceled"],
    )
    if total > 0:
        logger.info("Sync stats: %s", stats)

    return stats


@celery_app.task(name="app.tasks.splynx_sync.run_customer_accounts_details_sync")
def run_customer_accounts_details_sync() -> dict[str, dict[str, int]]:
    """Sync only Splynx customer accounts and detail fields into DotMac Sub."""
    from app.services.splynx_customer_sync import run_customer_sync

    logger.info("Splynx customer accounts/details sync starting")
    result = run_customer_sync(dry_run=False)
    logger.info("Splynx customer accounts/details sync complete: %s", result)
    return result


@celery_app.task(name="app.tasks.splynx_sync.run_subscriber_status_sync")
def run_subscriber_status_sync() -> dict[str, int]:
    """Sync Splynx customers.status → Subscriber.status (with deleted=1 → canceled).

    Splynx's customer-level block triggers walled-garden RADIUS. This MUST run
    regularly during dual-run so dotmac_sub mirrors Splynx blocks.
    """
    import sys
    if "/app" not in sys.path: sys.path.insert(0, "/app")
    from scripts.migration.sync_subscriber_status_from_splynx import run

    logger.info("Splynx subscriber-status sync starting")
    result = run(dry_run=False)
    logger.info("Splynx subscriber-status sync complete: %s", result)
    return result


@celery_app.task(name="app.tasks.splynx_sync.run_subscription_status_sync")
def run_subscription_status_sync() -> dict[str, int]:
    """Sync Splynx services_internet (status + ipv4) → Subscription.

    Picks up service-level status changes and dynamic IP reassignments
    (Splynx changes ipv4 when walled-gardening / unblocking customers).
    """
    from app.services.migrations.sync_subscription_status_from_splynx import run

    logger.info("Splynx subscription status+IP sync starting")
    result = run(dry_run=False)
    logger.info("Splynx subscription status+IP sync complete: %s", result)
    return result


@celery_app.task(name="app.tasks.splynx_sync.run_refresh_radius_from_subs")
def run_refresh_radius_from_subs() -> dict[str, int]:
    """Rebuild radcheck + radreply from dotmac_sub authoritative joins.

    No Splynx API. Picks up any status/IP/offer/profile changes flowing into
    Subscription via the other syncs, plus customer-level blocks via
    Subscriber.status. Idempotent; per-user DELETE+INSERT.
    """
    import sys
    if "/app" not in sys.path: sys.path.insert(0, "/app")
    from scripts.migration.populate_radius_from_subs import populate

    logger.info("RADIUS refresh-from-subs starting")
    result = populate(dry_run=False)
    logger.info("RADIUS refresh-from-subs complete: %s", result)
    return result
