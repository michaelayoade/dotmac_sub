"""Clear the phantom-era local billing notifications.

The local billing runner that produced the ~₦692M phantom invoices also queued
local dunning/invoice notifications against that phantom-polluted AR
(``invoice_overdue`` with a broken ``#{}`` invoice reference, plus a handful of
``invoice_created`` / ``invoice_sent``). During the legacy dual-run the local
app was not the biller and must not dun customers — the external biller sent the real
notices — so every one of these is a phantom-era artifact.

Scope is tight and verified non-destructive to customers: only the three local
billing event types, none of which were ever delivered (all queued/canceled).
Backs up to CSV first, then deletes. Dry-run by default.

Usage:
    python scripts/billing/purge_phantom_notifications.py            # dry-run + backup
    python scripts/billing/purge_phantom_notifications.py --execute  # backup + delete
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from sqlalchemy import text

from app.db import SessionLocal

EVENT_TYPES = ("invoice_overdue", "invoice_created", "invoice_sent")
# Definitively phantom: a local billing notice for a subscriber who has NO real
# open AR. Subscribers who still owe a live imported balance are EXCLUDED
# — their notice could reference real debt, so it's left for manual review.
_HAS_REAL_AR = (
    "subscriber_id IN (SELECT account_id FROM invoices WHERE is_active = true "
    "AND status IN ('issued', 'overdue', 'partially_paid'))"
)
TARGET = (
    "event_type IN ('invoice_overdue', 'invoice_created', 'invoice_sent') "
    f"AND NOT ({_HAS_REAL_AR})"
)
BACKUP_DIR = Path("/app/uploads/phantom_invoice_purge_2026-06-15")


def _dump(db, name: str, query: str) -> int:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    rows = db.execute(text(query)).fetchall()
    if rows:
        cols = list(rows[0]._mapping.keys())
        with (BACKUP_DIR / f"{name}.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([r._mapping[c] for c in cols])
    return len(rows)


def main(execute: bool) -> None:
    db = SessionLocal()
    try:
        total = db.execute(
            text(f"SELECT count(*) FROM notifications WHERE {TARGET}")
        ).scalar()
        delivered = db.execute(
            text(
                f"SELECT count(*) FROM notifications WHERE {TARGET} AND status = 'delivered'"
            )
        ).scalar()
        print(f"phantom-era billing notifications: {total}")
        print(f"  ever delivered (NOT touched)    : {delivered}")
        # Safety rail: never delete anything that actually reached a customer.
        scope = f"{TARGET} AND status <> 'delivered'"
        to_delete = db.execute(
            text(f"SELECT count(*) FROM notifications WHERE {scope}")
        ).scalar()
        print(f"  to delete (queued/canceled)     : {to_delete}")

        print(f"\nBacking up to {BACKUP_DIR} ...")
        n = _dump(
            db, "phantom_notifications", f"SELECT * FROM notifications WHERE {scope}"
        )
        print(f"  backed up {n} notifications")

        if not execute:
            print("\nDRY-RUN — backup written, nothing deleted. Re-run with --execute.")
            return

        # No FK children exist for this set (verified), but delete defensively.
        db.execute(
            text(
                f"DELETE FROM notification_deliveries WHERE notification_id IN "
                f"(SELECT id FROM notifications WHERE {scope})"
            )
        )
        db.execute(
            text(
                f"DELETE FROM alert_notification_logs WHERE notification_id IN "
                f"(SELECT id FROM notifications WHERE {scope})"
            )
        )
        deleted = db.execute(text(f"DELETE FROM notifications WHERE {scope}")).rowcount
        db.commit()
        print(f"\nDONE — deleted {deleted} phantom-era billing notifications.")
    finally:
        db.close()


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)
