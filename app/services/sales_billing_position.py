"""The Sale -> Money handoff: read a sales order's position from billing.

`sales.orders` currently *stores* `amount_paid`, `balance_due`,
`payment_status` and `paid_at`. Those are Money-pipeline facts held as
Sale-pipeline columns, derived by ad-hoc assignment rather than by the invoice
state machine, and reconciled against the ledger by nothing. One duplicated
boundary has already produced four separate money defects.

This module is the read side of the replacement: given a SalesOrder, resolve
what billing says about it. It is the shadow phase of an explicit authority
migration --

  old owner   sales.orders, storing derived money columns
  new owner   financial.invoices / financial.payments, read through here
  shadow      both computed; disagreement reported, never auto-corrected
  cutover     stored columns become reads once drift is understood and zero
  retirement  the columns are dropped and _apply_payment_fields deleted

It writes nothing and decides nothing. Money repairs need finance approval and
belong to their owner, so drift here is evidence for a human, not an input to
an automatic correction.

## Joining is currently metadata, not structure

There is no foreign key from Invoice or Payment to SalesOrder. The links are:

  installation invoice   Project.metadata_["selfcare_installation_invoice_id"],
                         reached from the order's Project
  subscription invoice   SalesOrderLine.metadata_["selfcare_subscription_invoice_id"]
  payment                Payment.external_id == "crm:sales_order:{id}:payment"

The sales-to-service contract says the chain uses structural foreign keys and
that metadata identifiers are provenance, not canonical joins. That is not true
of this boundary, and it is the reason the handoff has no contract: there is
nothing structural to contract over. Establishing those keys is the next slice;
this module deliberately reads what exists today so the shadow phase can start
without a migration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.models.sales import SalesOrder, SalesOrderPaymentStatus
from app.services.common import coerce_uuid, round_money

logger = logging.getLogger(__name__)

_ZERO = Decimal("0.00")

#: Invoice states that still represent customer-facing debt.
_OPEN_INVOICE_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)


@dataclass(frozen=True)
class SalesOrderBillingPosition:
    """What billing says about one sales order.

    ``invoiced`` and ``settled`` come from the ledger. ``payment_status`` is the
    status the Sale pipeline *would* derive from them, so the shadow comparison
    is like-for-like against the stored column.
    """

    sales_order_id: UUID
    invoiced: Decimal = _ZERO
    settled: Decimal = _ZERO
    open_balance: Decimal = _ZERO
    payment_status: str = SalesOrderPaymentStatus.pending.value
    invoice_ids: tuple[UUID, ...] = ()
    payment_ids: tuple[UUID, ...] = ()
    #: Joins that could not be resolved, e.g. a metadata id pointing nowhere.
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True)
class SalesOrderBillingDrift:
    """A disagreement between the stored Sale columns and the ledger."""

    sales_order_id: UUID
    field: str
    stored: str
    billing: str

    def __str__(self) -> str:  # pragma: no cover - operator display
        return (
            f"sales_order={self.sales_order_id} {self.field}: "
            f"stored={self.stored} billing={self.billing}"
        )


def _invoice_ids_for(
    db: Session, sales_order: SalesOrder
) -> tuple[list[UUID], list[str]]:
    """Collect the invoice ids reachable from a sales order, and what was not."""
    ids: list[UUID] = []
    unresolved: list[str] = []

    project = sales_order.project
    if project is not None and isinstance(project.metadata_, dict):
        raw = str(
            project.metadata_.get("selfcare_installation_invoice_id") or ""
        ).strip()
        if raw:
            resolved = coerce_uuid(raw)
            if resolved is None:
                unresolved.append(f"installation_invoice:{raw}")
            else:
                ids.append(resolved)

    for line in sales_order.lines or ():
        if not getattr(line, "is_active", True):
            continue
        meta = line.metadata_ if isinstance(line.metadata_, dict) else {}
        raw = str(meta.get("selfcare_subscription_invoice_id") or "").strip()
        if not raw:
            continue
        resolved = coerce_uuid(raw)
        if resolved is None:
            unresolved.append(f"subscription_invoice:{raw}")
        else:
            ids.append(resolved)

    # Preserve order, drop duplicates: one invoice may be referenced twice.
    seen: set[UUID] = set()
    unique = [i for i in ids if not (i in seen or seen.add(i))]
    return unique, unresolved


def resolve_billing_position(
    db: Session, sales_order: SalesOrder
) -> SalesOrderBillingPosition:
    """Read this sales order's money position from the billing ledger.

    Pure read. Never writes, never repairs, never asks another owner to.
    """
    invoice_ids, unresolved = _invoice_ids_for(db, sales_order)

    invoiced = _ZERO
    open_balance = _ZERO
    found_ids: list[UUID] = []
    if invoice_ids:
        invoices = db.scalars(
            select(Invoice).where(
                Invoice.id.in_(invoice_ids), Invoice.is_active.is_(True)
            )
        ).all()
        found = {invoice.id for invoice in invoices}
        unresolved.extend(f"invoice_missing:{i}" for i in invoice_ids if i not in found)
        for invoice in invoices:
            found_ids.append(invoice.id)
            invoiced += Decimal(str(invoice.total or 0))
            if invoice.status in _OPEN_INVOICE_STATUSES:
                open_balance += Decimal(str(invoice.balance_due or 0))

    # The sales-order payment carries a stable external ref from the CRM era.
    payments = db.scalars(
        select(Payment).where(
            Payment.external_id == f"crm:sales_order:{sales_order.id}:payment",
            Payment.is_active.is_(True),
            Payment.status == PaymentStatus.succeeded,
        )
    ).all()
    settled = sum(
        (Decimal(str(payment.amount or 0)) for payment in payments), start=_ZERO
    )

    invoiced = round_money(invoiced)
    settled = round_money(settled)
    open_balance = round_money(open_balance)

    if invoiced > 0 and open_balance <= 0:
        status = SalesOrderPaymentStatus.paid.value
    elif settled > 0:
        status = SalesOrderPaymentStatus.partial.value
    else:
        status = SalesOrderPaymentStatus.pending.value

    return SalesOrderBillingPosition(
        sales_order_id=sales_order.id,
        invoiced=invoiced,
        settled=settled,
        open_balance=open_balance,
        payment_status=status,
        invoice_ids=tuple(found_ids),
        payment_ids=tuple(payment.id for payment in payments),
        unresolved=tuple(unresolved),
    )


def compare_with_stored(
    sales_order: SalesOrder, position: SalesOrderBillingPosition
) -> list[SalesOrderBillingDrift]:
    """Report where the stored Sale columns disagree with the ledger.

    Shadow-phase evidence only. A disagreement is a question for finance, not a
    licence to rewrite either side — a stored column may be right and the join
    incomplete, which is exactly what this phase exists to find out.
    """
    drifts: list[SalesOrderBillingDrift] = []

    # A waived order is settled by decision, not by money, so the ledger will
    # legitimately show nothing settled. Comparing it would report drift on
    # every waiver.
    if sales_order.payment_status == SalesOrderPaymentStatus.waived.value:
        return drifts

    # An order billing cannot see at all is not drift — it is an unlinked
    # order, reported separately so the two are never conflated.
    if not position.invoice_ids and not position.payment_ids:
        return drifts

    stored_paid = round_money(Decimal(str(sales_order.amount_paid or 0)))
    if stored_paid != position.settled:
        drifts.append(
            SalesOrderBillingDrift(
                sales_order_id=sales_order.id,
                field="amount_paid",
                stored=str(stored_paid),
                billing=str(position.settled),
            )
        )

    if sales_order.payment_status != position.payment_status:
        drifts.append(
            SalesOrderBillingDrift(
                sales_order_id=sales_order.id,
                field="payment_status",
                stored=str(sales_order.payment_status),
                billing=position.payment_status,
            )
        )

    return drifts


@dataclass
class BillingShadowReport:
    """Aggregate shadow-phase result across the scanned cohort."""

    scanned: int = 0
    unlinked: int = 0
    with_unresolved_joins: int = 0
    drifting: int = 0
    drifts: list[SalesOrderBillingDrift] = field(default_factory=list)

    def as_counts(self) -> dict[str, int]:
        return {
            "sales_orders_scanned": self.scanned,
            "sales_orders_unlinked_to_billing": self.unlinked,
            "sales_orders_with_unresolved_billing_joins": self.with_unresolved_joins,
            "sales_orders_drifting_from_billing": self.drifting,
        }


def scan_billing_shadow(
    db: Session, *, limit: int | None = None
) -> BillingShadowReport:
    """Compare every active sales order against the ledger. Read-only."""
    report = BillingShadowReport()
    query = (
        select(SalesOrder)
        .where(SalesOrder.is_active.is_(True))
        .order_by(SalesOrder.created_at, SalesOrder.id)
    )
    if limit is not None:
        query = query.limit(limit)

    for order in db.scalars(query).all():
        report.scanned += 1
        position = resolve_billing_position(db, order)
        if position.unresolved:
            report.with_unresolved_joins += 1
        if not position.invoice_ids and not position.payment_ids:
            report.unlinked += 1
            continue
        drifts = compare_with_stored(order, position)
        if drifts:
            report.drifting += 1
            report.drifts.extend(drifts)
    return report
