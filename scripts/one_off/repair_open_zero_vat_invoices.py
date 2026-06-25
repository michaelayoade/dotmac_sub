"""Repair open post-cutover invoices missing catalog VAT.

The script only touches active open invoices whose current tax total is zero.
Each line is re-evaluated through the same catalog/address/subscriber tax
resolver used by invoice generation; catalog-exempt lines stay exempt.

Dry-run by default.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal

from app.db import SessionLocal
from app.models.billing import Invoice, InvoiceLine, InvoiceStatus, TaxApplication
from app.models.catalog import Subscription
from app.services import billing_automation
from app.services.billing._common import (
    _calculate_tax_amount,
    _recalculate_invoice_totals,
)
from app.services.common import round_money

OPEN_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)


def _parse_since(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write invoice updates.")
    parser.add_argument(
        "--since",
        default="2026-06-16",
        help="Inclusive issue date lower bound, default 2026-06-16.",
    )
    args = parser.parse_args()

    since = _parse_since(args.since)
    db = SessionLocal()
    touched_invoices = 0
    touched_lines = 0
    projected_tax = Decimal("0.00")
    try:
        invoices = (
            db.query(Invoice)
            .filter(Invoice.is_active.is_(True))
            .filter(Invoice.status.in_(OPEN_STATUSES))
            .filter(Invoice.issued_at >= since)
            .filter(Invoice.tax_total == 0)
            .order_by(Invoice.issued_at.asc(), Invoice.invoice_number.asc())
            .all()
        )
        for invoice in invoices:
            taxable_lines: list[tuple[InvoiceLine, object]] = []
            for line in invoice.lines:
                if not line.is_active or line.tax_rate_id or not line.subscription_id:
                    continue
                subscription = db.get(Subscription, line.subscription_id)
                if subscription is None:
                    continue
                tax_rate_id = billing_automation._resolve_tax_rate_id(db, subscription)
                if not tax_rate_id:
                    continue
                taxable_lines.append((line, tax_rate_id))
                rate = db.get(billing_automation.TaxRate, tax_rate_id)
                if rate:
                    projected_tax += _calculate_tax_amount(
                        round_money(line.amount),
                        Decimal(str(rate.rate)),
                        billing_automation._default_tax_application(db),
                    )

            if not taxable_lines:
                continue
            touched_invoices += 1
            touched_lines += len(taxable_lines)
            if args.apply:
                for line, tax_rate_id in taxable_lines:
                    line.tax_rate_id = tax_rate_id
                    line.tax_application = billing_automation._default_tax_application(
                        db
                    )
                    if not line.tax_application:
                        line.tax_application = TaxApplication.exclusive
                _recalculate_invoice_totals(db, invoice)

        if args.apply:
            db.commit()

        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"open zero-VAT invoice repair — {mode}")
        print(f"since: {since.date().isoformat()}")
        print(f"candidate_invoices: {len(invoices)}")
        print(f"invoices_to_update: {touched_invoices}")
        print(f"lines_to_update: {touched_lines}")
        print(f"projected_added_tax: {round_money(projected_tax)}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
