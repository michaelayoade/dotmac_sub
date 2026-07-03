"""Audit (and correct) double-counted Splynx-void mirror reversal debits.

The June 23 cutover cleanup posted lone refund-source ledger debits for
payments that had been voided in Splynx, on the premise that the voided value
had reached the local books via the prepaid opening-balance seed. But the seed
read ``subscribers.deposit``, and Splynx reduces the deposit when a payment is
voided — so any void already reflected in the deposit before the seed makes
the local debit a *double* correction that strips real customer money
(case study: Nigeria Custom Service Evolve 1, ledger entry
a9711180-f05d-44fe-8825-ef3adddfbdfa, -188,125.00).

The mirror invariant (see app/models/splynx_transaction.py) makes this
testable per account: deposit = Σcredit − Σdebit over mirror rows with
deleted='0'. For every active lone refund debit on a failed/canceled payment
this tool classifies:

- ``confirmed_double_correction`` — the payment's mirror row is deleted, the
  deposit equals the non-deleted mirror sum (void already absorbed), and the
  active opening seed equals the deposit. The debit is provably wrong;
  ``--apply`` deactivates it (balance-restoring, like the pair cleanup).
- ``overshoot_review`` — void absorbed by the deposit, but the active seed
  differs from the deposit, so part of the debit may offset seed excess.
  Reported with ``suggested_min_credit`` = debit − max(seed − deposit, 0);
  never auto-fixed.
- ``justified_keep`` — deposit still contains the voided amount
  (deposit = mirror sum + void), so the debit is load-bearing.
- ``review_*`` — missing splynx id / mirror row / deposit, mirror row not
  deleted, or deposit↔mirror mismatch. Manual review, never auto-fixed.

Targeted remedy for reviewed rows: ``--fix-entry <ledger-entry-id>``
[``--credit <amount>``] posts a compensating adjustment credit (defaults to
the full debit amount), idempotent by memo. Dry-run by default everywhere.

Examples
--------
  python -m scripts.one_off.audit_void_mirror_double_reversals
  python -m scripts.one_off.audit_void_mirror_double_reversals --csv out.csv
  python -m scripts.one_off.audit_void_mirror_double_reversals --apply
  python -m scripts.one_off.audit_void_mirror_double_reversals \
      --fix-entry a9711180-f05d-44fe-8825-ef3adddfbdfa --credit 102975.54 --apply
"""

from __future__ import annotations

import argparse
import csv
import sys
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.billing import (
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentStatus,
)
from app.models.splynx_transaction import SplynxBillingTransaction
from app.models.subscriber import Subscriber
from app.services.common import round_money

DEAD_PAYMENT_STATUSES = (PaymentStatus.failed, PaymentStatus.canceled)
OPENING_MEMO = "Prepaid opening balance @ cutover"
CORRECTION_MEMO = (
    "Correction: reverse erroneous duplicate-payment reversal [id={id}]"
)
TOLERANCE = Decimal("0.01")
DEFAULT_OUTPUT = "scratchpad/void_mirror_double_reversals.csv"

AUTO_FIXABLE = "confirmed_double_correction"


@dataclass(frozen=True)
class Finding:
    entry: LedgerEntry
    classification: str
    subscriber_name: str
    splynx_payment_id: int | None
    deposit: Decimal | None
    mirror_excl_deleted: Decimal | None
    seed_total: Decimal | None
    suggested_min_credit: Decimal | None


def _lone_refund_debits(session: Session) -> list[tuple[LedgerEntry, Payment]]:
    rows = session.execute(
        select(LedgerEntry, Payment)
        .join(Payment, Payment.id == LedgerEntry.payment_id)
        .where(
            LedgerEntry.is_active.is_(True),
            LedgerEntry.entry_type == LedgerEntryType.debit,
            LedgerEntry.source == LedgerSource.refund,
            Payment.status.in_(DEAD_PAYMENT_STATUSES),
        )
        .order_by(LedgerEntry.created_at)
    ).all()
    lone: list[tuple[LedgerEntry, Payment]] = []
    for entry, payment in rows:
        has_credit = session.execute(
            select(LedgerEntry.id).where(
                LedgerEntry.is_active.is_(True),
                LedgerEntry.payment_id == entry.payment_id,
                LedgerEntry.entry_type == LedgerEntryType.credit,
                LedgerEntry.source == LedgerSource.payment,
            )
        ).first()
        if has_credit is None:
            lone.append((entry, payment))
    return lone


