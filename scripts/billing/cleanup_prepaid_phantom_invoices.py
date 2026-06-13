"""Void phantom invoices wrongly generated for prepaid accounts.

Prepaid customers draw down a deposit and (in the authoritative Splynx
model) never receive invoices. The local runner generated postpaid-style,
duplicate, UNNUMBERED invoices for them — inflating "amount due" and dragging
prepaid available balance (credit − open invoices) negative, which feeds
wrongful dunning/prepaid enforcement.

This voids open, locally-generated (invoice_number IS NULL) invoices on
prepaid accounts. It is:
  - dry-run by default (--execute to act)
  - idempotent (only touches issued/overdue/partially_paid)
  - non-destructive (status -> void, balance_due -> 0, audit marker in
    metadata; rows are kept, not deleted)
  - interlocked: refuses to --execute while billing_enabled is True, so it
    can never race the live runner regenerating what it voids.

Usage:
    python scripts/billing/cleanup_prepaid_phantom_invoices.py            # dry-run
    python scripts/billing/cleanup_prepaid_phantom_invoices.py --execute
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal

from app.db import SessionLocal
from app.models.billing import Invoice, InvoiceStatus
from app.models.subscriber import Subscriber
from app.services.billing_settings import billing_enabled

_OPEN = (InvoiceStatus.issued, InvoiceStatus.overdue, InvoiceStatus.partially_paid)


def _candidates(db):
    return (
        db.query(Invoice)
        .join(Subscriber, Subscriber.id == Invoice.account_id)
        .filter(
            Subscriber.billing_mode == "prepaid",
            Subscriber.is_active.is_(True),
            Invoice.is_active.is_(True),
            Invoice.status.in_(_OPEN),
            Invoice.invoice_number.is_(None),
        )
    )


def main(execute: bool) -> None:
    db = SessionLocal()
    try:
        gate = billing_enabled(db)
        q = _candidates(db)
        rows = q.all()
        subs = {r.account_id for r in rows}
        total = sum((r.balance_due or Decimal("0")) for r in rows)

        # Numbered open invoices on prepaid accounts — NOT touched; reported
        # so we can see whether any "real" issued invoices exist for prepaid.
        numbered = (
            db.query(Invoice)
            .join(Subscriber, Subscriber.id == Invoice.account_id)
            .filter(
                Subscriber.billing_mode == "prepaid",
                Subscriber.is_active.is_(True),
                Invoice.is_active.is_(True),
                Invoice.status.in_(_OPEN),
                Invoice.invoice_number.isnot(None),
            )
            .count()
        )

        print("=== prepaid phantom-invoice cleanup ===")
        print(f"billing_enabled (master gate) : {gate}")
        print(f"prepaid subscribers affected  : {len(subs)}")
        print(f"unnumbered open invoices      : {len(rows)}  (VOID candidates)")
        print(f"  total phantom balance_due   : NGN {total:,.2f}")
        print(f"numbered open invoices        : {numbered}  (LEFT ALONE)")

        if not execute:
            print("\nDRY-RUN — nothing changed. Re-run with --execute to void.")
            return

        if gate:
            print(
                "\nREFUSED: billing_enabled is True. Set it False first so the "
                "runner cannot regenerate voided invoices. Aborting."
            )
            sys.exit(2)

        now = datetime.now(UTC)
        voided = 0
        for inv in rows:
            meta = dict(inv.metadata_ or {}) if hasattr(inv, "metadata_") else {}
            meta["voided_by"] = "prepaid_phantom_cleanup"
            meta["voided_at"] = now.isoformat()
            meta["void_prior_status"] = inv.status.value
            meta["void_prior_balance_due"] = str(inv.balance_due)
            if hasattr(inv, "metadata_"):
                inv.metadata_ = meta
            inv.status = InvoiceStatus.void
            inv.balance_due = Decimal("0.00")
            voided += 1
            if voided % 500 == 0:
                db.commit()
                print(f"  ...voided {voided}")
        db.commit()
        print(f"\nDONE — voided {voided} phantom invoices across {len(subs)} subs.")
    finally:
        db.close()


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)
