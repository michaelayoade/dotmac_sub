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

import json
import logging
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, text

from app.services.migrations.db_connections import (
    dotmac_session,
    fetch_all,
    splynx_connection,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_ENTITY_INVOICE = "invoice"
_ENTITY_PAYMENT = "payment"
_ENTITY_PAYMENT_ALLOCATION = "payment_allocation"
_ENTITY_BILLING_TRANSACTION = "billing_transaction"

_PAYMENT_DATE_FIELDS = (
    "updated_at",
    "real_create_datetime",
    "payment_date",
    "date",
)

_PAYMENT_COUNT_QUERY_BY_FIELDS: dict[tuple[str, ...], str] = {
    ("updated_at",): "SELECT COUNT(*) as cnt FROM payments WHERE updated_at >= %s",
    ("real_create_datetime",): (
        "SELECT COUNT(*) as cnt FROM payments WHERE real_create_datetime >= %s"
    ),
    ("payment_date",): "SELECT COUNT(*) as cnt FROM payments WHERE payment_date >= %s",
    ("date",): "SELECT COUNT(*) as cnt FROM payments WHERE date >= %s",
    ("updated_at", "real_create_datetime"): (
        "SELECT COUNT(*) as cnt FROM payments "
        "WHERE COALESCE(updated_at, real_create_datetime) >= %s"
    ),
    ("updated_at", "payment_date"): (
        "SELECT COUNT(*) as cnt FROM payments "
        "WHERE COALESCE(updated_at, payment_date) >= %s"
    ),
    ("updated_at", "date"): (
        "SELECT COUNT(*) as cnt FROM payments WHERE COALESCE(updated_at, date) >= %s"
    ),
    ("real_create_datetime", "payment_date"): (
        "SELECT COUNT(*) as cnt FROM payments "
        "WHERE COALESCE(real_create_datetime, payment_date) >= %s"
    ),
    ("real_create_datetime", "date"): (
        "SELECT COUNT(*) as cnt FROM payments "
        "WHERE COALESCE(real_create_datetime, date) >= %s"
    ),
    ("payment_date", "date"): (
        "SELECT COUNT(*) as cnt FROM payments WHERE COALESCE(payment_date, date) >= %s"
    ),
    ("updated_at", "real_create_datetime", "payment_date"): (
        "SELECT COUNT(*) as cnt FROM payments "
        "WHERE COALESCE(updated_at, real_create_datetime, payment_date) >= %s"
    ),
    ("updated_at", "real_create_datetime", "date"): (
        "SELECT COUNT(*) as cnt FROM payments "
        "WHERE COALESCE(updated_at, real_create_datetime, date) >= %s"
    ),
    ("updated_at", "payment_date", "date"): (
        "SELECT COUNT(*) as cnt FROM payments "
        "WHERE COALESCE(updated_at, payment_date, date) >= %s"
    ),
    ("real_create_datetime", "payment_date", "date"): (
        "SELECT COUNT(*) as cnt FROM payments "
        "WHERE COALESCE(real_create_datetime, payment_date, date) >= %s"
    ),
    ("updated_at", "real_create_datetime", "payment_date", "date"): (
        "SELECT COUNT(*) as cnt FROM payments "
        "WHERE COALESCE(updated_at, real_create_datetime, payment_date, date) >= %s"
    ),
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


def _is_splynx_deleted(value) -> bool:
    """Normalize Splynx deleted flags from MySQL and FDW/staging rows."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _can_use_sync_state(db) -> bool:
    return hasattr(db, "execute")


def _ensure_sync_state_tables(db) -> None:
    """Create lightweight sync cursor/skip tables if migrations are not applied yet."""
    if not _can_use_sync_state(db):
        return
    session_info = getattr(db, "info", None)
    if isinstance(session_info, dict) and session_info.get("splynx_sync_state_ready"):
        return
    db.execute(
        text("""
            CREATE TABLE IF NOT EXISTS splynx_sync_cursors (
                entity VARCHAR(40) PRIMARY KEY,
                last_splynx_id INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
    )
    db.execute(
        text("""
            CREATE TABLE IF NOT EXISTS splynx_sync_skips (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                entity VARCHAR(40) NOT NULL,
                splynx_id INTEGER NOT NULL,
                customer_id INTEGER,
                reason VARCHAR(80) NOT NULL,
                payload JSONB,
                attempts INTEGER NOT NULL DEFAULT 0,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                resolved_at TIMESTAMPTZ,
                UNIQUE (entity, splynx_id)
            )
        """)
    )
    db.execute(
        text("""
            CREATE INDEX IF NOT EXISTS ix_splynx_sync_skips_unresolved
            ON splynx_sync_skips (entity, resolved_at, splynx_id)
        """)
    )
    if isinstance(session_info, dict):
        session_info["splynx_sync_state_ready"] = True


def _get_cursor(db, entity: str) -> int:
    if not _can_use_sync_state(db):
        return 0
    _ensure_sync_state_tables(db)
    value = db.execute(
        text("SELECT last_splynx_id FROM splynx_sync_cursors WHERE entity = :entity"),
        {"entity": entity},
    ).scalar()
    return int(value or 0)


def _set_cursor(db, entity: str, last_splynx_id: int) -> None:
    if not _can_use_sync_state(db):
        return
    _ensure_sync_state_tables(db)
    db.execute(
        text("""
            INSERT INTO splynx_sync_cursors (entity, last_splynx_id, updated_at)
            VALUES (:entity, :last_splynx_id, NOW())
            ON CONFLICT (entity) DO UPDATE SET
                last_splynx_id = GREATEST(
                    splynx_sync_cursors.last_splynx_id,
                    EXCLUDED.last_splynx_id
                ),
                updated_at = NOW()
        """),
        {"entity": entity, "last_splynx_id": int(last_splynx_id or 0)},
    )


def _retry_ids(db, entity: str, limit: int = 1000) -> list[int]:
    if not _can_use_sync_state(db):
        return []
    _ensure_sync_state_tables(db)
    return [
        int(row[0])
        for row in db.execute(
            text("""
                SELECT splynx_id
                FROM splynx_sync_skips
                WHERE entity = :entity AND resolved_at IS NULL
                ORDER BY splynx_id
                LIMIT :limit
            """),
            {"entity": entity, "limit": limit},
        ).all()
    ]


def _record_skip(
    db,
    *,
    entity: str,
    splynx_id: int,
    customer_id: int | None,
    reason: str,
    payload: dict | None = None,
) -> None:
    if not _can_use_sync_state(db):
        return
    _ensure_sync_state_tables(db)
    db.execute(
        text("""
            INSERT INTO splynx_sync_skips (
                entity, splynx_id, customer_id, reason, payload, attempts, last_seen_at
            )
            VALUES (
                :entity, :splynx_id, :customer_id, :reason,
                CAST(:payload AS jsonb), 1, NOW()
            )
            ON CONFLICT (entity, splynx_id) DO UPDATE SET
                customer_id = EXCLUDED.customer_id,
                reason = EXCLUDED.reason,
                payload = EXCLUDED.payload,
                attempts = splynx_sync_skips.attempts + 1,
                last_seen_at = NOW(),
                resolved_at = NULL
        """),
        {
            "entity": entity,
            "splynx_id": int(splynx_id),
            "customer_id": customer_id,
            "reason": reason,
            "payload": json.dumps(payload or {}),
        },
    )


def _resolve_skip(db, *, entity: str, splynx_id: int) -> None:
    if not _can_use_sync_state(db):
        return
    _ensure_sync_state_tables(db)
    db.execute(
        text("""
            UPDATE splynx_sync_skips
            SET resolved_at = NOW(), last_seen_at = NOW()
            WHERE entity = :entity AND splynx_id = :splynx_id AND resolved_at IS NULL
        """),
        {"entity": entity, "splynx_id": int(splynx_id)},
    )


def _id_window_clause(db, entity: str) -> tuple[str, int]:
    cursor = _get_cursor(db, entity)
    retry_ids = _retry_ids(db, entity)
    clauses = [f"id > {cursor}"]  # noqa: S608 - cursor is internal integer state.
    if retry_ids:
        ids = ",".join(str(int(value)) for value in retry_ids)
        clauses.append(f"id IN ({ids})")  # noqa: S608 - retry IDs are internal ints.
    return " OR ".join(clauses), cursor


def _table_columns(conn, table_name: str) -> set[str]:
    """Return source table columns from Splynx MySQL."""
    if table_name not in {"payments"}:
        raise ValueError(f"Unsupported Splynx table: {table_name}")
    rows = fetch_all(conn, f"SHOW COLUMNS FROM {table_name}")  # noqa: S608
    return {str(row.get("Field") or row.get("field") or "") for row in rows}


def _payment_since_fields(conn) -> tuple[str, ...]:
    columns = _table_columns(conn, "payments")
    fields = tuple(field for field in _PAYMENT_DATE_FIELDS if field in columns)
    if not fields:
        raise RuntimeError("Splynx payments table has no usable date column")
    return fields


def _payment_since_expression(conn) -> str:
    fields = _payment_since_fields(conn)
    if len(fields) == 1:
        return fields[0]
    return f"COALESCE({', '.join(fields)})"


def _payment_count_query(conn) -> str:
    fields = _payment_since_fields(conn)
    query = _PAYMENT_COUNT_QUERY_BY_FIELDS.get(fields)
    if query is None:
        raise RuntimeError(f"Unsupported Splynx payments date columns: {fields!r}")
    return query


def _payment_paid_at(row: dict) -> datetime | None:
    return _parse_date(
        row.get("payment_date")
        or row.get("date")
        or row.get("real_create_datetime")
        or row.get("updated_at")
    )


def _fetch_invoice_items(conn, inv_id: int) -> list[dict]:
    """Fetch non-deleted line items for a Splynx invoice."""
    return fetch_all(
        conn,
        f"SELECT * FROM invoices_items WHERE invoice_id = {inv_id} AND deleted = '0'",  # noqa: S608  # nosec B608
    )


def _compute_invoice_aggregates(
    items: list[dict],
    row: dict,
) -> tuple[Decimal, Decimal, datetime | None, datetime | None]:
    """Compute subtotal, tax_total, billing_period_start/end from line items.

    Falls back to invoice-level fields when line items are missing or
    have no period data.
    """
    subtotal = Decimal("0")
    tax_total = Decimal("0")
    period_starts: list[datetime | None] = []
    period_ends: list[datetime | None] = []

    for item in items:
        price = Decimal(str(item.get("price") or "0"))
        qty = Decimal(str(item.get("quantity") or "1"))
        tax_pct = Decimal(str(item.get("tax") or "0"))
        line_amount = price * qty
        subtotal += line_amount
        tax_total += line_amount * tax_pct / Decimal("100")

        period_starts.append(_parse_date(item.get("period_from")))
        period_ends.append(_parse_date(item.get("period_to")))

    # Fall back to invoice total if no line items
    if not items:
        subtotal = Decimal(str(row.get("total") or "0"))
        tax_total = Decimal("0")

    # Billing period: min(period_from), max(period_to) from items
    valid_starts = [d for d in period_starts if d]
    valid_ends = [d for d in period_ends if d]
    billing_start = (
        min(valid_starts) if valid_starts else _parse_date(row.get("date_created"))
    )
    billing_end = max(valid_ends) if valid_ends else _parse_date(row.get("date_till"))

    return subtotal, tax_total, billing_start, billing_end


def _resolve_subscription_id(
    conn,
    service_map: dict[int, str],
    transaction_id: int | None,
) -> str | None:
    """Map a line item's transaction_id → Splynx service_id → DotMac subscription_id."""
    if not transaction_id:
        return None
    rows = fetch_all(
        conn,
        f"SELECT service_id FROM billing_transactions WHERE id = {transaction_id}",  # noqa: S608  # nosec B608
    )
    if not rows:
        return None
    service_id = rows[0].get("service_id")
    if not service_id:
        return None
    return service_map.get(int(service_id))


def sync_new_invoices(conn, db, since: datetime | None = None) -> dict[str, int]:
    """Sync invoices from Splynx.

    When ``since`` is omitted, use durable Splynx ID cursors plus retryable skips.
    The timed mode is retained for dry-run/manual compatibility.
    """
    from app.models.billing import Invoice, InvoiceLine, InvoiceStatus, TaxApplication
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping
    from app.models.subscriber import Subscriber

    valid_subscriber_ids = set(db.scalars(select(Subscriber.id)).all())
    customer_map = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.customer
            )
        ).all()
        if m.dotmac_id in valid_subscriber_ids
    }
    service_map: dict[int, str] = {
        m.splynx_id: str(m.dotmac_id)
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.service
            )
        ).all()
    }
    existing_invoice_ids = set(
        db.scalars(
            select(Invoice.splynx_invoice_id).where(
                Invoice.splynx_invoice_id.is_not(None)
            )
        ).all()
    )
    invoice_mappings = {
        m.splynx_id: m
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.invoice
            )
        ).all()
    }

    cursor_mode = since is None
    if cursor_mode:
        where_clause, cursor = _id_window_clause(db, _ENTITY_INVOICE)
        query = f"SELECT * FROM invoices WHERE {where_clause} ORDER BY id"  # noqa: S608  # nosec B608
    else:
        assert since is not None  # cursor_mode is False ⇒ since is set
        cursor = 0
        since_str = since.strftime("%Y-%m-%d %H:%M:%S")
        query = f"""
            SELECT * FROM invoices
            WHERE real_create_datetime >= '{since_str}'
            ORDER BY id
        """  # noqa: S608  # nosec B608
    rows = fetch_all(conn, query)
    created = 0
    skipped = 0
    resolved = 0
    max_seen_id = cursor

    for row in rows:
        inv_id = row["id"]
        max_seen_id = max(max_seen_id, int(inv_id))
        if inv_id in existing_invoice_ids:
            skipped += 1
            _resolve_skip(db, entity=_ENTITY_INVOICE, splynx_id=inv_id)
            continue

        subscriber_id = customer_map.get(row.get("customer_id"))
        if not subscriber_id:
            skipped += 1
            _record_skip(
                db,
                entity=_ENTITY_INVOICE,
                splynx_id=inv_id,
                customer_id=row.get("customer_id"),
                reason="customer_not_mapped",
                payload={
                    "number": row.get("number"),
                    "status": row.get("status"),
                    "deleted": row.get("deleted"),
                    "real_create_datetime": str(row.get("real_create_datetime") or ""),
                },
            )
            continue

        is_deleted = _is_splynx_deleted(row.get("deleted"))
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

        # Fetch line items first to compute aggregates
        items = _fetch_invoice_items(conn, inv_id)
        subtotal, tax_total, billing_start, billing_end = _compute_invoice_aggregates(
            items, row
        )

        invoice = Invoice(
            account_id=subscriber_id,
            invoice_number=(row.get("number") or "")[:80] or None,
            status=status,
            currency="NGN",
            subtotal=subtotal,
            tax_total=tax_total,
            total=total,
            balance_due=due,
            billing_period_start=billing_start,
            billing_period_end=billing_end,
            issued_at=_parse_date(row.get("date_created")),
            due_at=_parse_date(row.get("date_till")),
            paid_at=_parse_date(row.get("date_updated"))
            if status == InvoiceStatus.paid
            else None,
            is_sent=row.get("is_sent") in ("1", 1, True),
            splynx_invoice_id=inv_id,
            is_active=not is_deleted,
        )
        db.add(invoice)
        db.flush()

        existing_mapping = invoice_mappings.get(inv_id)
        if existing_mapping:
            existing_mapping.dotmac_id = invoice.id
            existing_mapping.metadata_ = {
                **(existing_mapping.metadata_ or {}),
                "orphan_repaired_at": datetime.now(UTC).isoformat(),
            }
        else:
            db.add(
                SplynxIdMapping(
                    entity_type=SplynxEntityType.invoice,
                    splynx_id=inv_id,
                    dotmac_id=invoice.id,
                )
            )

        # Create invoice line items
        for item in items:
            price = Decimal(str(item.get("price") or "0"))
            qty = Decimal(str(item.get("quantity") or "1"))
            subscription_id = _resolve_subscription_id(
                conn, service_map, item.get("transaction_id")
            )
            line = InvoiceLine(
                invoice_id=invoice.id,
                subscription_id=subscription_id,
                description=(item.get("description") or "Line item")[:255],
                quantity=qty,
                unit_price=price,
                amount=price * qty,
                tax_application=TaxApplication.exclusive,
                is_active=True,
            )
            db.add(line)

        created += 1
        existing_invoice_ids.add(inv_id)
        _resolve_skip(db, entity=_ENTITY_INVOICE, splynx_id=inv_id)
        resolved += 1

    db.flush()
    if cursor_mode:
        _set_cursor(db, _ENTITY_INVOICE, max_seen_id)
    logger.info(
        "Invoices synced: %d new, %d skipped, %d resolved skips",
        created,
        skipped,
        resolved,
    )
    return {"created": created, "skipped": skipped, "resolved": resolved}