def _mirror_sum_excl_deleted(session: Session, splynx_customer_id: int) -> Decimal:
    signed = case(
        (SplynxBillingTransaction.entry_type == "credit", SplynxBillingTransaction.amount),
        else_=-SplynxBillingTransaction.amount,
    )
    total = session.execute(
        select(func.coalesce(func.sum(signed), 0)).where(
            SplynxBillingTransaction.splynx_customer_id == splynx_customer_id,
            SplynxBillingTransaction.deleted.is_(False),
        )
    ).scalar_one()
    return round_money(Decimal(total))


def _mirror_row_deleted(
    session: Session, splynx_customer_id: int, splynx_payment_id: int
) -> bool | None:
    """True/False for the payment's mirror row deleted flag, None if absent."""
    deleted = session.execute(
        select(SplynxBillingTransaction.deleted).where(
            SplynxBillingTransaction.splynx_customer_id == splynx_customer_id,
            SplynxBillingTransaction.splynx_payment_id == splynx_payment_id,
        )
    ).scalars().all()
    if not deleted:
        return None
    return all(deleted)


def _active_seed_total(session: Session, account_id: uuid.UUID) -> Decimal | None:
    rows = session.execute(
        select(LedgerEntry.amount).where(
            LedgerEntry.account_id == account_id,
            LedgerEntry.is_active.is_(True),
            LedgerEntry.entry_type == LedgerEntryType.credit,
            LedgerEntry.memo == OPENING_MEMO,
        )
    ).scalars().all()
    if not rows:
        return None
    return round_money(sum(rows, Decimal("0")))


def _eq(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= TOLERANCE


def _classify(session: Session, entry: LedgerEntry, payment: Payment) -> Finding:
    subscriber = session.get(Subscriber, entry.account_id)
    name = ""
    deposit: Decimal | None = None
    mirror_excl: Decimal | None = None
    if subscriber is not None:
        name = f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
        if subscriber.deposit is not None:
            deposit = round_money(subscriber.deposit)
    seed_total = _active_seed_total(session, entry.account_id)
    amount = round_money(entry.amount)

    def finding(classification: str, suggested: Decimal | None = None) -> Finding:
        return Finding(
            entry=entry,
            classification=classification,
            subscriber_name=name,
            splynx_payment_id=payment.splynx_payment_id,
            deposit=deposit,
            mirror_excl_deleted=mirror_excl,
            seed_total=seed_total,
            suggested_min_credit=suggested,
        )

    if subscriber is None or subscriber.splynx_customer_id is None:
        return finding("review_no_splynx_customer")
    if payment.splynx_payment_id is None:
        return finding("review_no_splynx_payment_id")

    mirror_excl = _mirror_sum_excl_deleted(session, subscriber.splynx_customer_id)
    row_deleted = _mirror_row_deleted(
        session, subscriber.splynx_customer_id, payment.splynx_payment_id
    )
    if row_deleted is None:
        return finding("review_no_mirror_row")
    if not row_deleted:
        return finding("review_mirror_not_deleted")
    if deposit is None:
        return finding("review_no_deposit")

    if _eq(deposit, mirror_excl + amount):
        # Deposit still carries the voided value: the debit is load-bearing.
        return finding("justified_keep")
    if not _eq(deposit, mirror_excl):
        return finding("review_deposit_mirror_mismatch")

    # Void already absorbed by the deposit before the seed read it.
    if seed_total is not None and _eq(seed_total, deposit):
        return finding(AUTO_FIXABLE, suggested=amount)
    seed_excess = (
        max(seed_total - deposit, Decimal("0")) if seed_total is not None else Decimal("0")
    )
    return finding(
        "overshoot_review" if seed_total is not None else "review_no_seed",
        suggested=round_money(max(amount - seed_excess, Decimal("0"))),
    )


def _write_csv(path: Path, findings: list[Finding]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "classification",
                "ledger_entry_id",
                "account_id",
                "subscriber_name",
                "debit_amount",
                "payment_id",
                "splynx_payment_id",
                "deposit",
                "mirror_sum_excl_deleted",
                "active_seed_total",
                "suggested_min_credit",
                "entry_created_at",
                "memo",
            ]
        )
        for f in findings:
            writer.writerow(
                [
                    f.classification,
                    f.entry.id,
                    f.entry.account_id,
                    f.subscriber_name,
                    f.entry.amount,
                    f.entry.payment_id,
                    f.splynx_payment_id or "",
                    f.deposit if f.deposit is not None else "",
                    f.mirror_excl_deleted if f.mirror_excl_deleted is not None else "",
                    f.seed_total if f.seed_total is not None else "",
                    f.suggested_min_credit if f.suggested_min_credit is not None else "",
                    f.entry.created_at,
                    f.entry.memo,
                ]
            )


