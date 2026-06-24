"""Backfill ledger_entries.effective_date with each entry's real-world date.

The migrated AR ledger lost original transaction dates: every row carries the
2026-03-15 import instant in created_at. This resolves a best-effort
effective_date per entry, in strict priority order:

  1. invoice_id FK present  -> invoices.issued_at          (exact)
  2. payment_id FK present   -> payments.paid_at           (exact)
  3. migrated + unlinked     -> positional match to the legacy mirror
        (full migrated-ledger sequence zipped against the full
         historical billing transaction sequence per account, by import order vs
         transaction_date order). For a matched row we prefer the LOCAL
         invoice.issued_at / payment.paid_at reached via the legacy row's
         imported invoice/payment ids (so the ledger date matches the
         invoice view), falling back to the legacy transaction_date itself.
        The fill is GATED on entry_type+amount agreement at the matched
        position; non-aligned positions are left NULL.
  4. everything else (native post-cutover, adjustment seeds, unmatched) -> NULL

NULL is safe: display/order use COALESCE(effective_date, created_at), so a NULL
row keeps today's behaviour. Adjustment cutover seeds and native entries are
intentionally left NULL — their created_at already IS the real date.

Date provenance (for the finance question that will eventually come up): for the
~123k unlinked migrated debits the date is the legacy mirror's transaction_date,
NOT the local invoice's issued_at. Both fields originate from import data at cutover
and agree to the day for ~98% of rows (the rest differ by ≤1 day), so a ledger
debit's date can legitimately differ from its invoice's issued_at by a day —
they are two distinct imported source fields. We deliberately do NOT
positionally pin debits to invoices: flat-rate recurring plans produce many
identical-amount debits whose only timestamp is the shared import created_at, so
a positional match would confidently assign the wrong month. Independent-but-
right beats consistent-but-wrong.

Dry-run by default (prints a coverage waterfall, writes nothing). Idempotent:
re-running recomputes and only updates rows whose value changed.

Usage:
    python scripts/billing/backfill_ledger_effective_date.py            # dry-run
    python scripts/billing/backfill_ledger_effective_date.py --execute
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, text

# Anything imported on/before this date is a migrated row; later rows are native
# (their created_at is the real date and must stay NULL -> created_at).
_CUTOVER = "2026-03-16"

# Staging view of the resolved date per ledger id. Built as a temp table inside
# one transaction; dry-run rolls back, --execute commits the UPDATE.
_BUILD = f"""
CREATE TEMP TABLE _eff (id uuid PRIMARY KEY, effective_date timestamptz, tier text)
ON COMMIT DROP;

-- Tier 1: invoice_id FK -> invoices.issued_at
INSERT INTO _eff (id, effective_date, tier)
SELECT l.id, i.issued_at, '1_invoice_fk'
FROM ledger_entries l
JOIN invoices i ON i.id = l.invoice_id
WHERE l.invoice_id IS NOT NULL AND i.issued_at IS NOT NULL;

-- Tier 2: payment_id FK -> payments.paid_at (rows not already resolved)
INSERT INTO _eff (id, effective_date, tier)
SELECT l.id, p.paid_at, '2_payment_fk'
FROM ledger_entries l
JOIN payments p ON p.id = l.payment_id
LEFT JOIN _eff e ON e.id = l.id
WHERE e.id IS NULL AND l.payment_id IS NOT NULL AND p.paid_at IS NOT NULL;

-- Tier 3: positional match of the full migrated ledger sequence against the
-- full legacy transaction sequence, per account. Resolve only unresolved,
-- migrated rows, and only where entry_type+amount agree at the matched index.
WITH led AS (
    SELECT l.id, l.account_id, l.entry_type::text AS et, l.amount,
           row_number() OVER (PARTITION BY l.account_id
                              ORDER BY l.created_at, l.id) AS rn
    FROM ledger_entries l
    WHERE l.source <> 'adjustment'
      AND l.created_at::date <= DATE '{_CUTOVER}'
),
spl AS (
    SELECT s.subscriber_id, s.entry_type AS et, s.amount,
           s.transaction_date, s.splynx_invoice_id, s.splynx_payment_id,
           row_number() OVER (PARTITION BY s.subscriber_id
                              ORDER BY s.transaction_date,
                                       s.splynx_transaction_id) AS rn
    FROM splynx_billing_transactions s
    WHERE s.subscriber_id IS NOT NULL
),
matched AS (
    SELECT led.id,
           spl.transaction_date,
           spl.splynx_invoice_id,
           spl.splynx_payment_id,
           (led.et = spl.et AND led.amount = spl.amount) AS aligned
    FROM led
    JOIN spl ON spl.subscriber_id = led.account_id AND spl.rn = led.rn
)
INSERT INTO _eff (id, effective_date, tier)
SELECT m.id,
       -- prefer local invoice/payment date reached via the matched imported ids,
       -- else the imported transaction_date.
       COALESCE(
           (i.issued_at)::timestamptz,
           (p.paid_at)::timestamptz,
           (m.transaction_date)::timestamptz
       ),
       CASE
           WHEN i.issued_at IS NOT NULL THEN '3_splynx->local_invoice'
           WHEN p.paid_at IS NOT NULL THEN '3_splynx->local_payment'
           ELSE '3_splynx_txn_date'
       END
