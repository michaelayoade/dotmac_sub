"""Billing ledger page data — a projection over the billing SOT owners.

Consolidates the billing document inventory into one archetype-D ledger with a
facet per record type, each sourced from its owner in the billing service
package (billing_service.invoices / payments / credit_notes):
  - invoices: Invoice (owner: billing.invoices)
  - payments: Payment (owner: billing.payments)
  - credit_notes: CreditNote (owner: billing.credit_notes)

Status tone comes from the server-owned presentations (invoice / payment /
credit-note — all already defined). Amounts are formatted here as
currency-prefixed strings so the template needs no money filter. Read-only
projection; mutations stay on the billing action owners.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import billing as billing_service
from app.services.status_presentation import (
    credit_note_status_presentation,
    invoice_status_presentation,
    payment_status_presentation,
)

_LIMIT = 500

# facet order + labels
FACETS: tuple[tuple[str, str], ...] = (
    ("invoices", "Invoices"),
    ("payments", "Payments"),
    ("credit_notes", "Credit notes"),
)
_VALID = {key for key, _ in FACETS}


def _money(currency: object, amount: object) -> str:
    try:
        return f"{currency or 'NGN'} {float(amount or 0):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _dt(value: object) -> str:
    return value.strftime("%b %d, %Y") if value else "—"


def _invoice_rows(db: Session) -> tuple[list, list]:
    columns = [
        ("Number", "number"),
        ("Status", "__status"),
        ("Total", "total"),
        ("Balance", "balance"),
        ("Issued", "issued"),
    ]
    rows = [
        {
            "id": str(i.id),
            "number": i.invoice_number or str(i.id)[:8],
            "status": invoice_status_presentation(i.status),
            "total": _money(i.currency, i.total),
            "balance": _money(i.currency, i.balance_due),
            "issued": _dt(i.issued_at),
        }
        for i in billing_service.invoices.list(
            db, None, None, None, "created_at", "desc", _LIMIT, 0
        )
    ]
    return columns, rows


def _payment_rows(db: Session) -> tuple[list, list]:
    columns = [
        ("Reference", "ref"),
        ("Status", "__status"),
        ("Amount", "amount"),
        ("Received", "received"),
    ]
    rows = [
        {
            "id": str(p.id),
            "ref": p.external_id or str(p.id)[:8],
            "status": payment_status_presentation(p.status),
            "amount": _money(p.currency, p.amount),
            "received": _dt(p.created_at),
        }
        for p in billing_service.payments.list(
            db, None, None, None, None, "created_at", "desc", _LIMIT, 0
        )
    ]
    return columns, rows


def _credit_note_rows(db: Session) -> tuple[list, list]:
    columns = [
        ("Number", "number"),
        ("Status", "__status"),
        ("Total", "total"),
        ("Applied", "applied"),
        ("Issued", "issued"),
    ]
    rows = [
        {
            "id": str(c.id),
            "number": c.credit_number or str(c.id)[:8],
            "status": credit_note_status_presentation(c.status),
            "total": _money(c.currency, c.total),
            "applied": _money(c.currency, c.applied_total),
            "issued": _dt(c.issued_at),
        }
        for c in billing_service.credit_notes.list(
            db, None, None, None, None, "created_at", "desc", _LIMIT, 0
        )
    ]
    return columns, rows


_DISPATCH = {
    "invoices": _invoice_rows,
    "payments": _payment_rows,
    "credit_notes": _credit_note_rows,
}

_DETAIL_BASE = {
    "invoices": "/admin/billing/invoices/",
}


def billing_ledger_data(db: Session, facet: str = "invoices") -> dict:
    """Return the ledger page data for one billing facet (from its owner)."""
    facet = facet if facet in _VALID else "invoices"
    columns, rows = _DISPATCH[facet](db)
    return {
        "facet": facet,
        "facet_label": dict(FACETS)[facet],
        "facets": [{"key": k, "label": lbl} for k, lbl in FACETS],
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "detail_base": _DETAIL_BASE.get(facet, ""),
    }