def _fix_entry(
    session: Session, entry_id: uuid.UUID, credit: Decimal | None, apply: bool
) -> int:
    entry = session.get(LedgerEntry, entry_id)
    if entry is None:
        print(f"fix-entry: ledger entry {entry_id} not found")
        return 1
    if entry.entry_type != LedgerEntryType.debit or entry.source != LedgerSource.refund:
        print(f"fix-entry: {entry_id} is not a refund-source debit")
        return 1
    memo = CORRECTION_MEMO.format(id=entry.id)
    existing = session.execute(
        select(LedgerEntry.id).where(
            LedgerEntry.account_id == entry.account_id,
            LedgerEntry.memo == memo,
            LedgerEntry.is_active.is_(True),
        )
    ).first()
    if existing is not None:
        print(f"fix-entry: correction already posted for {entry_id} — skipping")
        return 0
    amount = round_money(credit if credit is not None else entry.amount)
    if amount <= 0 or amount > round_money(entry.amount):
        print(f"fix-entry: credit {amount} must be in (0, {entry.amount}]")
        return 1
    print(
        f"{'APPLY' if apply else 'DRY-RUN'}: post {amount} adjustment credit to "
        f"account {entry.account_id} correcting debit {entry.id} ({entry.amount})"
    )
    if not apply:
        return 0
    session.add(
        LedgerEntry(
            id=uuid.uuid4(),
            account_id=entry.account_id,
            payment_id=entry.payment_id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.adjustment,
            amount=amount,
            currency=entry.currency,
            memo=memo,
            is_active=True,
        )
    )
    session.commit()
    print("posted")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes")
    parser.add_argument(
        "--csv", type=Path, default=Path(DEFAULT_OUTPUT), help="audit CSV path"
    )
    parser.add_argument(
        "--fix-entry",
        type=uuid.UUID,
        default=None,
        help="post a compensating credit for this reviewed debit instead of sweeping",
    )
    parser.add_argument(
        "--credit",
        type=Decimal,
        default=None,
        help="credit amount for --fix-entry (default: full debit amount)",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        if args.fix_entry is not None:
            return _fix_entry(session, args.fix_entry, args.credit, args.apply)

        findings = [
            _classify(session, entry, payment)
            for entry, payment in _lone_refund_debits(session)
        ]
        by_class: dict[str, list[Finding]] = {}
        for f in findings:
            by_class.setdefault(f.classification, []).append(f)
        print(f"{'APPLY' if args.apply else 'DRY-RUN'}: {len(findings)} lone refund debits")
        for classification in sorted(by_class):
            rows = by_class[classification]
            total = sum((f.entry.amount for f in rows), Decimal("0"))
            print(f"  {classification}: {len(rows)} rows, total {round_money(total)}")

        _write_csv(args.csv, findings)
        print(f"wrote {args.csv}")

        fixable = by_class.get(AUTO_FIXABLE, [])
        if not args.apply:
            return 0
        for f in fixable:
            f.entry.is_active = False
        session.commit()
        print(f"deactivated {len(fixable)} confirmed double-correction debits")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
