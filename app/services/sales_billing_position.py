"""The Sale → Money handoff: read a sales order's position from billing.

`sales.orders` currently *stores* `amount_paid`, `balance_due`,
`payment_status` and `paid_at`. Those are Money-pipeline facts held as
Sale-pipeline columns, derived by ad-hoc assignment rather than by the invoice
state machine, and reconciled against the ledger by nothing. One duplicated
boundary has already produced four separate money defects.

This module is the read side of the replacement, and the shadow phase of an
explicit authority migration. It **writes nothing to sales or billing state**
and refuses to repair — see ``SUPPORTS_APPLY``.

## Settlement is read through allocation, never through payment origin

An order-originated payment carries
``external_id = "crm:sales_order:{id}:payment"``, but that proves **origin, not
application**: ``_record_sales_order_payment`` deliberately charges the account
rather than one invoice, and the ledger auto-allocates across whatever is open.
Summing those payments would credit this sale with money that settled some
other obligation. Settlement is therefore computed from ``PaymentAllocation``
rows against the order's own invoices; the originating payments are carried
separately as provenance only.

## The obligation → document → application chain

The structural target, which foreign keys alone do not achieve:

    finite SalesOrder billing obligation
      → structurally linked Invoice / InvoiceLine
      → PaymentAllocation or credit application
      → Payment / settlement
      → refunds, reversals, credit notes and waivers

An invoice header FK is insufficient where one invoice combines several
sources: the relationship belongs at line or obligation level with defined
partial-allocation semantics, so that a recurring invoice descending from this
sale's subscription cannot inflate the original sale.

## Joining is currently metadata, not structure

  installation invoice   Project.metadata_["selfcare_installation_invoice_id"]
  subscription invoice   SalesOrderLine.metadata_["selfcare_subscription_invoice_id"]
  originating payment    Payment.external_id == "crm:sales_order:{id}:payment"

That is the known exception recorded in ``SALES_TO_SERVICE_LIFECYCLE_SOT.md``.
This module reads what exists today so the shadow phase can start without a
migration, and classifies every unsafe join so the structural slice is driven
by evidence.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentAllocation
from app.models.sales import SalesOrder, SalesOrderPaymentStatus, SalesOrderStatus
from app.models.sales_billing_shadow import (
    SALES_BILLING_SHADOW_CONTRACT_VERSION,
    SalesBillingShadowBucket,
    SalesBillingShadowRun,
)
from app.services.common import coerce_uuid, round_money

logger = logging.getLogger(__name__)

#: This check observes; it never repairs. A shared CLI that offers --apply must
#: read this and refuse, rather than silently no-op — a silent no-op implies an
#: authority this check deliberately lacks.
SUPPORTS_APPLY = False

_ZERO = Decimal("0.00")

_OPEN_INVOICE_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)

#: Buckets that must reach zero before the boundary can carry authority.
_BLOCKING_BUCKETS = frozenset(
    {
        SalesBillingShadowBucket.WAIVED_EVIDENCE_MISSING,
        SalesBillingShadowBucket.UNLINKED_UNEXPECTED,
        SalesBillingShadowBucket.UNRESOLVED_INVALID,
        SalesBillingShadowBucket.UNRESOLVED_MISSING,
        SalesBillingShadowBucket.UNRESOLVED_AMBIGUOUS,
        SalesBillingShadowBucket.DRIFTING,
    }
)


class ShadowCheckCannotRepair(RuntimeError):
    """Raised when the shadow check is asked to repair anything."""


@dataclass(frozen=True)
class SalesOrderBillingPosition:
    sales_order_id: UUID
    invoiced: Decimal = _ZERO
    #: Money actually applied to THIS order's invoices, via allocation.
    settled: Decimal = _ZERO
    open_balance: Decimal = _ZERO
    payment_status: str = SalesOrderPaymentStatus.pending.value
    invoice_ids: tuple[UUID, ...] = ()
    #: Payments that originated from this order. Provenance only — an
    #: originating payment may have settled a different obligation entirely.
    originating_payment_ids: tuple[UUID, ...] = ()
    invalid_joins: tuple[str, ...] = ()
    missing_joins: tuple[str, ...] = ()
    ambiguous_joins: tuple[str, ...] = ()


@dataclass(frozen=True)
class SalesOrderBillingDrift:
    sales_order_id: UUID
    field: str
    stored: str
    billing: str

    def __str__(self) -> str:  # pragma: no cover - operator display
        return (
            f"sales_order={self.sales_order_id} {self.field}: "
            f"stored={self.stored} billing={self.billing}"
        )


def _referenced_invoice_ids(
    sales_order: SalesOrder,
) -> tuple[list[UUID], list[str]]:
    """Invoice ids this order references, plus malformed references."""
    ids: list[UUID] = []
    invalid: list[str] = []

    def _take(raw: object, label: str) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        try:
            # coerce_uuid raises on a malformed value rather than returning
            # None; a metadata join carrying one is exactly the unsafe-join
            # evidence this phase exists to surface, not an error to propagate.
            resolved = coerce_uuid(text)
        except (ValueError, AttributeError, TypeError):
            resolved = None
        if resolved is None:
            invalid.append(f"{label}:{text}")
        else:
            ids.append(resolved)

    project = sales_order.project
    if project is not None and isinstance(project.metadata_, dict):
        _take(
            project.metadata_.get("selfcare_installation_invoice_id"),
            "installation_invoice",
        )

    for line in sales_order.lines or ():
        if not getattr(line, "is_active", True):
            continue
        meta = line.metadata_ if isinstance(line.metadata_, dict) else {}
        _take(meta.get("selfcare_subscription_invoice_id"), "subscription_invoice")

    seen: set[UUID] = set()
    unique = [i for i in ids if not (i in seen or seen.add(i))]
    return unique, invalid


def resolve_billing_position(
    db: Session,
    sales_order: SalesOrder,
    *,
    shared_invoice_ids: frozenset[UUID] = frozenset(),
) -> SalesOrderBillingPosition:
    """Read this order's money position from the ledger. Pure read.

    ``shared_invoice_ids`` are invoices reachable from more than one sales
    order — ambiguity the caller detects cohort-wide and passes down.
    """
    referenced, invalid = _referenced_invoice_ids(sales_order)

    invoiced = _ZERO
    open_balance = _ZERO
    found_ids: list[UUID] = []
    missing: list[str] = []
    ambiguous: list[str] = []

    if referenced:
        invoices = db.scalars(
            select(Invoice).where(
                Invoice.id.in_(referenced), Invoice.is_active.is_(True)
            )
        ).all()
        found = {invoice.id for invoice in invoices}
        missing.extend(f"invoice_missing:{i}" for i in referenced if i not in found)
        for invoice in invoices:
            if invoice.id in shared_invoice_ids:
                ambiguous.append(f"invoice_shared:{invoice.id}")
            found_ids.append(invoice.id)
            invoiced += Decimal(str(invoice.total or 0))
            if invoice.status in _OPEN_INVOICE_STATUSES:
                open_balance += Decimal(str(invoice.balance_due or 0))

    # Settlement = what was APPLIED to these invoices, not what this order
    # originated. See the module docstring.
    settled = _ZERO
    if found_ids:
        rows = db.execute(
            select(PaymentAllocation.amount).where(
                PaymentAllocation.invoice_id.in_(found_ids),
                PaymentAllocation.is_active.is_(True),
            )
        ).all()
        settled = sum((Decimal(str(row[0] or 0)) for row in rows), start=_ZERO)

    originating = db.scalars(
        select(Payment.id).where(
            Payment.external_id == f"crm:sales_order:{sales_order.id}:payment",
            Payment.is_active.is_(True),
        )
    ).all()

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
        originating_payment_ids=tuple(originating),
        invalid_joins=tuple(invalid),
        missing_joins=tuple(missing),
        ambiguous_joins=tuple(ambiguous),
    )


def _has_canonical_waiver_evidence(sales_order: SalesOrder) -> bool:
    """A waiver is only excluded from comparison if its owner wrote evidence."""
    metadata = sales_order.metadata_ if isinstance(sales_order.metadata_, dict) else {}
    waiver = metadata.get("waiver")
    if not isinstance(waiver, dict):
        return False
    return bool(
        str(waiver.get("waived_by") or "").strip()
        and str(waiver.get("reason") or "").strip()
        and str(waiver.get("waived_at") or "").strip()
    )


def _expects_billing(sales_order: SalesOrder) -> bool:
    """Whether this order should have billing artifacts by now."""
    if sales_order.status == SalesOrderStatus.draft.value:
        return False
    return Decimal(str(sales_order.total or 0)) > 0


def classify(
    sales_order: SalesOrder,
    position: SalesOrderBillingPosition,
) -> tuple[SalesBillingShadowBucket, list[SalesOrderBillingDrift]]:
    """Assign exactly one bucket, and any drift when the order is comparable.

    Order matters: unsafe joins are reported before comparison, because a
    comparison across a join we do not trust is not evidence of anything.
    """
    if sales_order.payment_status == SalesOrderPaymentStatus.waived.value:
        if _has_canonical_waiver_evidence(sales_order):
            return SalesBillingShadowBucket.WAIVED_EXCLUDED, []
        return SalesBillingShadowBucket.WAIVED_EVIDENCE_MISSING, []

    if position.invalid_joins:
        return SalesBillingShadowBucket.UNRESOLVED_INVALID, []
    if position.missing_joins:
        return SalesBillingShadowBucket.UNRESOLVED_MISSING, []
    if position.ambiguous_joins:
        return SalesBillingShadowBucket.UNRESOLVED_AMBIGUOUS, []

    if not position.invoice_ids:
        if _expects_billing(sales_order):
            return SalesBillingShadowBucket.UNLINKED_UNEXPECTED, []
        return SalesBillingShadowBucket.UNLINKED_EXPECTED, []

    drifts: list[SalesOrderBillingDrift] = []
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

    if drifts:
        return SalesBillingShadowBucket.DRIFTING, drifts
    return SalesBillingShadowBucket.AGREEING, []


@dataclass
class BillingShadowReport:
    scanned: int = 0
    buckets: Counter = field(default_factory=Counter)
    drifts: list[SalesOrderBillingDrift] = field(default_factory=list)
    cohort_fingerprint: str = ""
    contract_version: int = SALES_BILLING_SHADOW_CONTRACT_VERSION

    @property
    def clean(self) -> bool:
        """No blocking bucket has any member."""
        return not any(self.buckets.get(bucket, 0) for bucket in _BLOCKING_BUCKETS)

    def as_counts(self) -> dict[str, int]:
        counts = {
            f"sales_billing_shadow_{bucket.value}": self.buckets.get(bucket, 0)
            for bucket in SalesBillingShadowBucket
        }
        counts["sales_billing_shadow_scanned"] = self.scanned
        return counts

    def assert_exhaustive(self) -> None:
        """Every in-scope order must land in exactly one bucket.

        A silently unclassified order would shrink the denominator and make a
        dirty cohort look clean, so this fails the run instead.
        """
        total = sum(self.buckets.values())
        if total != self.scanned:
            raise RuntimeError(
                "Sale → Money shadow buckets are not exhaustive: "
                f"scanned={self.scanned} bucketed={total}"
            )


def _shared_invoice_ids(db: Session, orders: list[SalesOrder]) -> frozenset[UUID]:
    """Invoices reachable from more than one sales order.

    The installation-invoice path deliberately reuses an invoice across
    projects sharing a sales order or quote, so this is a real state — and an
    invoice that cannot be attributed to one obligation cannot carry the
    boundary.
    """
    owners: dict[UUID, set[UUID]] = defaultdict(set)
    for order in orders:
        referenced, _invalid = _referenced_invoice_ids(order)
        for invoice_id in referenced:
            owners[invoice_id].add(order.id)
    return frozenset(
        invoice_id for invoice_id, order_ids in owners.items() if len(order_ids) > 1
    )


def scan_billing_shadow(
    db: Session,
    *,
    apply: bool = False,
    persist: bool = True,
    actor_id: str | None = None,
) -> BillingShadowReport:
    """Observe the Sale → Money boundary. Never repairs.

    ``apply=True`` fails closed rather than silently doing nothing: a no-op
    would imply this check could repair if asked, and it cannot.
    """
    if apply:
        raise ShadowCheckCannotRepair(
            "The Sale → Money shadow check observes only (SUPPORTS_APPLY is "
            "False). Money repairs belong to their owner and need finance "
            "approval; a disagreement here does not establish which side is "
            "wrong."
        )

    orders = list(
        db.scalars(
            select(SalesOrder)
            .where(SalesOrder.is_active.is_(True))
            .order_by(SalesOrder.created_at, SalesOrder.id)
        ).all()
    )
    shared = _shared_invoice_ids(db, orders)

    report = BillingShadowReport()
    fingerprint = hashlib.sha256()
    for order in orders:
        report.scanned += 1
        position = resolve_billing_position(db, order, shared_invoice_ids=shared)
        bucket, drifts = classify(order, position)
        report.buckets[bucket] += 1
        report.drifts.extend(drifts)
        fingerprint.update(f"{order.id}:{bucket.value}\n".encode())

    report.cohort_fingerprint = fingerprint.hexdigest()
    report.assert_exhaustive()

    if persist:
        persist_run(db, report, actor_id=actor_id)
    return report


def persist_run(
    db: Session, report: BillingShadowReport, *, actor_id: str | None = None
) -> SalesBillingShadowRun:
    """Append one observation and commit it.

    Committed on its own rather than left for the caller: a sweep that rolls
    back its repair attempt must not be able to erase the observation it made
    beforehand. Evidence of what was true is not part of what was attempted.
    """
    run = SalesBillingShadowRun(
        contract_version=report.contract_version,
        cohort_fingerprint=report.cohort_fingerprint,
        scanned=report.scanned,
        bucket_counts={
            bucket.value: report.buckets.get(bucket, 0)
            for bucket in SalesBillingShadowBucket
        },
        clean=report.clean,
        actor_id=actor_id,
    )
    db.add(run)
    db.commit()
    return run


def consecutive_clean_runs(db: Session) -> int:
    """How many consecutive clean observations end the current window.

    Resets on any dirty run, and on a contract-version change: bucket semantics
    differing across runs means they are not comparable observations.
    """
    runs = db.scalars(
        select(SalesBillingShadowRun)
        .where(
            SalesBillingShadowRun.contract_version
            == SALES_BILLING_SHADOW_CONTRACT_VERSION
        )
        .order_by(
            SalesBillingShadowRun.observed_at.desc(), SalesBillingShadowRun.id.desc()
        )
    ).all()
    streak = 0
    for run in runs:
        if not run.clean:
            break
        streak += 1
    return streak
