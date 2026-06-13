"""Read-only pre-enable rehearsal for the prepaid drawdown engine.

This is the single safest step before enabling the engine: it computes what the
engine WOULD charge and what every prepaid balance WOULD be, and reconciles both
against Splynx — without moving a single naira. Two checks:

1. CHARGE RATE — per active prepaid subscription, the engine's intended
   monthly-equivalent charge (via the real ``_period_charge``) is compared to
   the imported per-service price (``subscription.unit_price``). Flags:
     - zero-charge prepaid subs (the "free internet" failure mode), and
     - rate mismatches (engine monthly != unit_price) beyond tolerance.

2. BALANCE / SEED — per prepaid subscriber, the local resolved balance
   (``_resolve_prepaid_available_balance``: the seeded ledger if seeded, else
   the synced deposit) is compared to the LIVE Splynx ``customer_billing.deposit``.
   A mismatch means a wrong starting balance → wrong suspension timing.

Run daily during the shadow window before enable; investigate any non-empty
mismatch set. Read-only: never writes, never charges.

Usage:
    python scripts/billing/reconcile_prepaid_billing.py [--tolerance 1.00] [--limit 25]
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal

from app.db import SessionLocal
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.collections._core import _resolve_prepaid_available_balance
from app.services.prepaid_billing import _monthly_equivalent, _period_charge
from scripts.migration.db_connections import fetch_all, splynx_connection


def _arg(flag: str, default: str) -> str:
    if flag in sys.argv:
        return sys.argv[sys.argv.index(flag) + 1]
    return default


def main() -> bool:
    tolerance = Decimal(_arg("--tolerance", "1.00"))
    limit = int(_arg("--limit", "25"))
    now = datetime.now(UTC)
    db = SessionLocal()
    try:
        subs = (
            db.query(Subscription)
            .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
            .filter(Subscription.billing_mode == BillingMode.prepaid)
            .filter(Subscription.status == SubscriptionStatus.active)
            .filter(Subscriber.status == SubscriberStatus.active)
            .all()
        )
        print(f"active prepaid subscriptions: {len(subs)}")

        # --- 1. CHARGE RATE ---
        zero_charge = []
        rate_mismatch = []
        for sub in subs:
            charge, _currency, period_days = _period_charge(db, sub, now)
            if charge <= Decimal("0.00"):
                zero_charge.append((sub, period_days))
                continue
            # The engine's monthly-equivalent should match the imported price.
            monthly = (
                _monthly_equivalent(charge, None) * Decimal("30") / Decimal(period_days)
            )
            expected = (
                Decimal(str(sub.unit_price))
                if sub.unit_price is not None and sub.unit_price > 0
                else None
            )
            if expected is not None and abs(monthly - expected) > tolerance:
                rate_mismatch.append((sub, monthly, expected))

        print("\n=== 1. CHARGE RATE ===")
        print(f"zero-charge prepaid subs (FREE SERVICE risk): {len(zero_charge)}")
        for sub, pd in zero_charge[:limit]:
            print(f"  sub {str(sub.id)[:8]} offer={sub.offer_id} period={pd}d -> 0.00")
        print(f"rate mismatches (>tol {tolerance}): {len(rate_mismatch)}")
        for sub, monthly, expected in rate_mismatch[:limit]:
            print(
                f"  sub {str(sub.id)[:8]}: engine monthly {monthly:,.2f} "
                f"vs unit_price {expected:,.2f}"
            )

        # --- 2. BALANCE / SEED vs Splynx deposit ---
        splynx_subs = [
            s for s in subs if s.subscriber and s.subscriber.splynx_customer_id
        ]
        by_cid = {
            int(s.subscriber.splynx_customer_id): s.subscriber for s in splynx_subs
        }
        splynx_deposit: dict[int, Decimal] = {}
        if by_cid:
            placeholders = ",".join(["%s"] * len(by_cid))
            query = (
                "SELECT customer_id, deposit FROM customer_billing "  # noqa: S608
                f"WHERE customer_id IN ({placeholders})"
            )
            with splynx_connection() as conn:
                for r in fetch_all(conn, query, tuple(by_cid)):
                    splynx_deposit[int(r["customer_id"])] = Decimal(
                        str(r["deposit"] or "0")
                    ).quantize(Decimal("0.01"))

        balance_mismatch = []
        for cid, subscriber in by_cid.items():
            authoritative = splynx_deposit.get(cid)
            if authoritative is None:
                continue
            local = _resolve_prepaid_available_balance(db, str(subscriber.id)).quantize(
                Decimal("0.01")
            )
            if abs(local - authoritative) > tolerance:
                balance_mismatch.append((cid, local, authoritative))

        print("\n=== 2. BALANCE / SEED vs Splynx deposit ===")
        print(f"splynx-linked prepaid subscribers checked: {len(by_cid)}")
        print(f"balance mismatches (>tol {tolerance}): {len(balance_mismatch)}")
        for cid, local, auth in balance_mismatch[:limit]:
            print(f"  cust {cid}: local {local:,.2f} vs splynx {auth:,.2f}")

        ok = not zero_charge and not rate_mismatch and not balance_mismatch
        print(
            "\nRESULT: "
            + (
                "CLEAN — safe to proceed."
                if ok
                else "MISMATCHES FOUND — investigate before enable."
            )
        )
        return ok
    finally:
        db.close()


if __name__ == "__main__":
    # Machine-enforced cutover gate: non-zero exit on any mismatch so
    # `reconcile && seed && enable` (or any wrapper) HALTS on a dirty result
    # instead of sailing through an advisory print.
    raise SystemExit(0 if main() else 1)
