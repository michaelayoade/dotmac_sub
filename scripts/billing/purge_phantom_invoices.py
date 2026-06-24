"""Hard-delete the billing-runner PHANTOM invoices (and their child rows).

Background: the local billing runner once double-billed ~₦692M of invoices that
never existed in the authoritative external biller. PR #207/#249 VOIDED them,
but the user wants the faulty rows physically removed so they cannot surface in
any UI or report.

The phantom set is tightly scoped and *never* touches migrated external data:

    is_active = true  AND  splynx_invoice_id IS NULL  AND  status = 'void'

Invoices that have real financial entanglement (a payment_allocation or a
ledger_entry — e.g. a local succeeded/refunded payment was applied to one) are
EXCLUDED and left voided for individual review; deleting them would un-apply
real money. Children are removed first to satisfy FK constraints.

Always backs up every targeted row to CSV before deleting. Dry-run by default.

Usage:
    python scripts/billing/purge_phantom_invoices.py            # dry-run + backup
    python scripts/billing/purge_phantom_invoices.py --execute  # backup + delete
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from sqlalchemy import text

from app.db import SessionLocal

# Phantom = locally-generated (no external invoice id), already voided, still active.
PHANTOM = "is_active = true AND splynx_invoice_id IS NULL AND status = 'void'"
# Entangled = referenced by a real payment or ledger entry; do NOT delete.
NOT_ENTANGLED = (
    "id NOT IN (SELECT invoice_id FROM payment_allocations WHERE invoice_id IS NOT NULL) "
    "AND id NOT IN (SELECT invoice_id FROM ledger_entries WHERE invoice_id IS NOT NULL)"
)
CLEAN_IDS = f"SELECT id FROM invoices WHERE {PHANTOM} AND {NOT_ENTANGLED}"

# Under /app/uploads (host-mounted ./uploads) so the backup survives the
# container — /app/backups would be ephemeral container storage.
BACKUP_DIR = Path("/app/uploads/phantom_invoice_purge_2026-06-15")


def _dump(db, name: str, query: str) -> int:
    """COPY a query result to a CSV file; return row count."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    path = BACKUP_DIR / f"{name}.csv"
    rows = db.execute(text(query)).fetchall()
    if rows:
        cols = list(rows[0]._mapping.keys())
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([r._mapping[c] for c in cols])
    return len(rows)


def main(execute: bool) -> None:
    db = SessionLocal()
    try:
        n_clean = db.execute(
            text(f"SELECT count(*) FROM invoices WHERE {PHANTOM} AND {NOT_ENTANGLED}")
        ).scalar()
        n_phantom = db.execute(
            text(f"SELECT count(*) FROM invoices WHERE {PHANTOM}")
        ).scalar()
        n_lines = db.execute(
            text(
                f"SELECT count(*) FROM invoice_lines WHERE invoice_id IN ({CLEAN_IDS})"
            )
        ).scalar()
        n_pdf = db.execute(
            text(
                f"SELECT count(*) FROM invoice_pdf_exports WHERE invoice_id IN ({CLEAN_IDS})"
            )
        ).scalar()
        print(f"phantom invoices total            : {n_phantom}")
        print(f"  excluded (entangled, kept void) : {n_phantom - n_clean}")
        print(f"CLEAN invoices to delete          : {n_clean}")
        print(f"  child invoice_lines             : {n_lines}")
        print(f"  child invoice_pdf_exports       : {n_pdf}")

        # Always back up the exact rows that will be deleted (full row images).
        print(f"\nBacking up to {BACKUP_DIR} ...")
        b_inv = _dump(
            db,
            "invoices",
            f"SELECT * FROM invoices WHERE {PHANTOM} AND {NOT_ENTANGLED}",
        )
        b_lines = _dump(
            db,
            "invoice_lines",
            f"SELECT * FROM invoice_lines WHERE invoice_id IN ({CLEAN_IDS})",
        )
        b_pdf = _dump(
            db,
            "invoice_pdf_exports",
            f"SELECT * FROM invoice_pdf_exports WHERE invoice_id IN ({CLEAN_IDS})",
        )
        # Also archive the 4 entangled invoices for the record (not deleted).
        b_ent = _dump(
            db,
            "entangled_kept",
            f"SELECT * FROM invoices WHERE {PHANTOM} AND NOT ({NOT_ENTANGLED})",
        )
        print(
            f"  backed up: {b_inv} invoices, {b_lines} lines, {b_pdf} pdf_exports, "
            f"{b_ent} entangled (kept)"
        )

        if not execute:
            print("\nDRY-RUN — backup written, nothing deleted. Re-run with --execute.")
            return

        # The app session carries a short statement_timeout; deleting an invoice
        # forces a per-row FK lock-check against ledger_entries (no index on its
        # invoice_id), which is slow at this volume. Disable the timeout for this
        # one-off maintenance session and delete in committed batches by id so a
        # single statement never runs unbounded.
        db.execute(text("SET statement_timeout = 0"))
        ids = [r[0] for r in db.execute(text(CLEAN_IDS)).fetchall()]
        print(f"\nDeleting {len(ids)} invoices in batches of 500 ...")

        d_pdf = db.execute(
            text(f"DELETE FROM invoice_pdf_exports WHERE invoice_id IN ({CLEAN_IDS})")
        ).rowcount
        d_lines = db.execute(
            text(f"DELETE FROM invoice_lines WHERE invoice_id IN ({CLEAN_IDS})")
        ).rowcount
        db.commit()

        d_inv = 0
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            d_inv += db.execute(
                text("DELETE FROM invoices WHERE id = ANY(:ids)"), {"ids": chunk}
            ).rowcount
            db.commit()
            print(f"  deleted {d_inv}/{len(ids)}", flush=True)
        print(
            f"\nDONE — deleted {d_inv} phantom invoices "
            f"({d_lines} lines, {d_pdf} pdf_exports). "
            f"{n_phantom - n_clean} entangled invoices kept (voided)."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)
