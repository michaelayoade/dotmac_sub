"""Parity proof: the imported transaction mirror reconciles to Splynx deposit.

For every splynx-linked subscriber, the mirror's net (Σ credit − Σ debit over
non-deleted rows) must equal the live Splynx ``customer_billing.deposit``. Any
account that doesn't reconcile within tolerance is reported. Read-only.

Usage:
    python scripts/billing/reconcile_transaction_deposits.py [--tolerance 0.01] [--limit 25]
"""

from __future__ import annotations

import sys
from decimal import Decimal

from sqlalchemy import case, func

from app.db import SessionLocal
from app.models.splynx_transaction import SplynxBillingTransaction as T
from scripts.migration.db_connections import fetch_all, splynx_connection


def _arg(flag: str, default: str) -> str:
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def main() -> None:
    tol = Decimal(_arg("--tolerance", "0.01"))
    limit = int(_arg("--limit", "25"))
    db = SessionLocal()
    try:
        # Mirror net per customer: Σcredit − Σdebit over non-deleted rows. Only
        # credit/debit move the balance; 'other' (Splynx's rare empty-type rows)
        # is excluded — matching how Splynx computes the deposit.
        signed = func.sum(
            case(
                (T.entry_type == "credit", T.amount),
                (T.entry_type == "debit", -T.amount),
                else_=0,
            )
        )
        rows = (
            db.query(T.splynx_customer_id, signed)
            .filter(T.deleted.is_(False))
            .group_by(T.splynx_customer_id)
            .all()
        )
        mirror = {
            int(cid): Decimal(str(net or 0)).quantize(Decimal("0.01"))
            for cid, net in rows
        }
        print(f"customers with mirrored transactions: {len(mirror)}")
        if not mirror:
            print("mirror empty — run import_splynx_transactions.py --execute first.")
            return

        cids = list(mirror)
        deposit: dict[int, Decimal] = {}
        with splynx_connection() as c:
            for i in range(0, len(cids), 5000):
                chunk = cids[i : i + 5000]
                ph = ",".join(["%s"] * len(chunk))
                for r in fetch_all(
                    c,
                    f"SELECT customer_id, deposit FROM customer_billing WHERE customer_id IN ({ph})",  # noqa: S608
                    tuple(chunk),
                ):
                    deposit[int(r["customer_id"])] = Decimal(
                        str(r["deposit"] or "0")
                    ).quantize(Decimal("0.01"))

        mismatches = []
        for cid, net in mirror.items():
            dep = deposit.get(cid)
            if dep is None:
                continue
            if abs(net - dep) > tol:
                mismatches.append((cid, net, dep, net - dep))

        print(f"reconciled exactly (within {tol}): {len(mirror) - len(mismatches)}")
        print(f"MISMATCHES: {len(mismatches)}")
        for cid, net, dep, diff in sorted(
            mismatches, key=lambda x: abs(x[3]), reverse=True
        )[:limit]:
            print(
                f"  cust {cid}: mirror {net:,.2f} vs deposit {dep:,.2f}  (diff {diff:,.2f})"
            )

        ok = not mismatches
        print(
            "\nRESULT: "
            + (
                "PARITY — mirror reconciles to deposit."
                if ok
                else "MISMATCHES — investigate."
            )
        )
        raise SystemExit(0 if ok else 1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
