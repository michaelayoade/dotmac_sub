"""Write off obvious migration-cruft arrears on terminated-service accounts.

Context: the local-billing cutover left ~NGN101M of imported open invoices
sitting on accounts whose services are all terminal (disabled/canceled/etc.).
The live-service billing gate (``billing_settings.LIVE_SERVICE_STATUSES``) now
stops further reminders/escalations/autopay on them, but the balances still sit
in AR. A finance review of the bulk (~₦97.7M, enterprise/government) is exported
separately for manual reconciliation and is LEFT UNTOUCHED here.

This CLI writes off ONLY the obvious-cruft slice — three narrow buckets:
  * ``test_account``    — account name contains "test" (QA/staff test accounts)
  * ``no_subscription`` — account has no subscription record at all
  * ``old_tiny``        — invoice issued before 2024 with balance < ₦50,000

Each is written off via the canonical ``billing.invoices.write_off`` (a credit
adjustment ledger entry carrying the audit memo + reason, balance → 0, status →
void; the invoice record is preserved). Reason code:
``splynx_migration_terminal_account_cleanup``.

Dry-run by default; nothing is written without --apply. An --apply run aborts if
the selected count/total exceed the safety caps (guards against selection drift).

Examples
--------
  # Read-only: list exactly what would be written off
  python -m scripts.one_off.writeoff_terminal_arrears_cruft

  # Apply the write-offs (auditable, reversible via the ledger entry)
  python -m scripts.one_off.writeoff_terminal_arrears_cruft --apply
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from sqlalchemy import text

from app.db import SessionLocal
from app.services.billing import invoices as invoice_service

REASON = "splynx_migration_terminal_account_cleanup"

# Safety caps: an --apply run that would exceed either is refused, so a future
# change to the classification can't silently void far more than intended.
MAX_COUNT = 30
MAX_AMOUNT = Decimal("5000000.00")

SELECT_CRUFT = text(
    """
    WITH live AS (
        SELECT DISTINCT subscriber_id FROM subscriptions
        WHERE status IN ('active','suspended','pending')
    ),
    pop AS (
        SELECT i.id, i.account_id, i.invoice_number, i.balance_due, i.splynx_invoice_id,
               i.issued_at, i.billing_period_start, i.created_at
        FROM invoices i
        WHERE i.is_active
          AND i.status IN ('issued','partially_paid','overdue')
          AND i.balance_due > 0
          AND i.account_id NOT IN (SELECT subscriber_id FROM live)
    ),
    sub AS (
        SELECT DISTINCT ON (subscriber_id) subscriber_id, status
        FROM subscriptions ORDER BY subscriber_id, updated_at DESC
    )
    SELECT p.id,
           p.invoice_number,
           p.balance_due,
           p.splynx_invoice_id,
           COALESCE(s.company_name, s.display_name,
                    trim(s.first_name || ' ' || s.last_name)) AS name,
           COALESCE(sub.status::text, 'no_subscription') AS svc_status,
           CASE
               WHEN lower(COALESCE(s.company_name, s.display_name,
                          s.first_name || s.last_name, '')) LIKE '%test%'
                   THEN 'test_account'
               WHEN sub.subscriber_id IS NULL
                   THEN 'no_subscription'
               WHEN COALESCE(p.issued_at, p.billing_period_start, p.created_at) < '2024-01-01'
                    AND p.balance_due < 50000
                   THEN 'old_tiny'
               ELSE NULL
           END AS bucket
    FROM pop p
    JOIN subscribers s ON s.id = p.account_id
    LEFT JOIN sub ON sub.subscriber_id = p.account_id
    ORDER BY p.balance_due DESC
    """
)


def _memo(bucket: str, svc_status: str, splynx_id) -> str:
    src = f"splynx_invoice_id={splynx_id}" if splynx_id is not None else "native"
    return (
        f"{REASON} | bucket={bucket} | {src} | "
        f"Imported invoice on {svc_status} terminal account; "
        f"confirmed {bucket} migration cruft; excluded from collectible AR"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Write off (default: dry-run)"
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        rows = [r for r in db.execute(SELECT_CRUFT).mappings().all() if r["bucket"]]
        total = sum((r["balance_due"] for r in rows), Decimal("0.00"))

        print(f"\nReason code : {REASON}")
        print(f"Mode        : {'APPLY' if args.apply else 'DRY-RUN'}")
        print(f"Invoices    : {len(rows)}")
        print(f"Total amount: NGN {total:,.2f}\n")
        by_bucket: dict[str, list] = {}
        for r in rows:
            by_bucket.setdefault(r["bucket"], []).append(r)
        for bucket, items in sorted(by_bucket.items()):
            bt = sum((r["balance_due"] for r in items), Decimal("0.00"))
            print(f"  [{bucket}] {len(items)} invoices, NGN {bt:,.2f}")
            for r in items:
                print(
                    f"    - {r['invoice_number'] or '(no #)':<16} "
                    f"NGN {r['balance_due']:>14,.2f}  {r['svc_status']:<10} "
                    f"{(r['name'] or '?')[:34]}"
                )
        print()

        if not args.apply:
            print("Dry-run only. Re-run with --apply to write off.\n")
            return

        if len(rows) > MAX_COUNT or total > MAX_AMOUNT:
            raise SystemExit(
                f"ABORT: selection ({len(rows)} invoices / NGN {total:,.2f}) "
                f"exceeds safety caps ({MAX_COUNT} / NGN {MAX_AMOUNT:,.2f}). "
                "Refusing to apply — review the classification."
            )

        done = Decimal("0.00")
        for r in rows:
            invoice_service.write_off(
                db,
                str(r["id"]),
                memo=_memo(r["bucket"], r["svc_status"], r["splynx_invoice_id"]),
            )
            done += r["balance_due"]
            print(
                f"  written off {r['invoice_number'] or r['id']}  NGN {r['balance_due']:,.2f}"
            )
        print(f"\nDONE: wrote off {len(rows)} invoices, NGN {done:,.2f}\n")
    finally:
        db.close()


if __name__ == "__main__":
    main()