def sync_new_payments(conn, db, since: datetime | None = None) -> dict[str, int]:
    """Sync payments from Splynx.

    When ``since`` is omitted, use durable Splynx ID cursors plus retryable skips.
    The timed mode is retained for dry-run/manual compatibility.
    """
    from app.models.billing import Payment, PaymentStatus
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping
    from app.models.subscriber import Subscriber

    valid_subscriber_ids = set(db.scalars(select(Subscriber.id)).all())
    customer_map = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.customer
            )
        ).all()
        if m.dotmac_id in valid_subscriber_ids
    }
    existing_payment_ids = set(
        db.scalars(
            select(Payment.splynx_payment_id).where(
                Payment.splynx_payment_id.is_not(None)
            )
        ).all()
    )
    payment_mappings = {
        m.splynx_id: m
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.payment
            )
        ).all()
    }

    cursor_mode = since is None
    if cursor_mode:
        where_clause, cursor = _id_window_clause(db, _ENTITY_PAYMENT)
        query = f"SELECT * FROM payments WHERE {where_clause} ORDER BY id"  # noqa: S608  # nosec B608
    else:
        assert since is not None  # cursor_mode is False ⇒ since is set
        cursor = 0
        since_str = since.strftime("%Y-%m-%d %H:%M:%S")
        payment_since_expr = _payment_since_expression(conn)
        query = f"""
            SELECT * FROM payments
            WHERE {payment_since_expr} >= '{since_str}'
            ORDER BY id
        """  # noqa: S608  # nosec B608
    rows = fetch_all(conn, query)
    created = 0
    skipped = 0
    resolved = 0
    max_seen_id = cursor

    for row in rows:
        pid = row["id"]
        max_seen_id = max(max_seen_id, int(pid))
        if pid in existing_payment_ids:
            skipped += 1
            _resolve_skip(db, entity=_ENTITY_PAYMENT, splynx_id=pid)
            continue

        subscriber_id = customer_map.get(row.get("customer_id"))
        if not subscriber_id:
            skipped += 1
            _record_skip(
                db,
                entity=_ENTITY_PAYMENT,
                splynx_id=pid,
                customer_id=row.get("customer_id"),
                reason="customer_not_mapped",
                payload={
                    "invoice_id": row.get("invoice_id"),
                    "transaction_id": row.get("transaction_id"),
                    "deleted": row.get("deleted"),
                    "date": str(row.get("date") or ""),
                    "updated_at": str(row.get("updated_at") or ""),
                },
            )
            continue

        is_deleted = _is_splynx_deleted(row.get("deleted"))
        amount = Decimal(str(row.get("amount") or "0"))
        paid_at = _payment_paid_at(row)

        payment = Payment(
            account_id=subscriber_id,
            amount=amount,
            currency="NGN",
            status=PaymentStatus.succeeded
            if not is_deleted
            else PaymentStatus.canceled,
            paid_at=paid_at,
            receipt_number=(row.get("receipt_number") or "")[:120] or None,
            memo=(row.get("comment") or "")[:500] or None,
            splynx_payment_id=pid,
            is_active=not is_deleted,
        )
        db.add(payment)
        db.flush()

        existing_mapping = payment_mappings.get(pid)
        if existing_mapping:
            existing_mapping.dotmac_id = payment.id
            existing_mapping.metadata_ = {
                **(existing_mapping.metadata_ or {}),
                "orphan_repaired_at": datetime.now(UTC).isoformat(),
            }
        else:
            db.add(
                SplynxIdMapping(
                    entity_type=SplynxEntityType.payment,
                    splynx_id=pid,
                    dotmac_id=payment.id,
                )
            )
        created += 1
        existing_payment_ids.add(pid)
        _resolve_skip(db, entity=_ENTITY_PAYMENT, splynx_id=pid)
        resolved += 1

    db.flush()
    if cursor_mode:
        _set_cursor(db, _ENTITY_PAYMENT, max_seen_id)
    logger.info(
        "Payments synced: %d new, %d skipped, %d resolved skips",
        created,
        skipped,
        resolved,
    )
    return {"created": created, "skipped": skipped, "resolved": resolved}


