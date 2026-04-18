"""Phase 2: Migrate financial data from Splynx.

Migrates (in order):
1. invoices + invoices_items → Invoice + InvoiceLine
2. payments → Payment
3. invoice_payers (payment-invoice allocations) → PaymentAllocation
4. billing_transactions → LedgerEntry
5. credit_notes → CreditNote + CreditNoteLine
"""

from __future__ import annotations

import logging
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from scripts.migration.db_connections import (
    dotmac_session,
    fetch_all,
    fetch_batched,
    splynx_connection,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- Splynx invoice status → DotMac InvoiceStatus ---
INVOICE_STATUS_MAP = {
    "not_paid": "issued",
    "paid": "paid",
    "deleted": "void",
    "pending": "draft",
}

# --- Splynx billing_transactions_categories → LedgerCategory ---
LEDGER_CATEGORY_MAP = {
    1: "internet_service",  # Service
    2: "discount",  # Discount
    3: "other",  # Payment (handled as credit)
    4: "other",  # Refund
    5: "other",  # Correction
    6: "other",  # Credit note
    7: "installation_fee",  # Air Fibre Installation Cost
    8: "other",  # Call down support
    11: "equipment_purchase",  # Faulty Device Replacement
    12: "installation_fee",  # Dropcable Rerun
    13: "custom_service",  # IP Addresses
    17: "installation_fee",  # Ground Fibre Installation Cost
    20: "reconnection_fee",  # Relocations
    21: "tax",  # Withholding Tax
    22: "tax",  # Stampduty deducted from sales
}


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


def migrate_invoices(
    conn, db, customer_mapping: dict[int, uuid.UUID]
) -> dict[int, uuid.UUID]:
    """Migrate Splynx invoices + invoices_items → Invoice + InvoiceLine."""
    from app.models.billing import Invoice, InvoiceStatus
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

    # Load existing mappings
    existing_maps = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.invoice
            )
        ).all()
    }
    mapping: dict[int, uuid.UUID] = dict(existing_maps)
    created = 0
    skipped = 0

    query = "SELECT * FROM invoices ORDER BY id"

    for batch in fetch_batched(conn, query, batch_size=1000):
        for row in batch:
            inv_id = row["id"]
            if inv_id in existing_maps:
                continue

            subscriber_id = customer_mapping.get(row.get("customer_id"))
            if not subscriber_id:
                skipped += 1
                continue

            is_deleted = row.get("deleted") == "1"
            status_raw = row.get("status", "not_paid")
            if is_deleted:
                status_str = "void"
            else:
                status_str = INVOICE_STATUS_MAP.get(status_raw, "issued")

            status_enum = {
                "draft": InvoiceStatus.draft,
                "issued": InvoiceStatus.issued,
                "paid": InvoiceStatus.paid,
                "void": InvoiceStatus.void,
            }.get(status_str, InvoiceStatus.issued)

            total = Decimal(str(row.get("total") or "0"))
            due = Decimal(str(row.get("due") or "0"))

            invoice = Invoice(
                account_id=subscriber_id,
                invoice_number=(row.get("number") or "")[:80] or None,
                status=status_enum,
                currency="NGN",
                total=total,
                balance_due=due,
                issued_at=_parse_date(row.get("date_created")),
                due_at=_parse_date(row.get("date_payment")),
                paid_at=_parse_date(row.get("date_updated"))
                if status_str == "paid"
                else None,
                is_proforma=False,
                is_sent=row.get("is_sent") in ("1", 1, True),
                splynx_invoice_id=inv_id,
                is_active=not is_deleted,
            )
            db.add(invoice)

            try:
                db.flush()
            except Exception as e:
                db.rollback()
                logger.warning("Failed invoice %d: %s", inv_id, e)
                skipped += 1
                continue

            db.add(
                SplynxIdMapping(
                    entity_type=SplynxEntityType.invoice,
                    splynx_id=inv_id,
                    dotmac_id=invoice.id,
                )
            )
            mapping[inv_id] = invoice.id
            created += 1

        db.flush()
        if created % 5000 == 0 and created > 0:
            logger.info("Invoices: %d created so far (%d skipped)", created, skipped)

    db.flush()
    logger.info(
        "Invoices: %d created, %d skipped, %d total", created, skipped, len(mapping)
    )
    return mapping


