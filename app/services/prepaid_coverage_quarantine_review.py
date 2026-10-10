"""Read-only finance review of blocking prepaid coverage quarantine.

``financial.prepaid_service_coverage_reconciliation`` quarantines an account
when its financial evidence cannot be projected safely. Two reason codes have
no automatic repair:

* ``malformed_paid_invoice_period`` — an active, fully settled PAID invoice
  with a positive active line linked to the subscription has a missing or
  non-positive ``billing_period_start``/``billing_period_end``;
* ``malformed_renewal_origin`` — an unreversed ``prepaid_service_renewal``
  adjustment (internet service, active debit) whose ``origin_ref`` is not an
  exact ``<subscription>:<start>:<end>`` with a positive period, or whose
  account/amount/currency disagree with its ledger debit.

This query explains such a quarantine record by record and names the existing
reviewed owner (if any) that may correct each one. A malformed paid-invoice
period is corrected by ``financial.prepaid_paid_invoice_period_repair``. It reuses the owner's own
predicates so it cannot drift from what enforcement blocks on. It never
writes, never infers a period from memo or description text, and never
decides the correction: it states which facts Finance must establish before a
named owner may act, or the precise engineering capability that is missing.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.admin_alert import AdminAlert, AlertStatus
from app.models.billing import (
    AccountAdjustment,
    CreditNoteApplication,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    PaymentAllocation,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingMode, Subscription
from app.services.billing.adjustments import AccountAdjustmentOrigin
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.common import round_money, to_decimal
from app.services.prepaid_coverage_reconciliation import (
    ENFORCEMENT_BLOCKING_QUARANTINE_REASONS,
    CoverageReconciliationDecision,
    CoverageReconciliationReason,
    is_malformed_paid_invoice_period,
    parse_prepaid_renewal_origin_ref,
    preview_prepaid_coverage_reconciliation,
    split_prepaid_renewal_origin_ref,
)

#: Must equal ``collections.scheduled.PREPAID_COVERAGE_QUARANTINE_FINDING_PREFIX``
#: (asserted by tests); duplicated so this query does not import the runner.
QUARANTINE_FINDING_PREFIX = "prepaid-coverage:quarantine:"
RUNBOOK = "docs/runbooks/PREPAID_COVERAGE_QUARANTINE_FINANCE_REVIEW.md"
_UNUSED_RENEWAL_RUNBOOK = "docs/runbooks/UNUSED_PREPAID_RENEWAL_CORRECTION.md"
_PERIOD_REPAIR_OWNER = "financial.prepaid_paid_invoice_period_repair"
_PERIOD_REPAIR_CLI = "scripts.billing.repair_prepaid_paid_invoice_period"
_RENEWAL_ORIGIN = AccountAdjustmentOrigin.prepaid_service_renewal
_BASE_LINE_KIND = "base_subscription"
_DERIVED_LINE_PERIOD_SOURCES = frozenset({"paid_at_manual_invoice"})


class InvoicePeriodDefect(StrEnum):
    missing_start_and_end = "missing_start_and_end"
    missing_start = "missing_start"
    missing_end = "missing_end"
    end_not_after_start = "end_not_after_start"


class RenewalOriginDefect(StrEnum):
    origin_ref_missing = "origin_ref_missing"
    origin_ref_unparseable = "origin_ref_unparseable"
    origin_period_not_positive = "origin_period_not_positive"
    ledger_account_mismatch = "ledger_account_mismatch"
    ledger_amount_mismatch = "ledger_amount_mismatch"
    ledger_currency_mismatch = "ledger_currency_mismatch"


class PeriodProof(StrEnum):
    """What structured evidence (never memo text) says the period was."""

    source_entitlement = "source_entitlement"
    line_metadata_period = "line_metadata_period"
    derived_line_metadata_period = "derived_line_metadata_period"
    conflicting = "conflicting"
    none = "none"


class ResolutionRoute(StrEnum):
    unused_prepaid_renewal_correction = "unused_prepaid_renewal_correction"
    reviewed_account_adjustment_reversal = "reviewed_account_adjustment_reversal"
    reviewed_paid_invoice_period_repair = "reviewed_paid_invoice_period_repair"
    engineering_non_service_line_classification = (
        "engineering_non_service_line_classification"
    )
    engineering_renewal_origin_correction = "engineering_renewal_origin_correction"
    engineering_adjustment_ledger_repair = "engineering_adjustment_ledger_repair"


@dataclass(frozen=True, slots=True)
class PrepaidCoverageQuarantineReviewQuery:
    """Accounts to explain; ``None`` selects every open quarantine work item."""

    account_ids: tuple[UUID, ...] | None = None
    as_of: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResolutionOption:
    """One conditional path; Finance must establish ``when`` before using it."""

    route: ResolutionRoute
    sanctioned: bool
    when: str
    owner: str | None
    runbook: str | None
    command: str | None
    missing_capability: str | None


@dataclass(frozen=True, slots=True)
class EntitlementEvidence:
    entitlement_id: UUID
    subscription_id: UUID
    account_id: UUID
    status: str
    starts_at: datetime
    ends_at: datetime
    amount_funded: Decimal
    currency: str
    source_invoice_id: UUID | None
    source_invoice_line_id: UUID | None
    source_ledger_entry_id: UUID | None


@dataclass(frozen=True, slots=True)
class InvoiceLineEvidence:
    line_id: UUID
    subscription_id: UUID | None
    amount: Decimal
    quantity: Decimal
    is_active: bool
    line_kind: str | None
    in_quarantine_scope: bool
    metadata_period_start: datetime | None
    metadata_period_end: datetime | None
    metadata_period_source: str | None
    source_entitlements: tuple[EntitlementEvidence, ...]


@dataclass(frozen=True, slots=True)
class InvoicePeriodFinding:
    invoice_id: UUID
    invoice_number: str | None
    account_id: UUID
    status: str
    is_proforma: bool
    splynx_invoice_id: int | None
    currency: str
    total: Decimal
    balance_due: Decimal
    allocated_payments: Decimal
    applied_credit_notes: Decimal
    issued_at: datetime | None
    paid_at: datetime | None
    billing_period_start: datetime | None
    billing_period_end: datetime | None
    defect: InvoicePeriodDefect
    subscription_ids: tuple[UUID, ...]
    lines: tuple[InvoiceLineEvidence, ...]
    period_proof: PeriodProof
    proven_period_start: datetime | None
    proven_period_end: datetime | None
    options: tuple[ResolutionOption, ...]

    @property
    def sanctioned_repair_available(self) -> bool:
        return any(option.sanctioned for option in self.options)


@dataclass(frozen=True, slots=True)
class LedgerComparison:
    ledger_entry_id: UUID
    account_id: UUID
    amount: Decimal
    currency: str
    entry_type: str
    source: str | None
    invoice_id: UUID | None
    is_active: bool


@dataclass(frozen=True, slots=True)
class RenewalOriginFinding:
    adjustment_id: UUID
    account_id: UUID
    origin_ref: str | None
    amount: Decimal
    currency: str
    created_at: datetime | None
    parsed_subscription_id: UUID | None
    parsed_starts_at: datetime | None
    parsed_ends_at: datetime | None
    defects: tuple[RenewalOriginDefect, ...]
    ledger: LedgerComparison
    linked_entitlements: tuple[EntitlementEvidence, ...]
    options: tuple[ResolutionOption, ...]

    @property
    def sanctioned_repair_available(self) -> bool:
        return any(option.sanctioned for option in self.options)


@dataclass(frozen=True, slots=True)
class SubscriptionQuarantineState:
    subscription_id: UUID
    decision: CoverageReconciliationDecision
    reason: CoverageReconciliationReason


@dataclass(frozen=True, slots=True)
class QuarantineWorkItem:
    fingerprint: str
    status: str
    reason_codes: tuple[str, ...]
    sla_due_at: str | None
    first_seen_at: datetime | None
    last_seen_at: datetime | None


@dataclass(frozen=True, slots=True)
class AccountQuarantineReview:
    account_id: UUID
    work_item: QuarantineWorkItem | None
    subscriptions: tuple[SubscriptionQuarantineState, ...]
    invoice_findings: tuple[InvoicePeriodFinding, ...]
    renewal_origin_findings: tuple[RenewalOriginFinding, ...]

    @property
    def blocking_reasons(self) -> tuple[CoverageReconciliationReason, ...]:
        """Current quarantine reasons that keep the finance work item open."""
        return tuple(
            sorted(
                {
                    state.reason
                    for state in self.subscriptions
                    if state.decision == CoverageReconciliationDecision.quarantined
                    and state.reason in ENFORCEMENT_BLOCKING_QUARANTINE_REASONS
                },
                key=lambda value: value.value,
            )
        )

    @property
    def finding_count(self) -> int:
        return len(self.invoice_findings) + len(self.renewal_origin_findings)


@dataclass(frozen=True, slots=True)
class PrepaidCoverageQuarantineReview:
    as_of: datetime
    runbook: str
    accounts: tuple[AccountQuarantineReview, ...]

    @property
    def finding_count(self) -> int:
        return sum(account.finding_count for account in self.accounts)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return _utc(value) if value is not None else None


def _metadata_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _invoice_defect(
    starts_at: datetime | None, ends_at: datetime | None
) -> InvoicePeriodDefect:
    if starts_at is None and ends_at is None:
        return InvoicePeriodDefect.missing_start_and_end
    if starts_at is None:
        return InvoicePeriodDefect.missing_start
    if ends_at is None:
        return InvoicePeriodDefect.missing_end
    return InvoicePeriodDefect.end_not_after_start


def _entitlement_evidence(row: ServiceEntitlement) -> EntitlementEvidence:
    status = row.status
    return EntitlementEvidence(
        entitlement_id=row.id,
        subscription_id=row.subscription_id,
        account_id=row.account_id,
        status=status.value if isinstance(status, ServiceEntitlementStatus) else "",
        starts_at=_utc(row.starts_at),
        ends_at=_utc(row.ends_at),
        amount_funded=round_money(to_decimal(row.amount_funded)),
        currency=row.currency,
        source_invoice_id=row.source_invoice_id,
        source_invoice_line_id=row.source_invoice_line_id,
        source_ledger_entry_id=row.source_ledger_entry_id,
    )


def _open_work_items(
    db: Session, account_ids: tuple[UUID, ...] | None
) -> dict[UUID, QuarantineWorkItem]:
    statement = select(AdminAlert).where(
        AdminAlert.fingerprint.like(f"{QUARANTINE_FINDING_PREFIX}%"),
        AdminAlert.status != AlertStatus.resolved,
    )
    if account_ids is not None:
        statement = statement.where(
            AdminAlert.fingerprint.in_(
                [f"{QUARANTINE_FINDING_PREFIX}{value}" for value in account_ids]
            )
        )
    items: dict[UUID, QuarantineWorkItem] = {}
    for alert in db.scalars(statement.order_by(AdminAlert.fingerprint)).all():
        try:
            account_id = UUID(alert.fingerprint[len(QUARANTINE_FINDING_PREFIX) :])
        except ValueError:
            continue
        details = alert.details if isinstance(alert.details, dict) else {}
        reasons = details.get("reason_codes")
        sla_due_at = details.get("sla_due_at")
        items[account_id] = QuarantineWorkItem(
            fingerprint=alert.fingerprint,
            status=alert.status.value,
            reason_codes=tuple(
                sorted(str(value) for value in reasons)
                if isinstance(reasons, list)
                else ()
            ),
            sla_due_at=str(sla_due_at) if sla_due_at else None,
            first_seen_at=_optional_utc(alert.first_seen_at),
            last_seen_at=_optional_utc(alert.last_seen_at),
        )
    return items


def _collectible_prepaid_subscriptions(
    db: Session, account_id: UUID
) -> list[Subscription]:
    return list(
        db.scalars(
            select(Subscription)
            .where(
                Subscription.subscriber_id == account_id,
                Subscription.billing_mode == BillingMode.prepaid,
                Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES),
            )
            .order_by(Subscription.id)
        ).all()
    )


def _line_period_proof(
    lines: list[InvoiceLineEvidence],
) -> tuple[PeriodProof, datetime | None, datetime | None]:
    """Classify structured period evidence for the in-scope lines only."""
    scoped = [line for line in lines if line.in_quarantine_scope]
    entitlement_periods: set[tuple[datetime, datetime]] = set()
    entitlement_complete = bool(scoped)
    for line in scoped:
        active = [
            row
            for row in line.source_entitlements
            if row.status == ServiceEntitlementStatus.active.value
            and row.subscription_id == line.subscription_id
            and row.ends_at > row.starts_at
        ]
        if len(active) != 1:
            entitlement_complete = False
            continue
        entitlement_periods.add((active[0].starts_at, active[0].ends_at))

    metadata_periods: set[tuple[datetime, datetime]] = set()
    metadata_complete = bool(scoped)
    metadata_derived = False
    for line in scoped:
        start, end = line.metadata_period_start, line.metadata_period_end
        if start is None or end is None or end <= start:
            metadata_complete = False
            continue
        metadata_periods.add((start, end))
        if line.metadata_period_source in _DERIVED_LINE_PERIOD_SOURCES:
            metadata_derived = True

    if len(entitlement_periods) > 1 or len(metadata_periods) > 1:
        return PeriodProof.conflicting, None, None
    if (
        entitlement_periods
        and metadata_periods
        and (entitlement_periods != metadata_periods)
    ):
        return PeriodProof.conflicting, None, None
    if entitlement_complete and len(entitlement_periods) == 1:
        start, end = next(iter(entitlement_periods))
        return PeriodProof.source_entitlement, start, end
    if metadata_complete and len(metadata_periods) == 1:
        start, end = next(iter(metadata_periods))
        if metadata_derived:
            return PeriodProof.derived_line_metadata_period, start, end
        return PeriodProof.line_metadata_period, start, end
    return PeriodProof.none, None, None


def _period_repair_command(
    *,
    invoice_id: UUID,
    lines: list[InvoiceLineEvidence],
    proven_start: datetime | None,
    proven_end: datetime | None,
) -> str:
    """Prefilled READ-ONLY preview of the reviewed period repair."""
    scoped = [line for line in lines if line.in_quarantine_scope]
    line_id = str(scoped[0].line_id) if len(scoped) == 1 else "<line-id>"
    subscription_id = (
        str(scoped[0].subscription_id)
        if len(scoped) == 1 and scoped[0].subscription_id is not None
        else "<subscription-id>"
    )
    start = (
        proven_start.isoformat()
        if proven_start is not None
        else "<finance-documented-start>"
    )
    end = (
        proven_end.isoformat() if proven_end is not None else "<finance-documented-end>"
    )
    return (
        f"poetry run python -m {_PERIOD_REPAIR_CLI} preview "
        f"--invoice-id {invoice_id} --line-id {line_id} "
        f"--subscription-id {subscription_id} "
        f"--period-start {start} --period-end {end}"
    )


def _invoice_options(
    *,
    invoice_id: UUID,
    lines: list[InvoiceLineEvidence],
    proof: PeriodProof,
    proven_start: datetime | None,
    proven_end: datetime | None,
) -> tuple[ResolutionOption, ...]:
    options: list[ResolutionOption] = []
    scoped = [line for line in lines if line.in_quarantine_scope]
    if any(line.line_kind != _BASE_LINE_KIND for line in scoped):
        options.append(
            ResolutionOption(
                route=ResolutionRoute.engineering_non_service_line_classification,
                sanctioned=False,
                when=(
                    "Finance confirms from source documents that a scoped line is "
                    "a one-off or non-service charge (it never bought a service "
                    "period) even though it is linked to the subscription."
                ),
                owner="financial.prepaid_service_coverage_reconciliation",
                runbook=RUNBOOK,
                command=None,
                missing_capability=(
                    "No reviewed owner can mark a paid, subscription-linked, "
                    "positive invoice line as non-service evidence; the coverage "
                    "predicate treats every such line as a service period. Needs "
                    "an engineering change (reviewed line classification with "
                    "four-eyes approval and provenance)."
                ),
            )
        )
    proven = proof in {PeriodProof.source_entitlement, PeriodProof.line_metadata_period}
    options.append(
        ResolutionOption(
            route=ResolutionRoute.reviewed_paid_invoice_period_repair,
            sanctioned=True,
            when=(
                (
                    "Finance confirms the structured period shown in "
                    "proven_period_start/proven_period_end is the period this "
                    "invoice paid for."
                )
                if proven
                else (
                    "No single structured period exists (proof="
                    f"{proof.value}); Finance determines the paid period and "
                    "subscription from source documents (original or Splynx "
                    "invoice, payment receipt, customer order), never from memo "
                    "or description text."
                )
            ),
            owner=_PERIOD_REPAIR_OWNER,
            runbook=RUNBOOK,
            command=_period_repair_command(
                invoice_id=invoice_id,
                lines=lines,
                proven_start=proven_start if proven else None,
                proven_end=proven_end if proven else None,
            ),
            missing_capability=None,
        )
    )
    return tuple(options)


def _invoice_findings(
    db: Session,
    subscriptions: list[Subscription],
) -> tuple[InvoicePeriodFinding, ...]:
    subscription_ids = {subscription.id for subscription in subscriptions}
    if not subscription_ids:
        return ()
    # Exactly the owner's evidence selection (see _paid_invoice_evidence).
    rows = db.execute(
        select(InvoiceLine.id, Invoice)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(
            InvoiceLine.subscription_id.in_(subscription_ids),
            InvoiceLine.is_active.is_(True),
            InvoiceLine.amount > Decimal("0.00"),
            Invoice.is_active.is_(True),
            Invoice.status == InvoiceStatus.paid,
            Invoice.balance_due <= Decimal("0.00"),
        )
    ).all()
    scoped_lines: dict[UUID, set[UUID]] = defaultdict(set)
    invoices: dict[UUID, Invoice] = {}
    for line_id, invoice in rows:
        if not is_malformed_paid_invoice_period(
            invoice.billing_period_start, invoice.billing_period_end
        ):
            continue
        invoices[invoice.id] = invoice
        scoped_lines[invoice.id].add(line_id)
    if not invoices:
        return ()

    invoice_ids = sorted(invoices, key=str)
    all_lines = db.scalars(
        select(InvoiceLine)
        .where(InvoiceLine.invoice_id.in_(invoice_ids))
        .order_by(InvoiceLine.invoice_id, InvoiceLine.created_at, InvoiceLine.id)
    ).all()
    line_ids = [line.id for line in all_lines]
    entitlements_by_line: dict[UUID, list[EntitlementEvidence]] = defaultdict(list)
    for row in db.scalars(
        select(ServiceEntitlement)
        .where(ServiceEntitlement.source_invoice_line_id.in_(line_ids))
        .order_by(ServiceEntitlement.starts_at, ServiceEntitlement.id)
    ).all():
        if row.source_invoice_line_id is not None:
            entitlements_by_line[row.source_invoice_line_id].append(
                _entitlement_evidence(row)
            )
    allocated: dict[UUID, Decimal] = {
        row_invoice_id: round_money(to_decimal(total or 0))
        for row_invoice_id, total in db.execute(
            select(PaymentAllocation.invoice_id, func.sum(PaymentAllocation.amount))
            .where(
                PaymentAllocation.invoice_id.in_(invoice_ids),
                PaymentAllocation.is_active.is_(True),
                PaymentAllocation.reversed_at.is_(None),
            )
            .group_by(PaymentAllocation.invoice_id)
        ).all()
    }
    credited: dict[UUID, Decimal] = {
        row_invoice_id: round_money(to_decimal(total or 0))
        for row_invoice_id, total in db.execute(
            select(
                CreditNoteApplication.invoice_id,
                func.sum(CreditNoteApplication.amount),
            )
            .where(CreditNoteApplication.invoice_id.in_(invoice_ids))
            .group_by(CreditNoteApplication.invoice_id)
        ).all()
    }

    lines_by_invoice: dict[UUID, list[InvoiceLineEvidence]] = defaultdict(list)
    for line in all_lines:
        metadata = line.metadata_ if isinstance(line.metadata_, dict) else {}
        kind = metadata.get("kind")
        source = metadata.get("billing_period_source")
        lines_by_invoice[line.invoice_id].append(
            InvoiceLineEvidence(
                line_id=line.id,
                subscription_id=line.subscription_id,
                amount=round_money(to_decimal(line.amount)),
                quantity=to_decimal(line.quantity),
                is_active=bool(line.is_active),
                line_kind=str(kind) if kind else None,
                in_quarantine_scope=line.id in scoped_lines[line.invoice_id],
                metadata_period_start=_metadata_datetime(
                    metadata.get("billing_period_start")
                ),
                metadata_period_end=_metadata_datetime(
                    metadata.get("billing_period_end")
                ),
                metadata_period_source=str(source) if source else None,
                source_entitlements=tuple(entitlements_by_line.get(line.id, ())),
            )
        )

    findings: list[InvoicePeriodFinding] = []
    for invoice_id in invoice_ids:
        invoice = invoices[invoice_id]
        lines = lines_by_invoice[invoice_id]
        proof, proven_start, proven_end = _line_period_proof(lines)
        findings.append(
            InvoicePeriodFinding(
                invoice_id=invoice.id,
                invoice_number=invoice.invoice_number,
                account_id=invoice.account_id,
                status=invoice.status.value,
                is_proforma=bool(invoice.is_proforma),
                splynx_invoice_id=invoice.splynx_invoice_id,
                currency=(invoice.currency or "NGN").upper(),
                total=round_money(to_decimal(invoice.total)),
                balance_due=round_money(to_decimal(invoice.balance_due)),
                allocated_payments=round_money(
                    allocated.get(invoice_id, Decimal("0.00"))
                ),
                applied_credit_notes=round_money(
                    credited.get(invoice_id, Decimal("0.00"))
                ),
                issued_at=_optional_utc(invoice.issued_at),
                paid_at=_optional_utc(invoice.paid_at),
                billing_period_start=_optional_utc(invoice.billing_period_start),
                billing_period_end=_optional_utc(invoice.billing_period_end),
                defect=_invoice_defect(
                    invoice.billing_period_start, invoice.billing_period_end
                ),
                subscription_ids=tuple(
                    sorted(
                        {
                            line.subscription_id
                            for line in lines
                            if line.in_quarantine_scope
                            and line.subscription_id is not None
                        },
                        key=str,
                    )
                ),
                lines=tuple(lines),
                period_proof=proof,
                proven_period_start=proven_start,
                proven_period_end=proven_end,
                options=_invoice_options(
                    invoice_id=invoice.id,
                    lines=lines,
                    proof=proof,
                    proven_start=proven_start,
                    proven_end=proven_end,
                ),
            )
        )
    return tuple(findings)


def _renewal_defects(
    db: Session,
    adjustment: AccountAdjustment,
    ledger: LedgerEntry,
) -> tuple[
    tuple[RenewalOriginDefect, ...], UUID | None, datetime | None, datetime | None
]:
    parsed = parse_prepaid_renewal_origin_ref(adjustment.origin_ref)
    if parsed is None:
        if not (adjustment.origin_ref or "").strip():
            return (RenewalOriginDefect.origin_ref_missing,), None, None, None
        parts = split_prepaid_renewal_origin_ref(adjustment.origin_ref)
        if parts is None:
            return (RenewalOriginDefect.origin_ref_unparseable,), None, None, None
        try:
            parsed_subscription = UUID(parts[0])
            parsed_start = _utc(datetime.fromisoformat(parts[1].replace("Z", "+00:00")))
            parsed_end = _utc(datetime.fromisoformat(parts[2].replace("Z", "+00:00")))
        except ValueError:
            return (RenewalOriginDefect.origin_ref_unparseable,), None, None, None
        return (
            (RenewalOriginDefect.origin_period_not_positive,),
            parsed_subscription,
            parsed_start,
            parsed_end,
        )

    subscription_id, starts_at, ends_at = parsed
    # The owner applies the ledger comparison only to an origin naming a
    # collectible prepaid subscription (the full-cohort candidate set).
    candidate = db.scalar(
        select(Subscription.id).where(
            Subscription.id == subscription_id,
            Subscription.billing_mode == BillingMode.prepaid,
            Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES),
        )
    )
    defects: list[RenewalOriginDefect] = []
    if candidate is not None:
        if adjustment.account_id != ledger.account_id:
            defects.append(RenewalOriginDefect.ledger_account_mismatch)
        if round_money(to_decimal(adjustment.amount)) != round_money(
            to_decimal(ledger.amount)
        ):
            defects.append(RenewalOriginDefect.ledger_amount_mismatch)
        if adjustment.currency != ledger.currency:
            defects.append(RenewalOriginDefect.ledger_currency_mismatch)
    return tuple(defects), subscription_id, starts_at, ends_at


def _renewal_options(
    *,
    adjustment: AccountAdjustment,
    ledger: LedgerEntry,
    defects: tuple[RenewalOriginDefect, ...],
    linked: tuple[EntitlementEvidence, ...],
) -> tuple[ResolutionOption, ...]:
    ledger_defects = {
        RenewalOriginDefect.ledger_account_mismatch,
        RenewalOriginDefect.ledger_amount_mismatch,
        RenewalOriginDefect.ledger_currency_mismatch,
    }
    if ledger_defects.intersection(defects):
        return (
            ResolutionOption(
                route=ResolutionRoute.engineering_adjustment_ledger_repair,
                sanctioned=False,
                when="Always: the adjustment and its ledger debit disagree.",
                owner="financial.account_adjustments",
                runbook=RUNBOOK,
                command=None,
                missing_capability=(
                    "The adjustment's account, amount, or currency differs from "
                    "its ledger debit. Generic reversal and the unused-renewal "
                    "correction both refuse incomplete or inconsistent debit "
                    "evidence, and no owner rewrites either side. Needs "
                    "engineering investigation of how the pair diverged."
                ),
            ),
        )
    adjustment_debit = (
        ledger.invoice_id is None and ledger.source == LedgerSource.adjustment
    )
    amount = round_money(to_decimal(adjustment.amount))
    currency = str(adjustment.currency or "NGN").upper()
    # The unused-renewal owner requires exactly this linked pair.
    active = [
        row
        for row in linked
        if row.status == ServiceEntitlementStatus.active.value
        and row.account_id == adjustment.account_id
        and row.source_invoice_id is None
        and row.source_invoice_line_id is None
        and row.amount_funded == amount
        and row.currency == currency
    ]
    options: list[ResolutionOption] = []
    if len(active) == 1 and len(linked) == 1 and adjustment_debit:
        entitlement = active[0]
        options.append(
            ResolutionOption(
                route=ResolutionRoute.unused_prepaid_renewal_correction,
                sanctioned=True,
                when=(
                    "Finance confirms the customer did NOT receive service for "
                    f"{entitlement.starts_at.isoformat()} to "
                    f"{entitlement.ends_at.isoformat()} and approves returning "
                    "the debit to prepaid funding."
                ),
                owner="financial.prepaid_service_renewals",
                runbook=_UNUSED_RENEWAL_RUNBOOK,
                command=(
                    "poetry run python -m scripts.billing.billing_target_shadow "
                    "preview-unused-prepaid-renewal-correction "
                    f"--account {adjustment.account_id} "
                    f"--subscription {entitlement.subscription_id} "
                    f"--adjustment {adjustment.id} "
                    f"--entitlement {entitlement.entitlement_id}"
                ),
                missing_capability=None,
            )
        )
        options.append(
            ResolutionOption(
                route=ResolutionRoute.engineering_renewal_origin_correction,
                sanctioned=False,
                when=(
                    "Finance confirms the service WAS delivered, so the debit is "
                    "legitimate and only its origin reference is malformed. The "
                    "linked entitlement structurally proves "
                    f"{entitlement.subscription_id}:"
                    f"{entitlement.starts_at.isoformat()}:"
                    f"{entitlement.ends_at.isoformat()}."
                ),
                owner=None,
                runbook=RUNBOOK,
                command=None,
                missing_capability=(
                    "No reviewed owner corrects origin_ref on an existing "
                    "renewal adjustment; the legacy tax-invoice correction "
                    "requires an already exact origin_ref. Needs an "
                    "engineering-built reviewed origin correction from the "
                    "linked entitlement."
                ),
            )
        )
        return tuple(options)
    if not linked and adjustment_debit:
        options.append(
            ResolutionOption(
                route=ResolutionRoute.reviewed_account_adjustment_reversal,
                sanctioned=True,
                when=(
                    "Finance confirms the debit was raised in error (no service "
                    "period was bought) and approves returning it to prepaid "
                    "funding; no entitlement is linked to this debit."
                ),
                owner="financial.account_adjustments",
                runbook=RUNBOOK,
                command=(
                    f"POST /api/v1/account-adjustments/{adjustment.id}"
                    "/reversal/preview then /reversal (billing:ledger:write)"
                ),
                missing_capability=None,
            )
        )
    options.append(
        ResolutionOption(
            route=ResolutionRoute.engineering_renewal_origin_correction,
            sanctioned=False,
            when=(
                "The debit is legitimate (or the linked evidence is not exactly "
                "one active non-invoice entitlement), so the period cannot be "
                "proven from structured data."
            ),
            owner=None,
            runbook=RUNBOOK,
            command=None,
            missing_capability=(
                "No reviewed owner records a Finance-documented period for a "
                "renewal adjustment whose origin_ref is malformed and whose "
                "period is not proven by exactly one linked entitlement. Needs "
                "engineering; the account stays quarantined meanwhile."
            ),
        )
    )
    return tuple(options)


def _renewal_origin_findings(
    db: Session, account_id: UUID
) -> tuple[RenewalOriginFinding, ...]:
    # Exactly the owner's adjustment selection (see _adjustment_evidence).
    rows = db.execute(
        select(AccountAdjustment, LedgerEntry)
        .join(LedgerEntry, LedgerEntry.id == AccountAdjustment.ledger_entry_id)
        .where(
            AccountAdjustment.account_id == account_id,
            AccountAdjustment.origin == _RENEWAL_ORIGIN,
            AccountAdjustment.reversed_at.is_(None),
            AccountAdjustment.category == LedgerCategory.internet_service,
            LedgerEntry.is_active.is_(True),
            LedgerEntry.entry_type == LedgerEntryType.debit,
        )
        .order_by(AccountAdjustment.created_at, AccountAdjustment.id)
    ).all()
    findings: list[RenewalOriginFinding] = []
    for adjustment, ledger in rows:
        defects, subscription_id, starts_at, ends_at = _renewal_defects(
            db, adjustment, ledger
        )
        if not defects:
            continue
        linked = tuple(
            _entitlement_evidence(row)
            for row in db.scalars(
                select(ServiceEntitlement)
                .where(ServiceEntitlement.source_ledger_entry_id == ledger.id)
                .order_by(ServiceEntitlement.starts_at, ServiceEntitlement.id)
            ).all()
        )
        findings.append(
            RenewalOriginFinding(
                adjustment_id=adjustment.id,
                account_id=adjustment.account_id,
                origin_ref=adjustment.origin_ref,
                amount=round_money(to_decimal(adjustment.amount)),
                currency=adjustment.currency,
                created_at=_optional_utc(adjustment.created_at),
                parsed_subscription_id=subscription_id,
                parsed_starts_at=starts_at,
                parsed_ends_at=ends_at,
                defects=defects,
                ledger=LedgerComparison(
                    ledger_entry_id=ledger.id,
                    account_id=ledger.account_id,
                    amount=round_money(to_decimal(ledger.amount)),
                    currency=ledger.currency,
                    entry_type=ledger.entry_type.value,
                    source=ledger.source.value if ledger.source else None,
                    invoice_id=ledger.invoice_id,
                    is_active=bool(ledger.is_active),
                ),
                linked_entitlements=linked,
                options=_renewal_options(
                    adjustment=adjustment,
                    ledger=ledger,
                    defects=defects,
                    linked=linked,
                ),
            )
        )
    return tuple(findings)


def review_prepaid_coverage_quarantine(
    db: Session,
    query: PrepaidCoverageQuarantineReviewQuery,
) -> PrepaidCoverageQuarantineReview:
    """Explain blocking quarantine per account without changing any state."""
    observed_at = _utc(query.as_of or datetime.now(UTC))
    work_items = _open_work_items(db, query.account_ids)
    account_ids = (
        tuple(sorted(set(query.account_ids), key=str))
        if query.account_ids is not None
        else tuple(sorted(work_items, key=str))
    )
    accounts: list[AccountQuarantineReview] = []
    for account_id in account_ids:
        subscriptions = _collectible_prepaid_subscriptions(db, account_id)
        states: tuple[SubscriptionQuarantineState, ...] = ()
        if subscriptions:
            preview = preview_prepaid_coverage_reconciliation(
                db,
                as_of=observed_at,
                subscription_ids=tuple(row.id for row in subscriptions),
            )
            states = tuple(
                SubscriptionQuarantineState(
                    subscription_id=item.subscription_id,
                    decision=item.decision,
                    reason=item.reason,
                )
                for item in preview.items
            )
        accounts.append(
            AccountQuarantineReview(
                account_id=account_id,
                work_item=work_items.get(account_id),
                subscriptions=states,
                invoice_findings=_invoice_findings(db, subscriptions),
                renewal_origin_findings=_renewal_origin_findings(db, account_id),
            )
        )
    return PrepaidCoverageQuarantineReview(
        as_of=observed_at,
        runbook=RUNBOOK,
        accounts=tuple(accounts),
    )


__all__ = [
    "AccountQuarantineReview",
    "EntitlementEvidence",
    "InvoiceLineEvidence",
    "InvoicePeriodDefect",
    "InvoicePeriodFinding",
    "LedgerComparison",
    "PeriodProof",
    "PrepaidCoverageQuarantineReview",
    "PrepaidCoverageQuarantineReviewQuery",
    "QUARANTINE_FINDING_PREFIX",
    "QuarantineWorkItem",
    "RUNBOOK",
    "RenewalOriginDefect",
    "RenewalOriginFinding",
    "ResolutionOption",
    "ResolutionRoute",
    "SubscriptionQuarantineState",
    "review_prepaid_coverage_quarantine",
]