def sync_payment_allocations(conn, db) -> dict[str, int]:
    """Sync Splynx invoice_payers payment allocations into PaymentAllocation."""
    from app.models.billing import Invoice, Payment, PaymentAllocation

    payment_map = {
        p.splynx_payment_id: p.id
        for p in db.scalars(
            select(Payment).where(Payment.splynx_payment_id.is_not(None))
        ).all()
    }
    invoice_map = {
        i.splynx_invoice_id: i.id
        for i in db.scalars(
            select(Invoice).where(Invoice.splynx_invoice_id.is_not(None))
        ).all()
    }
    existing_pairs = {
        (row.payment_id, row.invoice_id)
        for row in db.scalars(select(PaymentAllocation)).all()
    }

    where_clause, cursor = _id_window_clause(db, _ENTITY_PAYMENT_ALLOCATION)
    query = (
        "SELECT * FROM invoice_payers "
        f"WHERE type = 'payment' AND ({where_clause}) "
        "ORDER BY id"
    )  # noqa: S608  # nosec B608
    rows = fetch_all(conn, query)
    created = 0
    skipped = 0
    resolved = 0
    max_seen_id = cursor

    for row in rows:
        alloc_id = int(row["id"])
        max_seen_id = max(max_seen_id, alloc_id)
        splynx_payment_id = row.get("payer_id")
        splynx_invoice_id = row.get("invoice_id")
        payment_id = payment_map.get(splynx_payment_id)
        invoice_id = invoice_map.get(splynx_invoice_id)

        if not payment_id:
            skipped += 1
            _record_skip(
                db,
                entity=_ENTITY_PAYMENT_ALLOCATION,
                splynx_id=alloc_id,
                customer_id=row.get("customer_id"),
                reason="payment_not_imported",
                payload={
                    "payer_id": splynx_payment_id,
                    "invoice_id": splynx_invoice_id,
                    "amount": str(row.get("amount") or "0"),
                },
            )
            continue
        if not invoice_id:
            skipped += 1
            # An invoice_payers row with no invoice_id is an allocation to the
            # customer's balance/deposit, not to an invoice — there is no local
            # invoice to apply it to and never will be (it's already reflected in
            # the authoritative Splynx deposit). Resolve it rather than recording
            # a retryable "invoice_not_imported" skip that re-runs every sync.
            if not splynx_invoice_id or int(splynx_invoice_id) == 0:
                _resolve_skip(db, entity=_ENTITY_PAYMENT_ALLOCATION, splynx_id=alloc_id)
                continue
            _record_skip(
                db,
                entity=_ENTITY_PAYMENT_ALLOCATION,
                splynx_id=alloc_id,
                customer_id=row.get("customer_id"),
                reason="invoice_not_imported",
                payload={
                    "payer_id": splynx_payment_id,
                    "invoice_id": splynx_invoice_id,
                    "amount": str(row.get("amount") or "0"),
                },
            )
            continue

        key = (payment_id, invoice_id)
        if key in existing_pairs:
            skipped += 1
            _resolve_skip(db, entity=_ENTITY_PAYMENT_ALLOCATION, splynx_id=alloc_id)
            continue

        db.add(
            PaymentAllocation(
                payment_id=payment_id,
                invoice_id=invoice_id,
                amount=Decimal(str(row.get("amount") or "0")),
                memo=f"Synced from Splynx invoice_payers #{alloc_id}",
            )
        )
        existing_pairs.add(key)
        created += 1
        resolved += 1
        _resolve_skip(db, entity=_ENTITY_PAYMENT_ALLOCATION, splynx_id=alloc_id)

    db.flush()
    _set_cursor(db, _ENTITY_PAYMENT_ALLOCATION, max_seen_id)
    logger.info(
        "Payment allocations synced: %d new, %d skipped, %d resolved skips",
        created,
        skipped,
        resolved,
    )
    return {"created": created, "skipped": skipped, "resolved": resolved}


