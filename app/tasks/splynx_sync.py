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


@celery_app.task(
    name="app.tasks.splynx_sync.run_incremental_sync",
    soft_time_limit=600,
    time_limit=900,
)
def run_incremental_sync(hours_back: int | None = None) -> dict[str, int]:
    """Pull recent changes from Splynx into DotMac Sub.

    Syncs new invoices, payments, payment allocations, credit notes, and status
    changes. Financial rows use durable Splynx ID cursors with retryable skips;
    ``hours_back`` is accepted for backward-compatible manual invocations only.

    Args:
        hours_back: How many hours of history to look back.

    Returns:
        Statistics dict with counts of synced records.
    """
    from app.services.migrations.db_connections import (
        dotmac_session,
        splynx_connection,
    )

    # Post-cutover (Phase 5, 2026-06-16): sub is the SOLE writer of subscription
    # lifecycle. Splynx must NOT push status changes / cancellations / invoices
    # into sub (that's the split-brain). The only Splynx→sub bridge still needed
    # is PAYMENTS — .ng customers still pay on the Splynx portal until
    # selfcare.dotmac.ng is re-pointed to dotmac; this rescues those payments
    # into sub. Disable this whole task once .ng is re-pointed.
    from app.services.migrations.incremental_sync import (
        sync_billing_transactions,
        sync_new_payments,
        sync_payment_allocations,
    )

    logger.info("Splynx incremental sync starting (cursor mode)")

    stats: dict[str, int] = {
        "invoices_created": 0,
        "payments_created": 0,
        "payment_allocations_created": 0,
        "credit_notes_created": 0,
        "billing_transactions_mirrored": 0,
        "invoice_skips": 0,
        "payment_skips": 0,
        "payment_allocation_skips": 0,
        "status_updated": 0,
        "customers_deleted": 0,
        "services_canceled": 0,
        "errors": 0,
    }

    try:
        with splynx_connection() as conn:
            with dotmac_session() as db:
                # PAYMENTS ONLY (the .ng rescue). Invoice/credit-note/status/
                # cancellation pulls are deliberately removed post-cutover — sub
                # is the sole lifecycle writer; pulling those from Splynx would be
                # split-brain. (Stats keys for the removed syncs stay 0.)
                pay_result = sync_new_payments(conn, db)
                db.commit()
                stats["payments_created"] = pay_result.get("created", 0)
                stats["payment_skips"] = pay_result.get("skipped", 0)

                alloc_result = sync_payment_allocations(conn, db)
                db.commit()
                stats["payment_allocations_created"] = alloc_result.get("created", 0)
                stats["payment_allocation_skips"] = alloc_result.get("skipped", 0)

                # Read-only mirror of Splynx's raw ledger (no lifecycle write).
                bt_result = sync_billing_transactions(conn, db)
                db.commit()
                stats["billing_transactions_mirrored"] = bt_result.get("created", 0)

    except Exception as exc:
        logger.error("Splynx incremental sync failed: %s", exc)
        stats["errors"] = 1
        raise

    total = (
        stats["invoices_created"]
        + stats["payments_created"]
        + stats["payment_allocations_created"]
        + stats["credit_notes_created"]
        + stats["billing_transactions_mirrored"]
        + stats["status_updated"]
        + stats["customers_deleted"]
        + stats["services_canceled"]
    )
    logger.info(
        "Splynx incremental sync complete: %d invoices, %d payments, %d payment "
        "allocations, %d credit notes, %d ledger txns, %d status changes, "
        "%d deleted customers, %d canceled services",
        stats["invoices_created"],
        stats["payments_created"],
        stats["payment_allocations_created"],
        stats["credit_notes_created"],
        stats["billing_transactions_mirrored"],
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

    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
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

    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from scripts.migration.populate_radius_from_subs import populate

    logger.info("RADIUS refresh-from-subs starting")
    result = populate(dry_run=False)
    logger.info("RADIUS refresh-from-subs complete: %s", result)
    return result


@celery_app.task(
    name="app.tasks.splynx_sync.run_new_subscriptions_sync",
    soft_time_limit=300,
    time_limit=600,
)
def run_new_subscriptions_sync() -> dict[str, int]:
    """Discover new Splynx services and create matching dotmac Subscriptions.

    Fills the gap where existing subscription_status_sync only UPDATES known
    subs but doesn't CREATE new ones for services added in Splynx since
    migration.
    """
    from app.services.migrations.sync_new_subscriptions_from_splynx import run

    logger.info("Splynx new-subscriptions discovery starting")
    result = run(dry_run=False)
    logger.info("Splynx new-subscriptions discovery complete: %s", result)
    return result


@celery_app.task(name="app.tasks.splynx_sync.run_password_freshness_sync")
def run_password_freshness_sync() -> None:
    """Repair radcheck/access_credentials for Splynx services changed in the
    last 26h (passwords don't flow through the other syncs). Dual-run only;
    retire with the Splynx decommission."""
    from scripts.migration.refresh_changed_passwords import main

    logger.info("password freshness sync starting")
    main(execute=True)
