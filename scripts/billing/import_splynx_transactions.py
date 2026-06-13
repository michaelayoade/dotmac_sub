"""Import Splynx billing_transactions into the local mirror (parity + audit).

Faithfully copies Splynx's transaction ledger into
``splynx_billing_transactions`` so local financial history is at parity with
Splynx and each customer's deposit reconciles (deposit = Σcredit − Σdebit).
Read-only against Splynx; writes only the mirror table.

Idempotent (keyed on splynx_transaction_id — re-runs/resumes skip existing).
Batched for the ~232k rows. Dry-run by default.

Usage:
    python scripts/billing/import_splynx_transactions.py            # dry-run
    python scripts/billing/import_splynx_transactions.py --execute
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import inspect as sa_inspect

from app.db import SessionLocal
from app.models.splynx_transaction import SplynxBillingTransaction
from app.models.subscriber import Subscriber
from scripts.migration.db_connections import (
    fetch_all,
    fetch_batched,
    splynx_connection,
)

BATCH = 2000


def _d(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v)
    if not s or s.startswith("0000"):
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _entry_type(v) -> str:
    t = (v or "").strip().lower()
    return t if t in ("credit", "debit") else "other"


def _int(v) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n or None  # treat 0 as "none" (Splynx uses 0 for absent FK)


def main(execute: bool) -> None:
    db = SessionLocal()
    try:
        # Ensure the mirror table exists (matches migration 150; safe if present).
        bind = db.get_bind()
        if not sa_inspect(bind).has_table(SplynxBillingTransaction.__tablename__):
            SplynxBillingTransaction.__table__.create(bind=bind)  # type: ignore[attr-defined]
        mapper = sa_inspect(SplynxBillingTransaction)

        sub_map = {
            int(cid): sid
            for cid, sid in db.query(
                Subscriber.splynx_customer_id, Subscriber.id
            ).filter(Subscriber.splynx_customer_id.isnot(None))
        }
        existing = {
            r[0] for r in db.query(SplynxBillingTransaction.splynx_transaction_id).all()
        }
        print(f"local subscribers (splynx-linked): {len(sub_map)}")
        print(f"already imported transactions    : {len(existing)}")

        with splynx_connection() as conn:
            cats = {
                int(r["id"]): r["name"]
                for r in fetch_all(
                    conn, "SELECT id, name FROM billing_transactions_categories"
                )
            }

            scanned = inserted = skipped = unlinked = 0
            pending: list[dict] = []
            for batch in fetch_batched(
                conn,
                "SELECT id, customer_id, type, total, category, description, date, "
                "period_from, period_to, invoice_id, payment_id, credit_note_id, "
                "service_id, service_type, source, deleted FROM billing_transactions",
                batch_size=BATCH,
            ):
                for r in batch:
                    scanned += 1
                    tid = int(r["id"])
                    if tid in existing:
                        skipped += 1
                        continue
                    cid = int(r["customer_id"])
                    sid = sub_map.get(cid)
                    if sid is None:
                        unlinked += 1
                    now = datetime.now(UTC)
                    pending.append(
                        {
                            "id": uuid.uuid4(),
                            "splynx_transaction_id": tid,
                            "splynx_customer_id": cid,
                            "subscriber_id": sid,
                            # credit/debit drive the balance; Splynx's rare
                            # empty-type rows are NOT in its deposit calc, so map
                            # them to 'other' (excluded from reconciliation), never
                            # silently to debit.
                            "entry_type": _entry_type(r["type"]),
                            "amount": Decimal(str(r["total"] or "0")),
                            "category_id": _int(r["category"]),
                            "category_name": cats.get(_int(r["category"]) or -1),
                            "description": (r["description"] or None),
                            "transaction_date": _d(r["date"]),
                            "period_from": _d(r["period_from"]),
                            "period_to": _d(r["period_to"]),
                            "splynx_invoice_id": _int(r["invoice_id"]),
                            "splynx_payment_id": _int(r["payment_id"]),
                            "splynx_credit_note_id": _int(r["credit_note_id"]),
                            "service_id": _int(r["service_id"]),
                            "service_type": (r["service_type"] or None),
                            "source": (r["source"] or None),
                            "deleted": str(r["deleted"]) == "1",
                            "created_at": now,
                            "updated_at": now,
                        }
                    )
                    inserted += 1
                    if execute and len(pending) >= BATCH:
                        db.bulk_insert_mappings(mapper, pending)
                        db.commit()
                        pending.clear()
            if execute and pending:
                db.bulk_insert_mappings(mapper, pending)
                db.commit()

        print("\n=== Splynx billing_transactions import ===")
        print(f"scanned   : {scanned}")
        print(f"to import : {inserted}  (unlinked to a local subscriber: {unlinked})")
        print(f"skipped (already present): {skipped}")
        if not execute:
            print("\nDRY-RUN — nothing written. Re-run with --execute to import.")
            return
        print(f"\nDONE — imported {inserted} transactions into the mirror.")
    finally:
        db.close()


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)