def _bt_date(value):
    """Coerce a Splynx date/datetime/string to a date (None for empty/0000)."""
    from datetime import date as _date

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    s = str(value)
    if not s or s.startswith("0000"):
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _bt_int(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n or None  # Splynx uses 0 for an absent FK; treat as None.


def sync_billing_transactions(conn, db) -> dict[str, int]:
    """Keep the ``splynx_billing_transactions`` mirror at parity with Splynx.

    Splynx's raw ``billing_transactions`` ledger is append-only and its running
    net per customer IS the authoritative deposit. The one-off importer
    (``scripts/billing/import_splynx_transactions.py``) seeds the mirror; this
    step tails new rows by id cursor so the mirror — and therefore the deposit
    reconciliation — stays current without a manual re-import.

    Idempotent: cursor-driven and guarded by the unique splynx_transaction_id.
    On first run the cursor is seeded from the mirror's max id so the ~232k
    already-imported rows are not re-scanned.
    """
    from app.models.splynx_transaction import SplynxBillingTransaction
    from app.models.subscriber import Subscriber

    cursor = _get_cursor(db, _ENTITY_BILLING_TRANSACTION)
    if cursor == 0:
        cursor = int(
            db.scalar(
                select(
                    func.coalesce(
                        func.max(SplynxBillingTransaction.splynx_transaction_id), 0
                    )
                )
            )
            or 0
        )

    sub_map = {
        int(cid): sid
        for cid, sid in db.execute(
            select(Subscriber.splynx_customer_id, Subscriber.id).where(
                Subscriber.splynx_customer_id.is_not(None)
            )
        ).all()
    }
    cats = {
        int(r["id"]): r["name"]
        for r in fetch_all(conn, "SELECT id, name FROM billing_transactions_categories")
    }

    rows = fetch_all(
        conn,
        "SELECT id, customer_id, type, total, category, description, date, "
        "period_from, period_to, invoice_id, payment_id, credit_note_id, "
        "service_id, service_type, source, deleted FROM billing_transactions "
        f"WHERE id > {int(cursor)} ORDER BY id",  # noqa: S608 - cursor is internal int.
    )
    created = 0
    unlinked = 0
    max_seen_id = cursor
    pending: list[dict] = []
    now = datetime.now(UTC)
    for r in rows:
        tid = int(r["id"])
        max_seen_id = max(max_seen_id, tid)
        cid = int(r["customer_id"])
        sid = sub_map.get(cid)
        if sid is None:
            unlinked += 1
        etype = (r["type"] or "").strip().lower()
        pending.append(
            {
                "id": uuid.uuid4(),
                "splynx_transaction_id": tid,
                "splynx_customer_id": cid,
                "subscriber_id": sid,
                "entry_type": etype if etype in ("credit", "debit") else "other",
                "amount": Decimal(str(r["total"] or "0")),
                "category_id": _bt_int(r["category"]),
                "category_name": cats.get(_bt_int(r["category"]) or -1),
                "description": (r["description"] or None),
                "transaction_date": _bt_date(r["date"]),
                "period_from": _bt_date(r["period_from"]),
                "period_to": _bt_date(r["period_to"]),
                "splynx_invoice_id": _bt_int(r["invoice_id"]),
                "splynx_payment_id": _bt_int(r["payment_id"]),
                "splynx_credit_note_id": _bt_int(r["credit_note_id"]),
                "service_id": _bt_int(r["service_id"]),
                "service_type": (r["service_type"] or None),
                "source": (r["source"] or None),
                "deleted": str(r["deleted"]) == "1",
                "created_at": now,
                "updated_at": now,
            }
        )
        created += 1

    if pending:
        # ON CONFLICT keeps the step safe if a row was already seeded by the importer.
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(SplynxBillingTransaction).values(pending)
        db.execute(
            stmt.on_conflict_do_nothing(index_elements=["splynx_transaction_id"])
        )

    _set_cursor(db, _ENTITY_BILLING_TRANSACTION, max_seen_id)
    logger.info(
        "Billing transactions mirrored: %d new (%d unlinked to a subscriber)",
        created,
        unlinked,
    )
    return {"created": created, "unlinked": unlinked}


def sync_status_changes(conn, db) -> dict[str, int]:
    """Sync customer status changes from Splynx."""
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping
    from app.models.subscriber import Subscriber, SubscriberStatus

    status_map = {
        "active": SubscriberStatus.active,
        "blocked": SubscriberStatus.blocked,
        "disabled": SubscriberStatus.disabled,
        "new": SubscriberStatus.new,
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


def sync_deleted_customers(conn, db) -> dict[str, int]:
    """Detect customers deleted in Splynx and soft-delete in DotMac."""
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping
    from app.models.subscriber import Subscriber, SubscriberStatus

    # Get all mapped customer IDs
    customer_map = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.customer
            )
        ).all()
    }

    if not customer_map:
        return {"soft_deleted": 0}

    # Find which mapped customers are now deleted in Splynx
    splynx_ids = ",".join(str(sid) for sid in customer_map)
    query = f"SELECT id FROM customers WHERE id IN ({splynx_ids}) AND deleted = '1'"  # noqa: S608  # nosec B608
    deleted_rows = fetch_all(conn, query)
    deleted_splynx_ids = {row["id"] for row in deleted_rows}

    soft_deleted = 0
    for splynx_id in deleted_splynx_ids:
        dotmac_id = customer_map.get(splynx_id)
        if not dotmac_id:
            continue
        subscriber = db.get(Subscriber, dotmac_id)
        if subscriber and subscriber.is_active:
            subscriber.is_active = False
            subscriber.status = SubscriberStatus.canceled
            # Update metadata to record the deletion source
            metadata = subscriber.metadata_ or {}
            metadata["splynx_deleted"] = True
            subscriber.metadata_ = metadata
            soft_deleted += 1

    db.flush()
    logger.info("Deleted customers synced: %d soft-deleted", soft_deleted)
    return {"soft_deleted": soft_deleted}


