"""Post verified Paystack cutover payments from the reconcile CSV.

Dry-run by default. In apply mode this consumes rows exported by
``paystack_cutover_reconcile_export.py`` and records only rows that are:

- ``classification=missing_success_payment``
- ``recovery_bucket=single_email_match``
- backed by a fresh Paystack verify call with matching transaction id/amount

Ambiguous shared-email rows and no-match rows are deliberately left for manual
review. Customer notifications are suppressed because this is back-office
cutover bookkeeping, not a new customer-initiated payment.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.billing import (
    Payment,
    PaymentProvider,
    PaymentProviderType,
    PaymentStatus,
)
from app.schemas.billing import PaymentCreate
from app.services import billing as billing_service
from app.services.common import round_money
from app.services.notification_suppression import suppress_notifications
from app.services.paystack import kobo_to_naira, verify_transaction

DEFAULT_INPUT = "scratchpad/paystack_cutover_reconcile.csv"


@dataclass(frozen=True)
class Candidate:
    reference: str
    paystack_id: str
    amount: Decimal
    currency: str
    paid_at: datetime | None
    account_id: UUID


def _parse_paid_at(value: str) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _candidate_from_row(row: dict[str, str]) -> Candidate | None:
    if row.get("classification") != "missing_success_payment":
        return None
    if row.get("recovery_bucket") != "single_email_match":
        return None
    account_id = (row.get("matched_subscriber_id") or "").strip()
    if not account_id:
        return None
    return Candidate(
        reference=(row.get("reference") or "").strip(),
        paystack_id=(row.get("paystack_id") or "").strip(),
        amount=Decimal(row.get("amount") or "0"),
        currency=(row.get("currency") or "NGN").strip() or "NGN",
        paid_at=_parse_paid_at(row.get("paid_at") or ""),
        account_id=UUID(account_id),
    )


def _load_candidates(path: Path) -> list[Candidate]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        return [
            candidate
            for row in reader
            if (candidate := _candidate_from_row(row)) is not None
        ]


def _paystack_provider(db: Session) -> PaymentProvider:
    provider = db.scalars(
        select(PaymentProvider)
        .where(PaymentProvider.provider_type == PaymentProviderType.paystack)
        .order_by(PaymentProvider.is_active.desc(), PaymentProvider.created_at.asc())
        .limit(1)
    ).first()
    if provider is None:
        raise RuntimeError("No Paystack payment provider configured")
    return provider


def _existing_payment(
    db: Session, provider_id: UUID, candidate: Candidate
) -> Payment | None:
    candidates = [v for v in {candidate.paystack_id, candidate.reference} if v]
    if not candidates:
        return None
    return db.scalars(
        select(Payment)
        .where(Payment.external_id.in_(candidates))
        .where((Payment.provider_id == provider_id) | (Payment.provider_id.is_(None)))
        .order_by(Payment.is_active.desc(), Payment.created_at.desc())
        .limit(1)
    ).first()


def _verify_candidate(db: Session, candidate: Candidate) -> None:
    tx = verify_transaction(db, candidate.reference)
    if tx.get("status") != "success":
        raise RuntimeError(
            f"{candidate.reference}: Paystack status is {tx.get('status')}"
        )
    paystack_id = str(tx.get("id") or "")
    if paystack_id != candidate.paystack_id:
        raise RuntimeError(
            f"{candidate.reference}: Paystack id changed "
            f"({paystack_id} != {candidate.paystack_id})"
        )
    amount = round_money(kobo_to_naira(int(tx.get("amount") or 0)))
    if amount != round_money(candidate.amount):
        raise RuntimeError(
            f"{candidate.reference}: amount changed ({amount} != {candidate.amount})"
        )


def _post_candidate(
    db: Session,
    provider: PaymentProvider,
    candidate: Candidate,
    *,
    dry_run: bool,
) -> str:
    existing = _existing_payment(db, provider.id, candidate)
    if existing is not None:
        return f"skip_existing:{existing.id}"

    _verify_candidate(db, candidate)
    if dry_run:
        db.rollback()
        return "would_post"

    with suppress_notifications():
        payment = billing_service.payments.create(
            db,
            PaymentCreate(
                account_id=candidate.account_id,
                provider_id=provider.id,
                amount=round_money(candidate.amount),
                currency=candidate.currency,
                status=PaymentStatus.succeeded,
                paid_at=candidate.paid_at,
                external_id=candidate.paystack_id,
                memo=(
                    "Paystack cutover recovery "
                    f"ref: {candidate.reference} id: {candidate.paystack_id}"
                ),
            ),
            auto_allocate=True,
        )
    return f"posted:{payment.id}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write payments. Without this flag the script is read-only.",
    )
    args = parser.parse_args()

    candidates = _load_candidates(Path(args.input))
    if args.limit is not None:
        candidates = candidates[: args.limit]

    dry_run = not args.apply
    db = SessionLocal()
    results: list[tuple[Candidate, str]] = []
    try:
        provider = _paystack_provider(db)
        for candidate in candidates:
            try:
                result = _post_candidate(
                    db,
                    provider,
                    candidate,
                    dry_run=dry_run,
                )
            except Exception as exc:
                db.rollback()
                result = f"error:{exc}"
            results.append((candidate, result))
    finally:
        db.close()

    counts: dict[str, int] = {}
    total_would_or_posted = Decimal("0.00")
    for candidate, result in results:
        status = result.split(":", 1)[0]
        counts[status] = counts.get(status, 0) + 1
        if status in {"would_post", "posted"}:
            total_would_or_posted += candidate.amount

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"Paystack cutover post credits — {mode}")
    print(f"candidates: {len(candidates)}")
    for key in sorted(counts):
        print(f"  {key}: {counts[key]}")
    print(f"total_would_or_posted: {total_would_or_posted}")
    for candidate, result in results:
        print(
            f"{result}\t{candidate.reference}\t{candidate.paystack_id}\t"
            f"{candidate.amount}\t{candidate.account_id}"
        )
    if dry_run:
        print("Re-run with --apply to write these payments.")


if __name__ == "__main__":
    main()
