"""Repair imported Splynx payments whose active allocations exceed receipt cash.

The drift detector found legacy Splynx receipts that were allocated to more
invoice value than the original payment amount. The prevention fix excludes
``Payment.splynx_payment_id`` from new credit-settlement backing; this one-off
repairs the historical rows by keeping each imported payment's oldest active
allocations up to the payment amount, then reducing or deactivating later
allocations.

Dry-run by default; pass ``--apply`` to write changes.

  python -m scripts.one_off.repair_splynx_overallocated_payments
  python -m scripts.one_off.repair_splynx_overallocated_payments --apply
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.billing import (
    Invoice,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from app.services.billing._common import _recalculate_invoice_totals
from app.services.common import coerce_uuid, round_money, to_decimal


@dataclass
class AllocationRepair:
    allocation_id: UUID
    payment_id: UUID
    invoice_id: UUID
    old_amount: Decimal
    new_amount: Decimal

    @property
    def removed_amount(self) -> Decimal:
        return round_money(self.old_amount - self.new_amount)

    @property
    def deactivate(self) -> bool:
        return self.new_amount <= Decimal("0.00")


@dataclass
class RepairPlan:
    repairs: list[AllocationRepair] = field(default_factory=list)

    @property
    def payments(self) -> int:
        return len({repair.payment_id for repair in self.repairs})

    @property
    def invoices(self) -> int:
        return len({repair.invoice_id for repair in self.repairs})

    @property
    def full_deactivations(self) -> int:
        return sum(1 for repair in self.repairs if repair.deactivate)

    @property
    def partial_reductions(self) -> int:
        return sum(1 for repair in self.repairs if not repair.deactivate)

    @property
    def total_removed(self) -> Decimal:
        return round_money(
            sum((repair.removed_amount for repair in self.repairs), Decimal("0.00"))
        )


def _overallocated_splynx_payment_ids(
    db: Session, *, payment_id: str | None = None, limit: int | None = None
) -> list[UUID]:
    allocated_sq = (
        db.query(
            PaymentAllocation.payment_id.label("payment_id"),
            func.sum(PaymentAllocation.amount).label("allocated"),
        )
        .filter(PaymentAllocation.is_active.is_(True))
        .group_by(PaymentAllocation.payment_id)
        .subquery()
    )
    query = (
        db.query(Payment.id)
        .join(allocated_sq, allocated_sq.c.payment_id == Payment.id)
        .filter(Payment.is_active.is_(True))
        .filter(Payment.status == PaymentStatus.succeeded)
        .filter(Payment.splynx_payment_id.is_not(None))
        .filter(allocated_sq.c.allocated - Payment.amount > Decimal("0.01"))
        .order_by(Payment.created_at.asc(), Payment.id.asc())
    )
    if payment_id:
        query = query.filter(Payment.id == coerce_uuid(payment_id))
    if limit:
        query = query.limit(limit)
    return [row[0] for row in query.all()]


def build_repair_plan(
    db: Session, *, payment_id: str | None = None, limit: int | None = None
) -> RepairPlan:
    plan = RepairPlan()
    for pid in _overallocated_splynx_payment_ids(
        db, payment_id=payment_id, limit=limit
    ):
        payment = db.get(Payment, pid)
        if payment is None:
            continue
        remaining = round_money(to_decimal(payment.amount))
        allocations = (
            db.query(PaymentAllocation)
            .filter(PaymentAllocation.payment_id == payment.id)
            .filter(PaymentAllocation.is_active.is_(True))
            .order_by(PaymentAllocation.created_at.asc(), PaymentAllocation.id.asc())
            .all()
        )
        for allocation in allocations:
            amount = round_money(to_decimal(allocation.amount))
            if remaining >= amount:
                remaining = round_money(remaining - amount)
                continue
            new_amount = max(Decimal("0.00"), remaining)
            if amount > new_amount:
                plan.repairs.append(
                    AllocationRepair(
                        allocation_id=allocation.id,
                        payment_id=allocation.payment_id,
                        invoice_id=allocation.invoice_id,
                        old_amount=amount,
                        new_amount=new_amount,
                    )
                )
            remaining = Decimal("0.00")
    return plan


def _active_payment_ledgers(
    db: Session, allocation: PaymentAllocation
) -> list[LedgerEntry]:
    return (
        db.query(LedgerEntry)
        .filter(LedgerEntry.payment_id == allocation.payment_id)
        .filter(LedgerEntry.invoice_id == allocation.invoice_id)
        .filter(LedgerEntry.source == LedgerSource.payment)
        .filter(LedgerEntry.entry_type == LedgerEntryType.credit)
        .filter(LedgerEntry.is_active.is_(True))
        .all()
    )


def apply_repair_plan(db: Session, plan: RepairPlan) -> None:
    touched_invoice_ids: set[UUID] = set()
    for repair in plan.repairs:
        allocation = db.get(PaymentAllocation, repair.allocation_id)
        if allocation is None or not allocation.is_active:
            continue
        current_amount = round_money(to_decimal(allocation.amount))
        if current_amount != repair.old_amount:
            raise RuntimeError(
                f"Allocation {allocation.id} changed from planned amount "
                f"{repair.old_amount} to {current_amount}; aborting"
            )

        ledgers = _active_payment_ledgers(db, allocation)
        if repair.deactivate:
            allocation.is_active = False
            for ledger in ledgers:
                ledger.is_active = False
        else:
            if len(ledgers) > 1:
                raise RuntimeError(
                    f"Allocation {allocation.id} has {len(ledgers)} active payment "
                    "ledger credits; partial reduction is ambiguous"
                )
            allocation.amount = repair.new_amount
            if ledgers:
                ledgers[0].amount = repair.new_amount
        touched_invoice_ids.add(allocation.invoice_id)

    db.flush()
    for invoice_id in touched_invoice_ids:
        invoice = db.get(Invoice, invoice_id)
        if invoice is not None:
            _recalculate_invoice_totals(db, invoice)
    db.flush()


def _print_plan(plan: RepairPlan, *, apply: bool) -> None:
    mode = "APPLY" if apply else "DRY-RUN (no changes written)"
    print(f"\n=== Repair Splynx over-allocated payments - {mode} ===")
    print(f"payments touched       : {plan.payments}")
    print(f"invoices touched       : {plan.invoices}")
    print(f"partial reductions     : {plan.partial_reductions}")
    print(f"full deactivations     : {plan.full_deactivations}")
    print(f"total allocation remove: {plan.total_removed}")
    if plan.repairs:
        print("\n--- first 25 allocation repairs ---")
        for repair in plan.repairs[:25]:
            action = "deactivate" if repair.deactivate else "reduce"
            print(
                f"  {action} allocation={repair.allocation_id} "
                f"payment={repair.payment_id} invoice={repair.invoice_id} "
                f"{repair.old_amount}->{repair.new_amount} "
                f"removed={repair.removed_amount}"
            )
    if not apply:
        print("\nRe-run with --apply to write these repairs.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write allocation repairs. Without this flag the script is read-only.",
    )
    parser.add_argument(
        "--payment-id",
        help="Restrict the repair plan to one payment UUID.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit the number of over-allocated payments considered.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        plan = build_repair_plan(db, payment_id=args.payment_id, limit=args.limit)
        _print_plan(plan, apply=args.apply)
        if args.apply and plan.repairs:
            apply_repair_plan(db, plan)
            db.commit()
            print("\nCommitted repairs.")
        else:
            db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    main()