def sync_new_credit_notes(conn, db, since: datetime) -> dict[str, int]:
    """Sync credit notes created since the given timestamp.

    Credit notes use the per-row ``splynx_credit_note_id`` column for linkage
    (not ``splynx_id_mappings``), matching how phase2 imports them.
    """
    from sqlalchemy import select as sa_select

    from app.models.billing import CreditNote, CreditNoteStatus
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

    customer_map = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.customer
            )
        ).all()
    }
    existing_ids = set(
        db.scalars(
            sa_select(CreditNote.splynx_credit_note_id).where(
                CreditNote.splynx_credit_note_id.is_not(None)
            )
        ).all()
    )

    status_map = {
        "used": CreditNoteStatus.applied,
        "not_refunded": CreditNoteStatus.issued,
        "deleted": CreditNoteStatus.void,
    }

    since_str = since.strftime("%Y-%m-%d %H:%M:%S")
    query = f"SELECT * FROM credit_notes WHERE real_create_datetime >= '{since_str}' ORDER BY id"  # noqa: S608  # nosec B608
    rows = fetch_all(conn, query)
    created = 0
    skipped = 0

    for row in rows:
        cn_id = row["id"]
        if cn_id in existing_ids:
            skipped += 1
            continue
        account_id = customer_map.get(row.get("customer_id"))
        if not account_id:
            skipped += 1
            continue

        is_deleted = _is_splynx_deleted(row.get("deleted"))
        status = status_map.get(str(row.get("status") or ""), CreditNoteStatus.issued)
        if is_deleted:
            status = CreditNoteStatus.void
        total = Decimal(str(row.get("total") or "0"))
        applied = total if status == CreditNoteStatus.applied else Decimal("0")

        db.add(
            CreditNote(
                account_id=account_id,
                credit_number=(row.get("number") or "")[:80] or None,
                status=status,
                currency="NGN",
                subtotal=Decimal("0"),
                tax_total=Decimal("0"),
                total=total,
                applied_total=applied,
                memo=row.get("note") or None,
                is_active=not is_deleted,
                splynx_credit_note_id=cn_id,
            )
        )
        created += 1

    db.flush()
    logger.info("Credit notes synced: %d new, %d skipped", created, skipped)
    return {"created": created, "skipped": skipped}


