"""Incremental sync: Pull recent changes from Splynx into DotMac Sub.

Designed to run periodically (every 15-30 min via cron or Celery beat) during
dual-run to keep DotMac Sub in sync with Splynx.

Syncs:
1. New/updated customers → Subscriber
2. New/updated services → Subscription
3. New invoices → Invoice + InvoiceLine
4. New payments → Payment + PaymentAllocation
5. Status changes (blocked/unblocked) → Subscriber/Subscription status
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from scripts.migration.db_connections import (
    dotmac_session,
    fetch_all,
    splynx_connection,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _parse_date(val) -> datetime | None:
    if not val:
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=UTC) if val.tzinfo is None else val
    try:
        from datetime import date as date_type

        if isinstance(val, date_type):
            return datetime(val.year, val.month, val.day, tzinfo=UTC)
    except (ValueError, TypeError):
        pass
    return None


def sync_new_invoices(conn, db, since: datetime) -> dict[str, int]:
    """Sync invoices created since the given timestamp."""
    from app.models.billing import Invoice, InvoiceLine, InvoiceStatus, TaxApplication
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

    customer_map = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.customer
            )
        ).all()
    }
    existing_invoice_ids = set(
        db.scalars(
            select(SplynxIdMapping.splynx_id).where(
                SplynxIdMapping.entity_type == SplynxEntityType.invoice
            )
        ).all()
    )

    since_str = since.strftime("%Y-%m-%d %H:%M:%S")
    query = f"""
        SELECT * FROM invoices
        WHERE real_create_datetime >= '{since_str}'
        ORDER BY id
    """
    rows = fetch_all(conn, query)
    created = 0
    skipped = 0

    for row in rows:
        inv_id = row["id"]
        if inv_id in existing_invoice_ids:
            skipped += 1
            continue

        subscriber_id = customer_map.get(row.get("customer_id"))
        if not subscriber_id:
            skipped += 1
            continue

        is_deleted = row.get("deleted") == "1"
        status_raw = row.get("status", "not_paid")
        status_map = {
            "not_paid": InvoiceStatus.issued,
            "paid": InvoiceStatus.paid,
            "deleted": InvoiceStatus.void,
            "pending": InvoiceStatus.draft,
        }
        status = status_map.get(status_raw, InvoiceStatus.issued)
        if is_deleted:
            status = InvoiceStatus.void

        total = Decimal(str(row.get("total") or "0"))
        due = Decimal(str(row.get("due") or "0"))

        invoice = Invoice(
            account_id=subscriber_id,
            invoice_number=(row.get("number") or "")[:80] or None,
            status=status,
            currency="NGN",
            total=total,
            balance_due=due,
            issued_at=_parse_date(row.get("date_created")),
            due_at=_parse_date(row.get("date_payment")),
            paid_at=_parse_date(row.get("date_updated")) if status == InvoiceStatus.paid else None,
            is_sent=row.get("is_sent") in ("1", 1, True),
            splynx_invoice_id=inv_id,
            is_active=not is_deleted,
        )
        db.add(invoice)
        db.flush()

        db.add(SplynxIdMapping(
            entity_type=SplynxEntityType.invoice,
            splynx_id=inv_id,
            dotmac_id=invoice.id,
        ))

        # Fetch and create invoice items
        items = fetch_all(
            conn,
            f"SELECT * FROM invoices_items WHERE invoice_id = {inv_id} AND deleted = '0'",
        )
        for item in items:
            price = Decimal(str(item.get("price") or "0"))
            qty = Decimal(str(item.get("quantity") or "1"))
            line = InvoiceLine(
                invoice_id=invoice.id,
                description=(item.get("description") or "Line item")[:255],
                quantity=qty,
                unit_price=price,
                amount=price * qty,
                tax_application=TaxApplication.exclusive,
                is_active=True,
            )
            db.add(line)

        created += 1

    db.flush()
    logger.info("Invoices synced: %d new, %d skipped", created, skipped)
    return {"created": created, "skipped": skipped}


def sync_new_payments(conn, db, since: datetime) -> dict[str, int]:
    """Sync payments created since the given timestamp."""
    from app.models.billing import Payment, PaymentStatus
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

    customer_map = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.customer
            )
        ).all()
    }
    existing_payment_ids = set(
        db.scalars(
            select(SplynxIdMapping.splynx_id).where(
                SplynxIdMapping.entity_type == SplynxEntityType.payment
            )
        ).all()
    )

    since_str = since.strftime("%Y-%m-%d")
    query = f"SELECT * FROM payments WHERE date >= '{since_str}' ORDER BY id"
    rows = fetch_all(conn, query)
    created = 0
    skipped = 0

    for row in rows:
        pid = row["id"]
        if pid in existing_payment_ids:
            skipped += 1
            continue

        subscriber_id = customer_map.get(row.get("customer_id"))
        if not subscriber_id:
            skipped += 1
            continue

        is_deleted = row.get("deleted") == "1"
        amount = Decimal(str(row.get("amount") or "0"))

        payment = Payment(
            account_id=subscriber_id,
            amount=amount,
            currency="NGN",
            status=PaymentStatus.succeeded if not is_deleted else PaymentStatus.canceled,
            paid_at=_parse_date(row.get("date")),
            receipt_number=(row.get("receipt_number") or "")[:120] or None,
            memo=(row.get("comment") or "")[:500] or None,
            splynx_payment_id=pid,
            is_active=not is_deleted,
        )
        db.add(payment)
        db.flush()

        db.add(SplynxIdMapping(
            entity_type=SplynxEntityType.payment,
            splynx_id=pid,
            dotmac_id=payment.id,
        ))
        created += 1

    db.flush()
    logger.info("Payments synced: %d new, %d skipped", created, skipped)
    return {"created": created, "skipped": skipped}


def sync_status_changes(conn, db) -> dict[str, int]:
    """Sync customer status changes from Splynx."""
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping
    from app.models.subscriber import Subscriber, SubscriberStatus

    status_map = {
        "active": SubscriberStatus.active,
        "blocked": SubscriberStatus.suspended,
        "disabled": SubscriberStatus.canceled,
        "new": SubscriberStatus.active,
    }

    # Get all customer mappings
    customer_map = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.customer
            )
        ).all()
    }

    # Get current Splynx statuses
    query = "SELECT id, status FROM customers WHERE deleted='0' AND category != 'lead'"
    rows = fetch_all(conn, query)
    updated = 0

    for row in rows:
        dotmac_id = customer_map.get(row["id"])
        if not dotmac_id:
            continue

        expected_status = status_map.get(row["status"])
        if not expected_status:
            continue

        subscriber = db.get(Subscriber, dotmac_id)
        if subscriber and subscriber.status != expected_status:
            subscriber.status = expected_status
            updated += 1

    db.flush()
    logger.info("Status changes synced: %d updated", updated)
    return {"updated": updated}


def run_incremental_sync(
    hours_back: int = 24,
    dry_run: bool = True,
) -> None:
    """Execute incremental sync."""
    since = datetime.now(UTC) - timedelta(hours=hours_back)
    logger.info("=== Incremental Sync (since %s) ===", since.isoformat())

    with splynx_connection() as conn:
        with dotmac_session() as db:
            if dry_run:
                since_str = since.strftime("%Y-%m-%d %H:%M:%S")
                tables = [
                    ("new invoices", f"SELECT COUNT(*) as cnt FROM invoices WHERE real_create_datetime >= '{since_str}'"),
                    ("new payments", f"SELECT COUNT(*) as cnt FROM payments WHERE date >= '{since.strftime('%Y-%m-%d')}'"),
                    ("status changes", "SELECT COUNT(*) as cnt FROM customers WHERE deleted='0' AND category != 'lead'"),
                ]
                for name, query in tables:
                    rows = fetch_all(conn, query)
                    logger.info("  %s: %d to check", name, rows[0]["cnt"])
                logger.info("Run with --execute to sync")
                return

            # Step 1: New invoices
            inv_result = sync_new_invoices(conn, db, since)
            db.commit()

            # Step 2: New payments
            pay_result = sync_new_payments(conn, db, since)
            db.commit()

            # Step 3: Status changes
            status_result = sync_status_changes(conn, db)
            db.commit()

            logger.info("=== Incremental sync complete ===")
            logger.info(
                "  Invoices: %d new | Payments: %d new | Status: %d updated",
                inv_result["created"],
                pay_result["created"],
                status_result["updated"],
            )


if __name__ == "__main__":
    hours = 24
    for arg in sys.argv:
        if arg.startswith("--hours="):
            hours = int(arg.split("=")[1])

    if "--execute" in sys.argv:
        run_incremental_sync(hours_back=hours, dry_run=False)
    else:
        run_incremental_sync(hours_back=hours, dry_run=True)
        print("\nTo execute: poetry run python -m scripts.migration.incremental_sync --execute")
        print("Options: --hours=48 (default: 24)")