def migrate_invoice_items(
    conn,
    db,
    invoice_mapping: dict[int, uuid.UUID],
    tax_mapping: dict[int, uuid.UUID],
) -> None:
    """Migrate Splynx invoices_items → InvoiceLine."""
    from app.models.billing import InvoiceLine, TaxApplication

    query = "SELECT * FROM invoices_items WHERE deleted='0' ORDER BY invoice_id, pos"
    created = 0
    skipped = 0

    for batch in fetch_batched(conn, query, batch_size=2000):
        for row in batch:
            invoice_id = invoice_mapping.get(row.get("invoice_id"))
            if not invoice_id:
                skipped += 1
                continue

            tax_rate_id = tax_mapping.get(row.get("tax_id"))
            price = Decimal(str(row.get("price") or "0"))
            qty = Decimal(str(row.get("quantity") or "1"))

            line = InvoiceLine(
                invoice_id=invoice_id,
                description=(row.get("description") or "Line item")[:255],
                quantity=qty,
                unit_price=price,
                amount=price * qty,
                tax_rate_id=tax_rate_id,
                tax_application=TaxApplication.exclusive,
                is_active=True,
            )
            db.add(line)
            created += 1

        db.flush()

    logger.info("Invoice lines: %d created, %d skipped", created, skipped)


def migrate_payments(
    conn, db, customer_mapping: dict[int, uuid.UUID]
) -> dict[int, uuid.UUID]:
    """Migrate Splynx payments → Payment."""
    from app.models.billing import Payment, PaymentStatus
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

    existing_maps = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.payment
            )
        ).all()
    }
    mapping: dict[int, uuid.UUID] = dict(existing_maps)
    created = 0
    skipped = 0

    query = "SELECT * FROM payments ORDER BY id"

    for batch in fetch_batched(conn, query, batch_size=1000):
        for row in batch:
            pid = row["id"]
            if pid in existing_maps:
                continue

            subscriber_id = customer_mapping.get(row.get("customer_id"))
            if not subscriber_id:
                skipped += 1
                continue

            is_deleted = row.get("deleted") == "1"
            amount = Decimal(str(row.get("amount") or "0"))

            payment = Payment(
                account_id=subscriber_id,
                amount=amount,
                currency="NGN",
                status=PaymentStatus.succeeded
                if not is_deleted
                else PaymentStatus.canceled,
                paid_at=_parse_date(row.get("date")),
                receipt_number=(row.get("receipt_number") or "")[:120] or None,
                memo=(row.get("comment") or "")[:500] or None,
                splynx_payment_id=pid,
                is_active=not is_deleted,
            )
            db.add(payment)

            try:
                db.flush()
            except Exception as e:
                db.rollback()
                logger.warning("Failed payment %d: %s", pid, e)
                skipped += 1
                continue

            db.add(
                SplynxIdMapping(
                    entity_type=SplynxEntityType.payment,
                    splynx_id=pid,
                    dotmac_id=payment.id,
                )
            )
            mapping[pid] = payment.id
            created += 1

        db.flush()
        if created % 5000 == 0 and created > 0:
            logger.info("Payments: %d created so far (%d skipped)", created, skipped)

    db.flush()
    logger.info(
        "Payments: %d created, %d skipped, %d total", created, skipped, len(mapping)
    )
    return mapping


def migrate_payment_allocations(
    conn,
    db,
    invoice_mapping: dict[int, uuid.UUID],
    payment_mapping: dict[int, uuid.UUID],
) -> None:
    """Migrate Splynx invoice_payers → PaymentAllocation."""
    from app.models.billing import PaymentAllocation

    query = "SELECT * FROM invoice_payers ORDER BY id"
    created = 0
    skipped = 0
    seen: set[tuple] = set()

    for batch in fetch_batched(conn, query, batch_size=2000):
        for row in batch:
            payment_id = payment_mapping.get(row.get("payment_id"))
            invoice_id = invoice_mapping.get(row.get("invoice_id"))
            if not payment_id or not invoice_id:
                skipped += 1
                continue

            # Deduplicate (unique constraint on payment_id + invoice_id)
            key = (payment_id, invoice_id)
            if key in seen:
                skipped += 1
                continue
            seen.add(key)

            amount = Decimal(str(row.get("amount") or "0"))
            alloc = PaymentAllocation(
                payment_id=payment_id,
                invoice_id=invoice_id,
                amount=amount,
            )
            db.add(alloc)
            created += 1

        db.flush()

    logger.info("Payment allocations: %d created, %d skipped", created, skipped)