def sync_deleted_services(conn, db) -> dict[str, int]:
    """Detect services deleted in Splynx and cancel in DotMac."""
    from app.models.catalog import Subscription, SubscriptionStatus
    from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping

    # Get all mapped service IDs (internet services only, splynx_id < 200000)
    service_map = {
        m.splynx_id: m.dotmac_id
        for m in db.scalars(
            select(SplynxIdMapping).where(
                SplynxIdMapping.entity_type == SplynxEntityType.service,
                SplynxIdMapping.splynx_id < 200000,
            )
        ).all()
    }

    if not service_map:
        return {"canceled": 0}

    splynx_ids = ",".join(str(sid) for sid in service_map)
    query = (
        f"SELECT id FROM services_internet WHERE id IN ({splynx_ids}) AND deleted = '1'"  # noqa: S608  # nosec B608
    )
    deleted_rows = fetch_all(conn, query)
    deleted_splynx_ids = {row["id"] for row in deleted_rows}

    canceled = 0
    for splynx_id in deleted_splynx_ids:
        dotmac_id = service_map.get(splynx_id)
        if not dotmac_id:
            continue
        subscription = db.get(Subscription, dotmac_id)
        if subscription and subscription.status != SubscriptionStatus.canceled:
            subscription.status = SubscriptionStatus.canceled
            canceled += 1

    db.flush()
    logger.info("Deleted services synced: %d canceled", canceled)
    return {"canceled": canceled}


