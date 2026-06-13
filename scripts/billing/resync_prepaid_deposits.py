"""Re-sync prepaid deposits from Splynx (authoritative) into the local app.

The local subscriber.deposit is a stale migration snapshot; Splynx's
customer_billing.deposit is the live prepaid balance (verified: customer
25313 = NGN 31,965.11 in Splynx vs a stale local value). This realigns
local deposits to Splynx for splynx-linked prepaid subscribers.

Deliberately narrow: touches ONLY subscriber.deposit. (The full
splynx_customer_sync also rewrites name/status/custom-fields — too broad for
a targeted drift fix.) Dry-run by default; --execute to write.

Usage:
    python scripts/billing/resync_prepaid_deposits.py            # dry-run
    python scripts/billing/resync_prepaid_deposits.py --execute
"""

from __future__ import annotations

import sys
from decimal import Decimal

from app.db import SessionLocal
from app.models.subscriber import Subscriber
from scripts.migration.db_connections import fetch_all, splynx_connection


def _dec(v) -> Decimal:
    return Decimal(str(v or "0")).quantize(Decimal("0.01"))


def main(execute: bool) -> None:
    db = SessionLocal()
    try:
        # Local splynx-linked prepaid subscribers, keyed by Splynx id.
        locals_ = (
            db.query(Subscriber)
            .filter(
                Subscriber.billing_mode == "prepaid",
                Subscriber.is_active.is_(True),
                Subscriber.splynx_customer_id.isnot(None),
            )
            .all()
        )
        by_cid = {int(s.splynx_customer_id): s for s in locals_}
        print(f"splynx-linked prepaid subscribers: {len(by_cid)}")
        if not by_cid:
            return

        placeholders = ",".join(["%s"] * len(by_cid))
        # placeholders are a fixed count of %s; the customer ids are bound as
        # query params (tuple(by_cid)), not interpolated — so this is safe.
        query = (
            "SELECT customer_id, deposit FROM customer_billing "  # noqa: S608
            f"WHERE customer_id IN ({placeholders})"
        )
        with splynx_connection() as conn:
            rows = fetch_all(conn, query, tuple(by_cid))
        splynx_deposit = {int(r["customer_id"]): _dec(r["deposit"]) for r in rows}

        drift = []
        for cid, sub in by_cid.items():
            authoritative = splynx_deposit.get(cid)
            if authoritative is None:
                continue
            local = _dec(sub.deposit)
            if local != authoritative:
                drift.append((cid, sub, local, authoritative))

        total_delta = sum((a - lo) for _, _, lo, a in drift)
        print(f"splynx rows returned             : {len(splynx_deposit)}")
        print(f"subscribers with deposit drift   : {len(drift)}")
        print(f"net deposit correction (Splynx-local): NGN {total_delta:,.2f}")
        for cid, _, lo, a in drift[:10]:
            print(f"  cust {cid}: local {lo:,.2f} -> splynx {a:,.2f}")
        if len(drift) > 10:
            print(f"  ... and {len(drift) - 10} more")

        if not execute:
            print("\nDRY-RUN — nothing changed. Re-run with --execute to apply.")
            return

        for _, sub, _, authoritative in drift:
            sub.deposit = authoritative
        db.commit()
        print(f"\nDONE — corrected {len(drift)} prepaid deposits from Splynx.")
    finally:
        db.close()


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)