def migrate_ledger_entries(
    conn,
    db,
    customer_mapping: dict[int, uuid.UUID],
    invoice_mapping: dict[int, uuid.UUID],
    payment_mapping: dict[int, uuid.UUID],
) -> None:
    """Migrate Splynx billing_transactions → LedgerEntry."""
    from app.models.billing import (
        LedgerCategory,
        LedgerEntry,
        LedgerEntryType,
        LedgerSource,
    )

    query = "SELECT * FROM billing_transactions ORDER BY id"
    created = 0
    skipped = 0

    # Build category enum lookup
    cat_enum_map = {v: getattr(LedgerCategory, v) for v in LedgerCategory.__members__}

    for batch in fetch_batched(conn, query, batch_size=2000):
        for row in batch:
            subscriber_id = customer_mapping.get(row.get("customer_id"))
            if not subscriber_id:
                skipped += 1
                continue

            is_deleted = row.get("deleted") == "1"
            entry_type_raw = row.get("type", "debit")
            entry_type = (
                LedgerEntryType.debit
                if entry_type_raw == "debit"
                else LedgerEntryType.credit
            )

            # Source mapping
            if entry_type == LedgerEntryType.debit:
                source = LedgerSource.invoice
            elif row.get("credit_note_id"):
                source = LedgerSource.credit_note
            else:
                source = LedgerSource.payment

            # Category mapping
            cat_id = row.get("category")
            cat_str = LEDGER_CATEGORY_MAP.get(cat_id, "other")
            category = cat_enum_map.get(cat_str)

            amount = abs(Decimal(str(row.get("total") or "0")))
            invoice_id = invoice_mapping.get(row.get("invoice_id"))
            payment_id = payment_mapping.get(row.get("payment_id"))

            entry = LedgerEntry(
                account_id=subscriber_id,
                entry_type=entry_type,
                source=source,
                category=category,
                amount=amount,
                currency="NGN",
                memo=(row.get("description") or "")[:500] or None,
                invoice_id=invoice_id,
                payment_id=payment_id,
                is_active=not is_deleted,
            )
            db.add(entry)
            created += 1

        db.flush()
        if created % 10000 == 0 and created > 0:
            logger.info(
                "Ledger entries: %d created so far (%d skipped)", created, skipped
            )

    db.flush()
    logger.info("Ledger entries: %d created, %d skipped", created, skipped)


def migrate_credit_notes(
    conn,
    db,
    customer_mapping: dict[int, uuid.UUID],
    invoice_mapping: dict[int, uuid.UUID],
) -> None:
    """Migrate Splynx credit_notes → CreditNote + CreditNoteLine."""
    from app.models.billing import CreditNote, CreditNoteStatus
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

    existing_maps = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.credit_note
            )
        ).all()
    }
    query = "SELECT * FROM credit_notes ORDER BY id"
    rows = fetch_all(conn, query)
    created = 0
    skipped = 0

    for row in rows:
        splynx_credit_note_id = row["id"]
        if splynx_credit_note_id in existing_maps:
            continue

        subscriber_id = customer_mapping.get(row.get("customer_id"))
        if not subscriber_id:
            skipped += 1
            continue

        is_deleted = row.get("deleted") == "1"
        status_raw = row.get("status", "not_refunded")
        status_map = {
            "not_refunded": CreditNoteStatus.issued,
            "refunded": CreditNoteStatus.applied,
            "deleted": CreditNoteStatus.void,
            "used": CreditNoteStatus.applied,
        }
        status = status_map.get(status_raw, CreditNoteStatus.issued)
        if is_deleted:
            status = CreditNoteStatus.void

        total = Decimal(str(row.get("total") or "0"))

        cn = CreditNote(
            account_id=subscriber_id,
            invoice_id=invoice_mapping.get(row.get("invoice_id")),
            credit_number=(row.get("number") or "")[:80] or None,
            status=status,
            currency="NGN",
            total=total,
            applied_total=total - Decimal(str(row.get("remind_amount") or "0")),
            memo=(row.get("description") or row.get("comment") or "")[:1000] or None,
            is_active=not is_deleted,
        )
        db.add(cn)
        db.flush()
        db.add(
            SplynxIdMapping(
                entity_type=SplynxEntityType.credit_note,
                splynx_id=splynx_credit_note_id,
                dotmac_id=cn.id,
            )
        )
        created += 1

    db.flush()
    logger.info("Credit notes: %d created, %d skipped", created, skipped)


