"""Bring local postpaid invoices to parity with Splynx (the authoritative biller).

For every active local invoice on a postpaid, Splynx-linked subscriber, compare
its status to the matching Splynx invoice (by invoice number) and align local
to Splynx:

  - Splynx 'paid'   + local open      -> mark local paid (balance_due 0)   [stale]
  - Splynx deleted  + local active    -> void local (balance_due 0)        [orphan]
  - Splynx 'not_paid'+ local open      -> leave (already parity)
  - other combinations                 -> reported, NOT changed (needs review)

Also REPORTS (does not change) two parity gaps for awareness:
  - local invoices marked paid but Splynx 'not_paid' (reverse drift)
  - Splynx 'not_paid' invoices with no local counterpart (missing import)

Only ever writes to the LOCAL app; never to Splynx. Dry-run by default;
non-destructive (status change + audit marker in metadata, rows kept).

Usage:
    python scripts/billing/reconcile_postpaid_invoices.py            # dry-run
    python scripts/billing/reconcile_postpaid_invoices.py --execute
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal

from app.db import SessionLocal
from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import BillingMode
from app.models.subscriber import Subscriber
from scripts.migration.db_connections import fetch_all, splynx_connection

_OPEN = (InvoiceStatus.issued, InvoiceStatus.overdue, InvoiceStatus.partially_paid)


def main(execute: bool) -> None:
    db = SessionLocal()
    try:
        pp = (
            db.query(Subscriber)
            .filter(Subscriber.billing_mode == BillingMode.postpaid)
            .filter(Subscriber.splynx_customer_id.isnot(None))
            .all()
        )
        sub_ids = [s.id for s in pp]
        cids = [int(s.splynx_customer_id) for s in pp]
        print(f"postpaid splynx-linked subscribers: {len(pp)}")

        local = (
            db.query(Invoice)
            .filter(Invoice.account_id.in_(sub_ids))
            .filter(Invoice.is_active.is_(True))
            .filter(Invoice.invoice_number.isnot(None))
            .all()
        )

        # Splynx invoice status by number + all Splynx not_paid numbers.
        nums = sorted({i.invoice_number for i in local})
        sx: dict[str, dict] = {}
        splynx_notpaid_nums: set[str] = set()
        if nums:
            ph = ",".join(["%s"] * len(nums))
            with splynx_connection() as c:
                for r in fetch_all(
                    c,
                    f"SELECT number, status, deleted FROM invoices WHERE number IN ({ph})",  # noqa: S608
                    tuple(nums),
                ):
                    sx[str(r["number"])] = r
            cph = ",".join(["%s"] * len(cids))
            with splynx_connection() as c:
                for r in fetch_all(
                    c,
                    "SELECT number FROM invoices WHERE customer_id IN "  # noqa: S608
                    f"({cph}) AND deleted='0' AND status='not_paid'",
                    tuple(cids),
                ):
                    splynx_notpaid_nums.add(str(r["number"]))

        to_pay: list[Invoice] = []  # stale: splynx paid, local open
        to_void: list[Invoice] = []  # orphan: splynx deleted, local open
        reverse_drift = []  # local paid, splynx not_paid (report only)
        other = Counter()
        for inv in local:
            s = sx.get(str(inv.invoice_number))
            is_open = inv.status in _OPEN
            if s is None:
                if is_open:
                    other["local_open_not_in_splynx"] += 1
                continue
            if s["deleted"] == "1":
                if is_open:
                    to_void.append(inv)
                continue
            st = s["status"]
            if st == "paid" and is_open:
                to_pay.append(inv)
            elif st == "not_paid" and inv.status == InvoiceStatus.paid:
                reverse_drift.append(inv)
            elif st == "not_paid" and is_open:
                other["parity_ok_not_paid"] += 1

        local_nums = {str(i.invoice_number) for i in local}
        missing = splynx_notpaid_nums - local_nums

        print("\n=== local→Splynx invoice parity (postpaid) ===")
        print(
            f"STALE  (Splynx paid, local open)   : {len(to_pay)}  "
            f"NGN {sum((i.balance_due or Decimal(0)) for i in to_pay):,.2f}  -> mark paid"
        )
        print(
            f"ORPHAN (Splynx deleted, local open): {len(to_void)}  "
            f"NGN {sum((i.balance_due or Decimal(0)) for i in to_void):,.2f}  -> void"
        )
        print(f"already parity (not_paid both)     : {other['parity_ok_not_paid']}")
        print("\n--- report only (NOT changed) ---")
        print(f"reverse drift (local paid, Splynx not_paid): {len(reverse_drift)}")
        print(
            f"local open, not in Splynx at all           : {other['local_open_not_in_splynx']}"
        )
        print(f"Splynx not_paid missing locally (import gap): {len(missing)}")

        if not execute:
            print("\nDRY-RUN — nothing changed. Re-run with --execute to apply.")
            return

        now = datetime.now(UTC)
        n_paid = n_void = 0
        for inv in to_pay:
            meta = dict(inv.metadata_ or {}) if hasattr(inv, "metadata_") else {}
            meta["splynx_parity"] = "marked_paid"
            meta["splynx_parity_at"] = now.isoformat()
            meta["prior_status"] = inv.status.value
            meta["prior_balance_due"] = str(inv.balance_due)
            if hasattr(inv, "metadata_"):
                inv.metadata_ = meta
            inv.status = InvoiceStatus.paid
            inv.balance_due = Decimal("0.00")
            n_paid += 1
        for inv in to_void:
            meta = dict(inv.metadata_ or {}) if hasattr(inv, "metadata_") else {}
            meta["splynx_parity"] = "voided_deleted_in_splynx"
            meta["splynx_parity_at"] = now.isoformat()
            meta["prior_status"] = inv.status.value
            meta["prior_balance_due"] = str(inv.balance_due)
            if hasattr(inv, "metadata_"):
                inv.metadata_ = meta
            inv.status = InvoiceStatus.void
            inv.balance_due = Decimal("0.00")
            n_void += 1
            if (n_paid + n_void) % 200 == 0:
                db.commit()
        db.commit()
        print(
            f"\nDONE — marked {n_paid} paid, voided {n_void} (local now matches Splynx)."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)