def run_incremental_sync(
    hours_back: int = 24,
    dry_run: bool = True,
) -> None:
    """Execute incremental sync.

    Dry-run reports a timestamp window for operator visibility. Execution uses
    durable ID cursors for invoices, payments, and payment allocations.
    """
    since = datetime.now(UTC) - timedelta(hours=hours_back)
    logger.info("=== Incremental Sync (since %s) ===", since.isoformat())

    with splynx_connection() as conn:
        with dotmac_session() as db:
            if dry_run:
                since_str = since.strftime("%Y-%m-%d %H:%M:%S")
                _ensure_sync_state_tables(db)
                invoice_clause, _ = _id_window_clause(db, _ENTITY_INVOICE)
                payment_clause, _ = _id_window_clause(db, _ENTITY_PAYMENT)
                allocation_clause, _ = _id_window_clause(db, _ENTITY_PAYMENT_ALLOCATION)
                tables = [
                    (
                        "cursor invoices",
                        f"SELECT COUNT(*) as cnt FROM invoices WHERE {invoice_clause}",
                        None,
                    ),
                    (
                        "cursor payments",
                        f"SELECT COUNT(*) as cnt FROM payments WHERE {payment_clause}",
                        None,
                    ),
                    (
                        "cursor payment allocations",
                        "SELECT COUNT(*) as cnt FROM invoice_payers "
                        f"WHERE type = 'payment' AND ({allocation_clause})",
                        None,
                    ),
                    (
                        "new credit notes",
                        "SELECT COUNT(*) as cnt FROM credit_notes "
                        "WHERE real_create_datetime >= %s",
                        (since_str,),
                    ),
                    (
                        "status changes",
                        "SELECT COUNT(*) as cnt FROM customers WHERE deleted='0' AND category != 'lead'",
                        None,
                    ),
                    (
                        "deleted customers",
                        "SELECT COUNT(*) as cnt FROM customers WHERE deleted='1' AND category != 'lead'",
                        None,
                    ),
                    (
                        "deleted services",
                        "SELECT COUNT(*) as cnt FROM services_internet WHERE deleted='1'",
                        None,
                    ),
                ]
                for name, query, params in tables:
                    rows = fetch_all(conn, query, params)
                    logger.info("  %s: %d to check", name, rows[0]["cnt"])
                logger.info("Run with --execute to sync")
                return

            # Step 1: New invoices (cursor + retry skips)
            inv_result = sync_new_invoices(conn, db)
            db.commit()

            # Step 2: New payments (cursor + retry skips)
            pay_result = sync_new_payments(conn, db)
            db.commit()

            # Step 3: Payment allocations (cursor + retry skips)
            alloc_result = sync_payment_allocations(conn, db)
            db.commit()

            # Step 4: New credit notes
            cn_result = sync_new_credit_notes(conn, db, since)
            db.commit()

            # Step 5: Status changes
            status_result = sync_status_changes(conn, db)
            db.commit()

            # Step 6: Detect deletions
            del_cust_result = sync_deleted_customers(conn, db)
            db.commit()

            del_svc_result = sync_deleted_services(conn, db)
            db.commit()

            logger.info("=== Incremental sync complete ===")
            logger.info(
                "  Invoices: %d new | Payments: %d new | Allocations: %d new"
                " | Credit notes: %d new | Status: %d updated"
                " | Customers deleted: %d | Services canceled: %d",
                inv_result["created"],
                pay_result["created"],
                alloc_result["created"],
                cn_result["created"],
                status_result["updated"],
                del_cust_result["soft_deleted"],
                del_svc_result["canceled"],
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
        print(
            "\nTo execute: poetry run python -m scripts.migration.incremental_sync --execute"
        )
        print("Options: --hours=48 (default: 24)")