def run_phase2(dry_run: bool = True) -> None:
    """Execute Phase 2 migration."""
    logger.info("=== Phase 2: Financial Data Migration ===")

    with splynx_connection() as conn:
        with dotmac_session() as db:
            if dry_run:
                logger.info("DRY RUN — counting source data only")
                tables = [
                    ("invoices", "SELECT COUNT(*) as cnt FROM invoices"),
                    (
                        "invoices (not deleted)",
                        "SELECT COUNT(*) as cnt FROM invoices WHERE deleted='0'",
                    ),
                    (
                        "invoices_items",
                        "SELECT COUNT(*) as cnt FROM invoices_items WHERE deleted='0'",
                    ),
                    ("payments", "SELECT COUNT(*) as cnt FROM payments"),
                    ("invoice_payers", "SELECT COUNT(*) as cnt FROM invoice_payers"),
                    (
                        "billing_transactions",
                        "SELECT COUNT(*) as cnt FROM billing_transactions",
                    ),
                    ("credit_notes", "SELECT COUNT(*) as cnt FROM credit_notes"),
                ]
                for name, query in tables:
                    rows = fetch_all(conn, query)
                    logger.info("  %s: %d rows", name, rows[0]["cnt"])
                logger.info("Run with --execute to migrate")
                return

            # Load customer mapping
            from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

            customer_mapping = {
                m.splynx_id: m.dotmac_id
                for m in db.scalars(
                    select(SplynxIdMapping).where(
                        SplynxIdMapping.entity_type == SplynxEntityType.customer
                    )
                ).all()
            }
            logger.info("Loaded %d customer mappings", len(customer_mapping))

            tax_mapping = {
                -m.splynx_id: m.dotmac_id  # Negative IDs for tax rates
                for m in db.scalars(
                    select(SplynxIdMapping).where(
                        SplynxIdMapping.entity_type == SplynxEntityType.tariff,
                        SplynxIdMapping.splynx_id < 0,
                    )
                ).all()
            }
            logger.info("Loaded %d tax mappings", len(tax_mapping))

            # Step 1: Invoices
            invoice_mapping = migrate_invoices(conn, db, customer_mapping)
            db.commit()
            logger.info("--- Invoices committed ---")

            # Step 2: Invoice items
            migrate_invoice_items(conn, db, invoice_mapping, tax_mapping)
            db.commit()
            logger.info("--- Invoice items committed ---")

            # Step 3: Payments
            payment_mapping = migrate_payments(conn, db, customer_mapping)
            db.commit()
            logger.info("--- Payments committed ---")

            # Step 4: Payment allocations
            migrate_payment_allocations(conn, db, invoice_mapping, payment_mapping)
            db.commit()
            logger.info("--- Payment allocations committed ---")

            # Step 5: Ledger entries
            migrate_ledger_entries(
                conn,
                db,
                customer_mapping,
                invoice_mapping,
                payment_mapping,
            )
            db.commit()
            logger.info("--- Ledger entries committed ---")

            # Step 6: Credit notes
            migrate_credit_notes(conn, db, customer_mapping, invoice_mapping)
            db.commit()
            logger.info("--- Credit notes committed ---")

            # Summary
            logger.info("=== Phase 2 complete ===")
            counts = db.execute(
                select(
                    SplynxIdMapping.entity_type, func.count(SplynxIdMapping.id)
                ).group_by(SplynxIdMapping.entity_type)
            ).all()
            logger.info("--- SplynxIdMapping summary ---")
            for entity_type, count in counts:
                logger.info("  %s: %d", entity_type.value, count)


if __name__ == "__main__":
    if "--execute" in sys.argv:
        run_phase2(dry_run=False)
    else:
        run_phase2(dry_run=True)
        print(
            "\nTo execute: poetry run python -m scripts.migration.phase2_financial_data --execute"
        )
