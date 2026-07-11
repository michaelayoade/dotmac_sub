"""Shared invoice collectibility predicates and balance helpers."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus
from app.services.common import coerce_uuid

OPEN_INVOICE_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)
DUE_INVOICE_STATUSES = OPEN_INVOICE_STATUSES
OVERDUE_DEBT_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)


def open_invoice_filters(account_id=None) -> tuple:
    """SQL predicates for active invoices with customer-facing debt."""
    filters = [
        Invoice.is_active.is_(True),
        Invoice.status.in_(OPEN_INVOICE_STATUSES),
        Invoice.balance_due > Decimal("0.00"),
    ]
    if account_id is not None:
        filters.insert(0, Invoice.account_id == coerce_uuid(str(account_id)))
    return tuple(filters)


def due_invoice_filters(account_id=None, *, now: datetime | None = None) -> tuple:
    """SQL predicates for open invoices that are due for collection now."""
    now = _as_aware(now)
    return (
        *open_invoice_filters(account_id),
        or_(
            Invoice.due_at <= now,
            and_(Invoice.due_at.is_(None), Invoice.status == InvoiceStatus.overdue),
        ),
    )


def overdue_debt_filters(account_id=None, *, now: datetime | None = None) -> tuple:
    """SQL predicates for debt that is overdue or should be treated as overdue."""
    now = _as_aware(now)
    filters = [
        Invoice.is_active.is_(True),
        Invoice.status.in_(OVERDUE_DEBT_STATUSES),
        Invoice.balance_due > Decimal("0.00"),
        or_(
            Invoice.status == InvoiceStatus.overdue,
            Invoice.due_at < now,
        ),
    ]
    if account_id is not None:
        filters.insert(0, Invoice.account_id == coerce_uuid(str(account_id)))
    return tuple(filters)


def overdue_status_filters(account_id=None) -> tuple:
    """SQL predicates for invoices already marked overdue."""
    filters = [
        Invoice.is_active.is_(True),
        Invoice.status == InvoiceStatus.overdue,
        Invoice.balance_due > Decimal("0.00"),
    ]
    if account_id is not None:
        filters.insert(0, Invoice.account_id == coerce_uuid(str(account_id)))
    return tuple(filters)


def list_open_invoices(
    db: Session, account_id, *, due_only: bool = False
) -> list[Invoice]:
    """Return open invoices for an account in deterministic collection order."""
    filters = (
        due_invoice_filters(account_id)
        if due_only
        else open_invoice_filters(account_id)
    )
    return list(
        db.scalars(
            select(Invoice)
            .where(*filters)
            .order_by(Invoice.due_at.asc().nullslast(), Invoice.created_at.asc())
        ).all()
    )


def invoice_balance_sum(db: Session, filters: Iterable) -> Decimal:
    total = db.execute(
        select(func.coalesce(func.sum(Invoice.balance_due), Decimal("0.00"))).where(
            *tuple(filters)
        )
    ).scalar()
    return Decimal(str(total or "0.00"))


def open_invoice_balance(db: Session, account_id) -> Decimal:
    return invoice_balance_sum(db, open_invoice_filters(account_id))


def due_invoice_balance(
    db: Session, account_id, *, now: datetime | None = None
) -> Decimal:
    return invoice_balance_sum(db, due_invoice_filters(account_id, now=now))


def overdue_debt_balance(
    db: Session, account_id, *, now: datetime | None = None
) -> Decimal:
    return invoice_balance_sum(db, overdue_debt_filters(account_id, now=now))


def overdue_status_count(db: Session, account_id) -> int:
    return int(
        db.execute(
            select(func.count(Invoice.id)).where(*overdue_status_filters(account_id))
        ).scalar()
        or 0
    )


def _as_aware(value: datetime | None) -> datetime:
    value = value or datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