FROM matched m
LEFT JOIN _eff e ON e.id = m.id
LEFT JOIN invoices i ON i.splynx_invoice_id = m.splynx_invoice_id
                    AND i.is_active AND m.splynx_invoice_id IS NOT NULL
LEFT JOIN payments p ON p.splynx_payment_id = m.splynx_payment_id
                    AND m.splynx_payment_id IS NOT NULL
WHERE e.id IS NULL AND m.aligned;

-- Plausibility guard: drop resolved dates outside [2015-01-01, now] (corrupt
-- source years, e.g. a '0202' typo) so they fall back to created_at, not a
-- garbage date.
UPDATE _eff SET effective_date = NULL
WHERE effective_date IS NOT NULL
  AND (effective_date < DATE '2015-01-01' OR effective_date > now());
"""

_REPORT = """
SELECT
  (SELECT count(*) FROM ledger_entries) AS ledger_total,
  (SELECT count(*) FROM _eff WHERE effective_date IS NOT NULL) AS resolved,
  (SELECT count(*) FROM ledger_entries) -
    (SELECT count(*) FROM _eff WHERE effective_date IS NOT NULL) AS left_null;
"""

_BY_TIER = """
SELECT tier, count(*) AS rows,
       min(effective_date)::date AS earliest,
       max(effective_date)::date AS latest
FROM _eff WHERE effective_date IS NOT NULL
GROUP BY tier ORDER BY tier;
"""

# Of the migrated, unlinked, non-adjustment rows (tier-3 candidates), how many
# stay NULL because the positional match did not align on type+amount.
_UNMATCHED = f"""
SELECT count(*) AS tier3_candidates_left_null
FROM ledger_entries l
LEFT JOIN _eff e ON e.id = l.id
WHERE l.source <> 'adjustment'
  AND l.created_at::date <= DATE '{_CUTOVER}'
  AND l.invoice_id IS NULL AND l.payment_id IS NULL
  AND e.id IS NULL;
"""


def main(execute: bool) -> None:
    url = os.environ["DATABASE_URL"]
    engine = create_engine(url)
    with engine.begin() as conn:
        for stmt in _BUILD.strip().split(";\n"):
            if stmt.strip():
                conn.execute(text(stmt))

        tot = conn.execute(text(_REPORT)).one()
        print("=== ledger effective_date backfill (dry-run) ===")
        print(f"ledger rows total : {tot.ledger_total:,}")
        print(f"resolved          : {tot.resolved:,}")
        print(f"left NULL (-> created_at fallback) : {tot.left_null:,}")
        print("\nby tier:")
        for r in conn.execute(text(_BY_TIER)):
            print(f"  {r.tier:28s} {r.rows:>8,}   [{r.earliest} .. {r.latest}]")
        u = conn.execute(text(_UNMATCHED)).one()
        print(
            f"\ntier-3 candidates left NULL (no aligned match): "
            f"{u.tier3_candidates_left_null:,}"
        )

        if not execute:
            print("\nDRY-RUN — nothing written. Re-run with --execute to apply.")
            raise SystemExit(0)

        res = conn.execute(
            text(
                "UPDATE ledger_entries l SET effective_date = e.effective_date "
                "FROM _eff e WHERE e.id = l.id "
                "AND l.effective_date IS DISTINCT FROM e.effective_date "
                "AND e.effective_date IS NOT NULL"
            )
        )
        print(f"\nDONE — updated {res.rowcount:,} rows.")


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)
