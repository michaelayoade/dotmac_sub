from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import exists, select

from app.db import SessionLocal
from app.models.billing import Payment, PaymentStatus
from app.models.subscriber import Subscriber
from app.services.billing.payments import Refunds
from app.services.common import round_money
from app.services.notification_suppression import suppress_notifications

OUTPUT = Path("/tmp/duplicate_paystack_recovery_reversals.csv")
RECOVERY_MEMO_PREFIX = "Paystack cutover recovery ref:"
MAX_PAYSTACK_FEE_DELTA = Decimal("1000.00")


@dataclass(frozen=True)
class Candidate:
    payment_id: str
    account_id: str
    subscriber_number: str
    subscriber_name: str
    paid_date: str
    recovery_amount: Decimal
    recovery_external_id: str
    sibling_payment_id: str
    sibling_amount: Decimal
    sibling_memo: str
    delta: Decimal


def _subscriber_name(subscriber: Subscriber) -> str:
    return (
        subscriber.display_name
        or subscriber.company_name
        or f"{subscriber.first_name} {subscriber.last_name}".strip()
    )


def _load_candidates(db) -> list[Candidate]:
    recoveries = db.scalars(
        select(Payment)
        .where(Payment.is_active.is_(True))
        .where(Payment.status == PaymentStatus.succeeded)
        .where(Payment.memo.like(f"{RECOVERY_MEMO_PREFIX}%"))
        .order_by(Payment.paid_at.asc(), Payment.created_at.asc())
    ).all()
    candidates: list[Candidate] = []
    for recovery in recoveries:
        if recovery.account_id is None or recovery.paid_at is None:
            continue
        paid_date = recovery.paid_at.date()
        siblings = db.scalars(
            select(Payment)
            .where(Payment.account_id == recovery.account_id)
            .where(Payment.id != recovery.id)
            .where(Payment.is_active.is_(True))
            .where(Payment.status == PaymentStatus.succeeded)
            .where(Payment.paid_at.is_not(None))
            .where(Payment.memo.not_like(f"{RECOVERY_MEMO_PREFIX}%"))
            .order_by(Payment.amount.desc())
        ).all()
        duplicate_sibling = None
        duplicate_delta = None
        for sibling in siblings:
            if sibling.paid_at is None or sibling.paid_at.date() != paid_date:
                continue
            delta = round_money(recovery.amount - sibling.amount)
            if Decimal("0.00") <= delta <= MAX_PAYSTACK_FEE_DELTA:
                duplicate_sibling = sibling
                duplicate_delta = delta
                break
        if duplicate_sibling is None or duplicate_delta is None:
            continue
        subscriber = db.get(Subscriber, recovery.account_id)
        candidates.append(
            Candidate(
                payment_id=str(recovery.id),
                account_id=str(recovery.account_id),
                subscriber_number=subscriber.subscriber_number if subscriber else "",
                subscriber_name=_subscriber_name(subscriber) if subscriber else "",
                paid_date=paid_date.isoformat(),
                recovery_amount=round_money(recovery.amount),
                recovery_external_id=recovery.external_id or "",
                sibling_payment_id=str(duplicate_sibling.id),
                sibling_amount=round_money(duplicate_sibling.amount),
                sibling_memo=duplicate_sibling.memo or "",
                delta=duplicate_delta,
            )
        )
    return candidates


def _write_csv(
    path: Path, candidates: list[Candidate], results: dict[str, str]
) -> None:
    rows = []
    for candidate in candidates:
        rows.append(
            {
                **candidate.__dict__,
                "result": results.get(candidate.payment_id, "dry_run"),
            }
        )
    fieldnames = (
        list(rows[0].keys())
        if rows
        else [
            "payment_id",
            "account_id",
            "subscriber_number",
            "subscriber_name",
            "paid_date",
            "recovery_amount",
            "recovery_external_id",
            "sibling_payment_id",
            "sibling_amount",
            "sibling_memo",
            "delta",
            "result",
        ]
    )
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(OUTPUT))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    results: dict[str, str] = {}
    try:
        candidates = _load_candidates(db)
        if args.apply:
            for candidate in candidates:
                already_failed = db.scalar(
                    select(
                        exists().where(
                            Payment.id == candidate.payment_id,
                            Payment.status == PaymentStatus.failed,
                        )
                    )
                )
                if already_failed:
                    results[candidate.payment_id] = "already_reversed"
                    continue
                with suppress_notifications():
                    Refunds.reverse_payment(
                        db,
                        candidate.payment_id,
                        reason=(
                            "Reverse duplicate Paystack cutover recovery; "
                            f"local same-day payment {candidate.sibling_payment_id} "
                            "already recorded this transaction"
                        ),
                    )
                results[candidate.payment_id] = "reversed"
        _write_csv(Path(args.output), candidates, results)
    finally:
        db.close()

    total = sum(
        (candidate.recovery_amount for candidate in candidates), Decimal("0.00")
    )
    print("=== APPLY ===" if args.apply else "=== DRY-RUN ===")
    print(f"duplicate recovery candidates: {len(candidates)}")
    print(f"total: {round_money(total)}")
    for status in sorted(set(results.values())):
        print(f"  {status}: {sum(1 for value in results.values() if value == status)}")
    print(f"output: {args.output}")


if __name__ == "__main__":
    main()
