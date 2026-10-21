"""Finance-reviewed repair of a paid prepaid invoice's service period.

``financial.prepaid_service_coverage_reconciliation`` quarantines a prepaid
subscription with ``malformed_paid_invoice_period`` when an active, fully
settled PAID invoice with a positive line linked to it has a missing or
non-positive ``billing_period_start``/``billing_period_end``. Nothing in the
system can tell which service period that money bought, so enforcement may
not act on the account until Finance states it.

This owner is the sanctioned way to record that statement. It follows the
repository's four-eyes pattern for financial corrections (see
``financial.prepaid_renewal_terms_backfill``'s reviewed record):

1. ``preview_paid_invoice_period_repair`` (read-only) validates the operator's
   proposed subscription and period against the invoice, settlement, currency,
   subscription terms, and existing entitlements. It shows the before/after
   documentary state, the entitlement the entitlement writer would create (or
   the existing one that already funds the payment), the projected effect on
   the quarantine work item, every blocker and warning, and a fingerprint.
2. ``request_paid_invoice_period_repair`` records one staff member's proposal,
   bound to that fingerprint, with a reason and an evidence reference plus
   SHA-256. It writes record-only evidence and changes no invoice.
3. ``approve_paid_invoice_period_repair`` is a different staff member's
   approval. Under account, subscription, invoice, line, and entitlement locks
   it recomputes the preview, requires the identical fingerprint, writes the
   period through the invoice owner's flush-only participant, creates the
   entitlement only through the existing paid-line entitlement writer, and
   stages audit and a domain event in the same transaction.

It never changes money, status, allocations, or ledger facts, never reads memo
or description text as evidence, and never decides the period: the operator
supplies it and the owner refuses anything it cannot prove consistent.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import NoReturn
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.admin_alert import AdminAlert, AlertStatus
from app.models.audit import AuditActorType
from app.models.billing import (
    CreditNoteApplication,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    PaymentAllocation,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingMode, Subscription
from app.models.event_store import EventStore
from app.models.subscriber import Subscriber
from app.schemas.audit import AuditEventCreate
from app.services.audit import AuditEvents
from app.services.billing.invoices import (
    InvoiceOwnerError,
    Invoices,
    ReviewedPaidPrepaidInvoicePeriodRestoration,
)
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.common import round_money, to_decimal
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.prepaid_coverage_reconciliation import (
    ENFORCEMENT_BLOCKING_QUARANTINE_REASONS,
    CoverageReconciliationDecision,
    CoverageReconciliationReason,
    is_malformed_paid_invoice_period,
    malformed_paid_invoice_ids_by_subscription,
    malformed_prepaid_renewal_origin_account_ids,
    preview_prepaid_coverage_reconciliation,
)
from app.services.sole_approver_exception import (
    SoleApproverExceptionGrant,
    authorize_sole_approver,
    stage_sole_approver_exception_audit,
)

logger = logging.getLogger(__name__)

OWNER = "financial.prepaid_paid_invoice_period_repair"
CONCERN = "finance-reviewed paid prepaid invoice period repair"
#: The existing narrowly scoped grant for repairing one already-paid prepaid
#: invoice's identity and coverage (seeded by migration 597). Requester and
#: approver must both hold it and must be different active staff.
REPAIR_PERMISSION = "billing:prepaid_reconciliation:repair"
RUNBOOK = "docs/runbooks/PREPAID_COVERAGE_QUARANTINE_FINANCE_REVIEW.md"
#: Must equal ``collections.scheduled.PREPAID_COVERAGE_QUARANTINE_FINDING_PREFIX``
#: (asserted by tests); duplicated so this owner does not import the runner.
QUARANTINE_FINDING_PREFIX = "prepaid-coverage:quarantine:"

_REQUEST_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="request_paid_invoice_period_repair",
)
_APPROVE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="approve_paid_invoice_period_repair",
)
_SCHEMA_VERSION = 1
#: Stable namespace for deterministic request/approval event identities.
_NAMESPACE = UUID("6c1f0b8e-2d4a-4e7b-9f3c-5a8d7e6b4c21")
_BASE_LINE_KIND = "base_subscription"
_MAX_REASON_LENGTH = 500
_MIN_REASON_LENGTH = 16
_MAX_EVIDENCE_REFERENCE_LENGTH = 200
_HEX = frozenset("0123456789abcdef")
_ZERO = Decimal("0.00")


class PaidInvoicePeriodRepairError(DomainError):
    """Stable fail-closed error raised by this owner."""


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise PaidInvoicePeriodRepairError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
        retryable=False,
    )


class EntitlementDisposition(StrEnum):
    """How the repaired paid line maps onto prepaid coverage evidence."""

    #: The paid-line entitlement writer creates one entitlement for the line.
    create_from_paid_line = "create_from_paid_line"
    #: One existing entitlement already funds this exact payment (it is linked
    #: to the line, or carries the invoice as its structured paid-invoice
    #: source). No second entitlement is created, so funding is not counted
    #: twice.
    existing_entitlement_funds_line = "existing_entitlement_funds_line"


class PaidInvoicePeriodRepairBlocker(StrEnum):
    invoice_not_paid = "invoice_not_paid"
    invoice_period_not_malformed = "invoice_period_not_malformed"
    line_not_positive = "line_not_positive"
    line_linked_to_other_subscription = "line_linked_to_other_subscription"
    invoice_has_other_subscription_lines = "invoice_has_other_subscription_lines"
    subscription_account_mismatch = "subscription_account_mismatch"
    subscription_not_prepaid = "subscription_not_prepaid"
    currency_mismatch = "currency_mismatch"
    settlement_does_not_match_total = "settlement_does_not_match_total"
    line_already_has_entitlement = "line_already_has_entitlement"
    overlapping_entitlement_unresolved = "overlapping_entitlement_unresolved"
    acknowledged_overlap_not_found = "acknowledged_overlap_not_found"
    adopted_entitlement_required = "adopted_entitlement_required"
    adopted_entitlement_invalid = "adopted_entitlement_invalid"
    adopted_entitlement_not_linked_to_invoice = (
        "adopted_entitlement_not_linked_to_invoice"
    )
    adopted_entitlement_period_mismatch = "adopted_entitlement_period_mismatch"
    adopted_entitlement_currency_mismatch = "adopted_entitlement_currency_mismatch"
    unacknowledged_warning = "unacknowledged_warning"
    acknowledged_warning_not_present = "acknowledged_warning_not_present"


class PaidInvoicePeriodRepairWarning(StrEnum):
    """Facts that may be legitimate but need an explicit Finance acknowledgement."""

    line_not_base_subscription = "line_not_base_subscription"
    subscription_terms_unpriced = "subscription_terms_unpriced"
    amount_differs_from_subscription_terms = "amount_differs_from_subscription_terms"
    period_not_one_billing_cycle = "period_not_one_billing_cycle"
    adopted_entitlement_amount_differs = "adopted_entitlement_amount_differs"


class PaidInvoicePeriodRepairStatus(StrEnum):
    requested = "requested"
    applied = "applied"


@dataclass(frozen=True, slots=True)
class PaidInvoicePeriodRepairQuery:
    """The operator's complete proposal; every field is part of the fingerprint."""

    invoice_id: UUID
    line_id: UUID
    subscription_id: UUID
    period_start: datetime
    period_end: datetime
    disposition: EntitlementDisposition = EntitlementDisposition.create_from_paid_line
    adopted_entitlement_id: UUID | None = None
    acknowledged_overlapping_entitlement_ids: tuple[UUID, ...] = ()
    acknowledged_warnings: tuple[PaidInvoicePeriodRepairWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class InvoicePeriodState:
    """Documentary period identity of the invoice and its reviewed line."""

    billing_period_start: datetime | None
    billing_period_end: datetime | None
    line_subscription_id: UUID | None
    line_period_start: datetime | None
    line_period_end: datetime | None


@dataclass(frozen=True, slots=True)
class OverlappingEntitlement:
    entitlement_id: UUID
    starts_at: datetime
    ends_at: datetime
    amount_funded: Decimal
    currency: str
    source_invoice_id: UUID | None
    source_invoice_line_id: UUID | None
    source_ledger_entry_id: UUID | None
    acknowledged: bool
    adopted: bool


@dataclass(frozen=True, slots=True)
class PlannedEntitlement:
    """The entitlement evidence the repair leaves behind for this payment."""

    disposition: EntitlementDisposition
    existing_entitlement_id: UUID | None
    subscription_id: UUID
    account_id: UUID
    starts_at: datetime
    ends_at: datetime
    amount_funded: Decimal
    currency: str
    source_invoice_id: UUID | None
    source_invoice_line_id: UUID | None


@dataclass(frozen=True, slots=True)
class SubscriptionTermsCheck:
    line_amount: Decimal
    line_kind: str | None
    subscription_unit_price: Decimal | None
    billing_cycle: str
    one_cycle_end: datetime


@dataclass(frozen=True, slots=True)
class SettlementCheck:
    invoice_total: Decimal
    balance_due: Decimal
    allocated_payments: Decimal
    applied_credit_notes: Decimal


@dataclass(frozen=True, slots=True)
class QuarantineEffect:
    """Projected effect on the account's prepaid coverage quarantine.

    Informational and time-dependent, so it is not part of the fingerprint.
    ``target_reason_after`` is ``None`` when the subscription is outside the
    collectible prepaid cohort that the sweep evaluates.
    """

    account_id: UUID
    as_of: datetime
    work_item_open: bool
    current_blocking_reasons: tuple[CoverageReconciliationReason, ...]
    target_reason_before: CoverageReconciliationReason | None
    target_reason_after: CoverageReconciliationReason | None
    other_malformed_invoice_ids: tuple[UUID, ...]
    projected_blocking_reasons: tuple[CoverageReconciliationReason, ...]

    @property
    def work_item_resolves_on_next_sweep(self) -> bool:
        return not self.projected_blocking_reasons


@dataclass(frozen=True, slots=True)
class PaidInvoicePeriodRepairPreview:
    query: PaidInvoicePeriodRepairQuery
    invoice_number: str | None
    account_id: UUID
    currency: str
    before: InvoicePeriodState
    after: InvoicePeriodState
    planned_entitlement: PlannedEntitlement
    overlapping_entitlements: tuple[OverlappingEntitlement, ...]
    terms: SubscriptionTermsCheck
    settlement: SettlementCheck
    warnings: tuple[PaidInvoicePeriodRepairWarning, ...]
    blockers: tuple[PaidInvoicePeriodRepairBlocker, ...]
    quarantine_effect: QuarantineEffect
    fingerprint: str

    @property
    def actionable(self) -> bool:
        return not self.blockers


@dataclass(frozen=True, slots=True)
class RequestPaidInvoicePeriodRepairCommand:
    """Step 1 of 2: one staff member proposes the reviewed repair.

    ``requested_by`` is the real staff principal (``SystemUser.id``) whose
    granted role authorized ``permission_granted``; ``context.actor`` remains
    the audit label.
    """

    query: PaidInvoicePeriodRepairQuery
    preview_fingerprint: str
    reason: str
    evidence_reference: str
    evidence_sha256: str
    requested_by: UUID
    permission_granted: bool


@dataclass(frozen=True, slots=True)
class ApprovePaidInvoicePeriodRepairCommand:
    """Step 2 of 2: a different staff member approves and applies it.

    The approver restates the fingerprint, so an approval cannot be given
    without seeing exactly what it applies.
    """

    request_id: UUID
    preview_fingerprint: str
    approved_by: UUID
    permission_granted: bool
    #: Only for ``approved_by == requested_by`` under the governed
    #: sole-approver exception (``governance.sole_approver_exception``).
    sole_approver_justification: str | None = None


@dataclass(frozen=True, slots=True)
class PaidInvoicePeriodRepairRequest:
    """Durable read model of one proposal and its approval, if any."""

    request_id: UUID
    query: PaidInvoicePeriodRepairQuery
    account_id: UUID
    preview_fingerprint: str
    reason: str
    evidence_reference: str
    evidence_sha256: str
    requested_by: UUID
    requested_at: datetime
    status: PaidInvoicePeriodRepairStatus
    approved_by: UUID | None
    entitlement_id: UUID | None


@dataclass(frozen=True, slots=True)
class PaidInvoicePeriodRepairResult:
    request_id: UUID
    invoice_id: UUID
    line_id: UUID
    subscription_id: UUID
    status: PaidInvoicePeriodRepairStatus
    preview_fingerprint: str
    billing_period_start: datetime
    billing_period_end: datetime
    disposition: EntitlementDisposition
    entitlement_id: UUID | None
    projected_blocking_reasons: tuple[CoverageReconciliationReason, ...]
    replayed: bool


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------


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


def _money(value: Decimal | int | float | str | None) -> Decimal:
    return round_money(to_decimal(value))


def _json_default(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, StrEnum):
        return value.value
    raise TypeError(f"unhashable fingerprint value: {type(value).__name__}")


def _hash(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=_json_default
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_query(query: PaidInvoicePeriodRepairQuery) -> None:
    start, end = query.period_start, query.period_end
    if (
        start.tzinfo is None
        or start.utcoffset() is None
        or end.tzinfo is None
        or end.utcoffset() is None
    ):
        _error("invalid_period", "Period boundaries must include a timezone offset.")
    if _utc(end) <= _utc(start):
        _error("invalid_period", "The period end must be after its start.")
    if (
        query.disposition is EntitlementDisposition.create_from_paid_line
        and query.adopted_entitlement_id is not None
    ):
        _error(
            "invalid_disposition",
            "An adopted entitlement is only valid with "
            "existing_entitlement_funds_line.",
        )
    if len(set(query.acknowledged_overlapping_entitlement_ids)) != len(
        query.acknowledged_overlapping_entitlement_ids
    ) or len(set(query.acknowledged_warnings)) != len(query.acknowledged_warnings):
        _error(
            "invalid_acknowledgement", "Acknowledgements must not contain duplicates."
        )


def _query_payload(query: PaidInvoicePeriodRepairQuery) -> dict[str, object]:
    return {
        "invoice_id": str(query.invoice_id),
        "line_id": str(query.line_id),
        "subscription_id": str(query.subscription_id),
        "period_start": _utc(query.period_start).isoformat(),
        "period_end": _utc(query.period_end).isoformat(),
        "disposition": query.disposition.value,
        "adopted_entitlement_id": (
            str(query.adopted_entitlement_id)
            if query.adopted_entitlement_id is not None
            else None
        ),
        "acknowledged_overlapping_entitlement_ids": sorted(
            str(value) for value in query.acknowledged_overlapping_entitlement_ids
        ),
        "acknowledged_warnings": sorted(
            value.value for value in query.acknowledged_warnings
        ),
    }


def _query_from_payload(payload: Mapping[str, object]) -> PaidInvoicePeriodRepairQuery:
    adopted = payload.get("adopted_entitlement_id")
    overlaps = payload.get("acknowledged_overlapping_entitlement_ids") or []
    warnings = payload.get("acknowledged_warnings") or []
    if not isinstance(overlaps, list) or not isinstance(warnings, list):
        _error("corrupt_request", "Stored repair request evidence is malformed.")
    return PaidInvoicePeriodRepairQuery(
        invoice_id=UUID(str(payload["invoice_id"])),
        line_id=UUID(str(payload["line_id"])),
        subscription_id=UUID(str(payload["subscription_id"])),
        period_start=datetime.fromisoformat(str(payload["period_start"])),
        period_end=datetime.fromisoformat(str(payload["period_end"])),
        disposition=EntitlementDisposition(str(payload["disposition"])),
        adopted_entitlement_id=UUID(str(adopted)) if adopted else None,
        acknowledged_overlapping_entitlement_ids=tuple(
            UUID(str(value)) for value in overlaps
        ),
        acknowledged_warnings=tuple(
            PaidInvoicePeriodRepairWarning(str(value)) for value in warnings
        ),
    )


# ---------------------------------------------------------------------------
# Preview (read-only query)
# ---------------------------------------------------------------------------


def _line_state(
    invoice: Invoice, line: InvoiceLine
) -> tuple[InvoicePeriodState, str | None]:
    metadata = line.metadata_ if isinstance(line.metadata_, dict) else {}
    kind = metadata.get("kind")
    return (
        InvoicePeriodState(
            billing_period_start=_optional_utc(invoice.billing_period_start),
            billing_period_end=_optional_utc(invoice.billing_period_end),
            line_subscription_id=line.subscription_id,
            line_period_start=_metadata_datetime(metadata.get("billing_period_start")),
            line_period_end=_metadata_datetime(metadata.get("billing_period_end")),
        ),
        str(kind) if kind else None,
    )


def _settlement(db: Session, invoice: Invoice) -> SettlementCheck:
    allocated = db.scalar(
        select(func.coalesce(func.sum(PaymentAllocation.amount), 0)).where(
            PaymentAllocation.invoice_id == invoice.id,
            PaymentAllocation.is_active.is_(True),
            PaymentAllocation.reversed_at.is_(None),
        )
    )
    credited = db.scalar(
        select(func.coalesce(func.sum(CreditNoteApplication.amount), 0)).where(
            CreditNoteApplication.invoice_id == invoice.id
        )
    )
    return SettlementCheck(
        invoice_total=_money(invoice.total),
        balance_due=_money(invoice.balance_due),
        allocated_payments=_money(allocated or 0),
        applied_credit_notes=_money(credited or 0),
    )


def _terms(
    db: Session,
    *,
    subscription: Subscription,
    line_amount: Decimal,
    line_kind: str | None,
    period_start: datetime,
) -> SubscriptionTermsCheck:
    from app.services.catalog.subscriptions import (
        _resolve_billing_cycle,
        billing_cycle_end,
    )

    cycle = _resolve_billing_cycle(
        db,
        str(subscription.offer_id),
        str(subscription.offer_version_id) if subscription.offer_version_id else None,
        override=subscription.billing_cycle,
    )
    unit_price = (
        _money(subscription.unit_price) if subscription.unit_price is not None else None
    )
    return SubscriptionTermsCheck(
        line_amount=line_amount,
        line_kind=line_kind,
        subscription_unit_price=unit_price,
        billing_cycle=cycle.value,
        one_cycle_end=_utc(billing_cycle_end(_utc(period_start), cycle)),
    )


def _overlapping_entitlements(
    db: Session,
    *,
    subscription_id: UUID,
    starts_at: datetime,
    ends_at: datetime,
) -> list[ServiceEntitlement]:
    return list(
        db.scalars(
            select(ServiceEntitlement)
            .where(
                ServiceEntitlement.subscription_id == subscription_id,
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
                ServiceEntitlement.starts_at < ends_at,
                ServiceEntitlement.ends_at > starts_at,
            )
            .order_by(ServiceEntitlement.starts_at, ServiceEntitlement.id)
        ).all()
    )


def _adopted_entitlement_linked(
    entitlement: ServiceEntitlement, *, invoice: Invoice, line: InvoiceLine
) -> bool:
    """Structured (never memo) proof that this entitlement funds this payment."""
    if entitlement.source_invoice_line_id is not None:
        return entitlement.source_invoice_line_id == line.id
    other_sources = (
        entitlement.source_ledger_entry_id,
        entitlement.source_billing_grant_id,
        entitlement.source_pause_episode_id,
        entitlement.source_outage_compensation_id,
    )
    if any(value is not None for value in other_sources):
        return False
    metadata = entitlement.metadata_ if isinstance(entitlement.metadata_, dict) else {}
    return entitlement.source_invoice_id == invoice.id or str(
        metadata.get("paid_invoice_id") or ""
    ) == str(invoice.id)


def _work_item_open(db: Session, account_id: UUID) -> bool:
    return (
        db.scalar(
            select(AdminAlert.id).where(
                AdminAlert.fingerprint == f"{QUARANTINE_FINDING_PREFIX}{account_id}",
                AdminAlert.status != AlertStatus.resolved,
            )
        )
        is not None
    )


def _quarantine_effect(
    db: Session,
    *,
    invoice: Invoice,
    subscription: Subscription,
    period_start: datetime,
    period_end: datetime,
    as_of: datetime,
) -> QuarantineEffect:
    cohort = list(
        db.scalars(
            select(Subscription)
            .where(
                Subscription.subscriber_id == invoice.account_id,
                Subscription.billing_mode == BillingMode.prepaid,
                Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES),
            )
            .order_by(Subscription.id)
        ).all()
    )
    preview = preview_prepaid_coverage_reconciliation(
        db,
        as_of=as_of,
        subscription_ids=tuple(row.id for row in cohort),
    )
    blocking = ENFORCEMENT_BLOCKING_QUARANTINE_REASONS

    def _blocking(
        decision: CoverageReconciliationDecision,
        reason: CoverageReconciliationReason,
    ) -> bool:
        return (
            decision is CoverageReconciliationDecision.quarantined
            and reason in blocking
        )

    current = {
        item.reason for item in preview.items if _blocking(item.decision, item.reason)
    }
    other_reasons = {
        item.reason
        for item in preview.items
        if item.subscription_id != subscription.id
        and _blocking(item.decision, item.reason)
    }
    other_malformed = tuple(
        value
        for value in malformed_paid_invoice_ids_by_subscription(
            db, (subscription.id,)
        ).get(subscription.id, ())
        if value != invoice.id
    )
    target = next(
        (item for item in preview.items if item.subscription_id == subscription.id),
        None,
    )
    reason_after: CoverageReconciliationReason | None = None
    after_blocking = False
    if target is not None:
        reason_after = target.reason
        after_blocking = _blocking(target.decision, target.reason)
        if target.reason is CoverageReconciliationReason.malformed_paid_invoice_period:
            # Mirror the owner's precedence once this invoice stops being
            # malformed. Exact current evidence is resolved before the
            # malformed classes, so a period spanning now is covered by the
            # entitlement this repair leaves behind.
            after_blocking = True
            if other_malformed:
                reason_after = (
                    CoverageReconciliationReason.malformed_paid_invoice_period
                )
            elif _utc(period_start) <= as_of < _utc(period_end):
                reason_after = CoverageReconciliationReason.funded_entitlement
                after_blocking = False
            elif subscription.subscriber_id in (
                malformed_prepaid_renewal_origin_account_ids(db, cohort, as_of=as_of)
            ):
                reason_after = CoverageReconciliationReason.malformed_renewal_origin
            elif (
                subscription.next_billing_at is not None
                and _utc(subscription.next_billing_at) > as_of
            ):
                reason_after = (
                    CoverageReconciliationReason.future_anchor_without_exact_evidence
                )
                after_blocking = False
            else:
                reason_after = CoverageReconciliationReason.due_without_coverage
                after_blocking = False
    projected = set(other_reasons)
    if after_blocking and reason_after is not None:
        projected.add(reason_after)
    return QuarantineEffect(
        account_id=invoice.account_id,
        as_of=as_of,
        work_item_open=_work_item_open(db, invoice.account_id),
        current_blocking_reasons=tuple(sorted(current, key=lambda r: r.value)),
        target_reason_before=target.reason if target is not None else None,
        target_reason_after=reason_after,
        other_malformed_invoice_ids=other_malformed,
        projected_blocking_reasons=tuple(sorted(projected, key=lambda r: r.value)),
    )


def preview_paid_invoice_period_repair(
    db: Session,
    query: PaidInvoicePeriodRepairQuery,
    *,
    as_of: datetime | None = None,
) -> PaidInvoicePeriodRepairPreview:
    """Validate one proposed period repair without changing state."""
    from app.services.prepaid_currency import resolve_prepaid_enforcement_currency

    _validate_query(query)
    observed_at = _utc(as_of or datetime.now(UTC))
    start = _utc(query.period_start)
    end = _utc(query.period_end)

    invoice = db.get(Invoice, query.invoice_id)
    if invoice is None:
        _error("invoice_not_found", "The invoice was not found.")
    line = db.get(InvoiceLine, query.line_id)
    if line is None or line.invoice_id != invoice.id or not line.is_active:
        _error(
            "line_not_found",
            "The line is not an active line of this invoice.",
            line_id=str(query.line_id),
        )
    subscription = db.get(Subscription, query.subscription_id)
    if subscription is None:
        _error("subscription_not_found", "The subscription was not found.")

    blockers: set[PaidInvoicePeriodRepairBlocker] = set()
    warnings: set[PaidInvoicePeriodRepairWarning] = set()
    currency = (invoice.currency or "").upper()

    if (
        not invoice.is_active
        or invoice.is_proforma
        or invoice.status is not InvoiceStatus.paid
        or _money(invoice.balance_due) > _ZERO
    ):
        blockers.add(PaidInvoicePeriodRepairBlocker.invoice_not_paid)
    if not is_malformed_paid_invoice_period(
        invoice.billing_period_start, invoice.billing_period_end
    ):
        blockers.add(PaidInvoicePeriodRepairBlocker.invoice_period_not_malformed)
    line_amount = _money(line.amount)
    if line_amount <= _ZERO:
        blockers.add(PaidInvoicePeriodRepairBlocker.line_not_positive)
    if line.subscription_id not in {None, subscription.id}:
        blockers.add(PaidInvoicePeriodRepairBlocker.line_linked_to_other_subscription)
    other_linked = db.scalar(
        select(InvoiceLine.id).where(
            InvoiceLine.invoice_id == invoice.id,
            InvoiceLine.id != line.id,
            InvoiceLine.is_active.is_(True),
            InvoiceLine.amount > _ZERO,
            InvoiceLine.subscription_id.is_not(None),
        )
    )
    if other_linked is not None:
        # The period is invoice-level; restoring it would silently assert the
        # same period for a second subscription-linked charge.
        blockers.add(
            PaidInvoicePeriodRepairBlocker.invoice_has_other_subscription_lines
        )
    if subscription.subscriber_id != invoice.account_id:
        blockers.add(PaidInvoicePeriodRepairBlocker.subscription_account_mismatch)
    if subscription.billing_mode != BillingMode.prepaid:
        blockers.add(PaidInvoicePeriodRepairBlocker.subscription_not_prepaid)
    if currency != resolve_prepaid_enforcement_currency(db):
        blockers.add(PaidInvoicePeriodRepairBlocker.currency_mismatch)
    settlement = _settlement(db, invoice)
    if (
        settlement.allocated_payments + settlement.applied_credit_notes
        != settlement.invoice_total
    ):
        blockers.add(PaidInvoicePeriodRepairBlocker.settlement_does_not_match_total)

    before, line_kind = _line_state(invoice, line)
    terms = _terms(
        db,
        subscription=subscription,
        line_amount=line_amount,
        line_kind=line_kind,
        period_start=start,
    )
    if line_kind != _BASE_LINE_KIND:
        warnings.add(PaidInvoicePeriodRepairWarning.line_not_base_subscription)
    if terms.subscription_unit_price is None or terms.subscription_unit_price <= _ZERO:
        warnings.add(PaidInvoicePeriodRepairWarning.subscription_terms_unpriced)
    elif terms.subscription_unit_price != line_amount:
        warnings.add(
            PaidInvoicePeriodRepairWarning.amount_differs_from_subscription_terms
        )
    if terms.one_cycle_end != end:
        warnings.add(PaidInvoicePeriodRepairWarning.period_not_one_billing_cycle)

    overlaps = _overlapping_entitlements(
        db, subscription_id=subscription.id, starts_at=start, ends_at=end
    )
    overlap_ids = {row.id for row in overlaps}
    acknowledged = set(query.acknowledged_overlapping_entitlement_ids)
    if acknowledged - overlap_ids:
        blockers.add(PaidInvoicePeriodRepairBlocker.acknowledged_overlap_not_found)

    adopted: ServiceEntitlement | None = None
    if query.disposition is EntitlementDisposition.create_from_paid_line:
        line_sourced = db.scalar(
            select(ServiceEntitlement.id).where(
                ServiceEntitlement.source_invoice_line_id == line.id,
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
            )
        )
        if line_sourced is not None:
            blockers.add(PaidInvoicePeriodRepairBlocker.line_already_has_entitlement)
        if overlap_ids - acknowledged:
            blockers.add(
                PaidInvoicePeriodRepairBlocker.overlapping_entitlement_unresolved
            )
        planned = PlannedEntitlement(
            disposition=query.disposition,
            existing_entitlement_id=None,
            subscription_id=subscription.id,
            account_id=invoice.account_id,
            starts_at=start,
            ends_at=end,
            amount_funded=line_amount,
            currency=currency,
            source_invoice_id=invoice.id,
            source_invoice_line_id=line.id,
        )
    else:
        if query.adopted_entitlement_id is None:
            blockers.add(PaidInvoicePeriodRepairBlocker.adopted_entitlement_required)
        else:
            adopted = db.get(ServiceEntitlement, query.adopted_entitlement_id)
        if adopted is not None and (
            adopted.status != ServiceEntitlementStatus.active
            or adopted.subscription_id != subscription.id
            or adopted.account_id != invoice.account_id
        ):
            blockers.add(PaidInvoicePeriodRepairBlocker.adopted_entitlement_invalid)
        elif adopted is None and query.adopted_entitlement_id is not None:
            blockers.add(PaidInvoicePeriodRepairBlocker.adopted_entitlement_invalid)
        if adopted is not None:
            if not _adopted_entitlement_linked(adopted, invoice=invoice, line=line):
                blockers.add(
                    PaidInvoicePeriodRepairBlocker.adopted_entitlement_not_linked_to_invoice
                )
            if _utc(adopted.starts_at) != start or _utc(adopted.ends_at) != end:
                blockers.add(
                    PaidInvoicePeriodRepairBlocker.adopted_entitlement_period_mismatch
                )
            if (adopted.currency or "").upper() != currency:
                blockers.add(
                    PaidInvoicePeriodRepairBlocker.adopted_entitlement_currency_mismatch
                )
            if _money(adopted.amount_funded) != line_amount:
                warnings.add(
                    PaidInvoicePeriodRepairWarning.adopted_entitlement_amount_differs
                )
        adopted_id = adopted.id if adopted is not None else None
        if overlap_ids - acknowledged - {adopted_id}:
            blockers.add(
                PaidInvoicePeriodRepairBlocker.overlapping_entitlement_unresolved
            )
        planned = PlannedEntitlement(
            disposition=query.disposition,
            existing_entitlement_id=adopted_id,
            subscription_id=subscription.id,
            account_id=invoice.account_id,
            starts_at=_utc(adopted.starts_at) if adopted is not None else start,
            ends_at=_utc(adopted.ends_at) if adopted is not None else end,
            amount_funded=(
                _money(adopted.amount_funded) if adopted is not None else line_amount
            ),
            currency=(adopted.currency or "").upper() if adopted else currency,
            source_invoice_id=adopted.source_invoice_id if adopted else None,
            source_invoice_line_id=(
                adopted.source_invoice_line_id if adopted else None
            ),
        )

    acknowledged_warnings = set(query.acknowledged_warnings)
    if warnings - acknowledged_warnings:
        blockers.add(PaidInvoicePeriodRepairBlocker.unacknowledged_warning)
    if acknowledged_warnings - warnings:
        blockers.add(PaidInvoicePeriodRepairBlocker.acknowledged_warning_not_present)

    after = InvoicePeriodState(
        billing_period_start=start,
        billing_period_end=end,
        line_subscription_id=subscription.id,
        line_period_start=start,
        line_period_end=end,
    )
    overlap_rows = tuple(
        OverlappingEntitlement(
            entitlement_id=row.id,
            starts_at=_utc(row.starts_at),
            ends_at=_utc(row.ends_at),
            amount_funded=_money(row.amount_funded),
            currency=(row.currency or "").upper(),
            source_invoice_id=row.source_invoice_id,
            source_invoice_line_id=row.source_invoice_line_id,
            source_ledger_entry_id=row.source_ledger_entry_id,
            acknowledged=row.id in acknowledged,
            adopted=adopted is not None and row.id == adopted.id,
        )
        for row in overlaps
    )
    ordered_warnings = tuple(sorted(warnings, key=lambda value: value.value))
    ordered_blockers = tuple(sorted(blockers, key=lambda value: value.value))
    fingerprint = _hash(
        {
            "owner": OWNER,
            "schema_version": _SCHEMA_VERSION,
            "query": _query_payload(query),
            "invoice": {
                "id": invoice.id,
                "account_id": invoice.account_id,
                "status": invoice.status.value,
                "is_active": bool(invoice.is_active),
                "is_proforma": bool(invoice.is_proforma),
                "currency": currency,
            },
            "line": {
                "amount": line_amount,
                "kind": line_kind,
            },
            "before": _state_payload(before),
            "after": _state_payload(after),
            "planned_entitlement": {
                "disposition": planned.disposition,
                "existing_entitlement_id": planned.existing_entitlement_id,
                "subscription_id": planned.subscription_id,
                "account_id": planned.account_id,
                "starts_at": planned.starts_at,
                "ends_at": planned.ends_at,
                "amount_funded": planned.amount_funded,
                "currency": planned.currency,
            },
            "overlaps": [
                {
                    "id": row.entitlement_id,
                    "starts_at": row.starts_at,
                    "ends_at": row.ends_at,
                    "amount_funded": row.amount_funded,
                    "currency": row.currency,
                    "source_invoice_line_id": row.source_invoice_line_id,
                    "source_ledger_entry_id": row.source_ledger_entry_id,
                }
                for row in overlap_rows
            ],
            "terms": {
                "unit_price": terms.subscription_unit_price,
                "billing_cycle": terms.billing_cycle,
                "one_cycle_end": terms.one_cycle_end,
            },
            "settlement": {
                "total": settlement.invoice_total,
                "balance_due": settlement.balance_due,
                "allocated": settlement.allocated_payments,
                "credited": settlement.applied_credit_notes,
            },
            "warnings": list(ordered_warnings),
            "blockers": list(ordered_blockers),
        }
    )
    return PaidInvoicePeriodRepairPreview(
        query=query,
        invoice_number=invoice.invoice_number,
        account_id=invoice.account_id,
        currency=currency,
        before=before,
        after=after,
        planned_entitlement=planned,
        overlapping_entitlements=overlap_rows,
        terms=terms,
        settlement=settlement,
        warnings=ordered_warnings,
        blockers=ordered_blockers,
        quarantine_effect=_quarantine_effect(
            db,
            invoice=invoice,
            subscription=subscription,
            period_start=start,
            period_end=end,
            as_of=observed_at,
        ),
        fingerprint=fingerprint,
    )


def _state_json(state: InvoicePeriodState) -> dict[str, str | None]:
    """Explicit scalar serialization for audit and event payloads."""
    return {
        key: (
            None
            if value is None
            else value.isoformat()
            if isinstance(value, datetime)
            else str(value)
        )
        for key, value in _state_payload(state).items()
    }


def _state_payload(state: InvoicePeriodState) -> dict[str, object]:
    return {
        "billing_period_start": state.billing_period_start,
        "billing_period_end": state.billing_period_end,
        "line_subscription_id": state.line_subscription_id,
        "line_period_start": state.line_period_start,
        "line_period_end": state.line_period_end,
    }


# ---------------------------------------------------------------------------
# Durable request/approval evidence (event store, record-only request)
# ---------------------------------------------------------------------------


def _request_event_id(idempotency_key: str) -> UUID:
    return uuid5(_NAMESPACE, f"{OWNER}:request:{idempotency_key}")


def _approval_event_id(request_id: UUID) -> UUID:
    return uuid5(_NAMESPACE, f"{OWNER}:approval:{request_id}")


def _event_payload(
    db: Session, *, event_id: UUID, event_type: EventType
) -> dict[str, object] | None:
    event = db.execute(
        select(EventStore).where(
            EventStore.event_id == event_id,
            EventStore.event_type == event_type.value,
        )
    ).scalar_one_or_none()
    return dict(event.payload or {}) if event is not None else None


def _request_from_payload(
    payload: Mapping[str, object], *, approval: Mapping[str, object] | None
) -> PaidInvoicePeriodRepairRequest:
    query_payload = payload.get("query")
    if not isinstance(query_payload, dict):
        _error("corrupt_request", "Stored repair request evidence is malformed.")
    entitlement = approval.get("entitlement_id") if approval is not None else None
    return PaidInvoicePeriodRepairRequest(
        request_id=UUID(str(payload["request_id"])),
        query=_query_from_payload(query_payload),
        account_id=UUID(str(payload["account_id"])),
        preview_fingerprint=str(payload["preview_fingerprint"]),
        reason=str(payload["reason"]),
        evidence_reference=str(payload["evidence_reference"]),
        evidence_sha256=str(payload["evidence_sha256"]),
        requested_by=UUID(str(payload["requested_by_system_user_id"])),
        requested_at=datetime.fromisoformat(str(payload["requested_at"])),
        status=(
            PaidInvoicePeriodRepairStatus.applied
            if approval is not None
            else PaidInvoicePeriodRepairStatus.requested
        ),
        approved_by=(
            UUID(str(approval["approved_by_system_user_id"]))
            if approval is not None
            else None
        ),
        entitlement_id=UUID(str(entitlement)) if entitlement else None,
    )


def _load_approval(db: Session, request_id: UUID) -> dict[str, object] | None:
    return _event_payload(
        db,
        event_id=_approval_event_id(request_id),
        event_type=EventType.prepaid_paid_invoice_period_repaired,
    )


def _load_request(
    db: Session, request_id: UUID
) -> PaidInvoicePeriodRepairRequest | None:
    payload = _event_payload(
        db,
        event_id=request_id,
        event_type=EventType.prepaid_paid_invoice_period_repair_requested,
    )
    if payload is None:
        return None
    return _request_from_payload(payload, approval=_load_approval(db, request_id))


def list_paid_invoice_period_repair_requests(
    db: Session,
    *,
    invoice_id: UUID | None = None,
    include_applied: bool = False,
) -> tuple[PaidInvoicePeriodRepairRequest, ...]:
    """Read-only: proposals (pending only by default), oldest first."""
    statement = select(EventStore).where(
        EventStore.event_type
        == EventType.prepaid_paid_invoice_period_repair_requested.value
    )
    if invoice_id is not None:
        statement = statement.where(EventStore.invoice_id == invoice_id)
    rows: list[PaidInvoicePeriodRepairRequest] = []
    for event in db.execute(statement.order_by(EventStore.created_at)).scalars():
        payload = dict(event.payload or {})
        request = _request_from_payload(
            payload,
            approval=_load_approval(db, UUID(str(payload["request_id"]))),
        )
        if include_applied or request.status is PaidInvoicePeriodRepairStatus.requested:
            rows.append(request)
    return tuple(rows)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _require_staff(
    db: Session,
    *,
    context: CommandContext,
    system_user_id: UUID,
    permission_granted: bool,
) -> None:
    from app.models.system_user import SystemUser

    if context.scope != REPAIR_PERMISSION or not permission_granted:
        _error("permission_denied", f"The {REPAIR_PERMISSION} permission is required.")
    user = db.get(SystemUser, system_user_id)
    if user is None or not user.is_active:
        _error(
            "invalid_actor", "The staff member must be an existing, active system user."
        )


def _validated_evidence(command: RequestPaidInvoicePeriodRepairCommand) -> str:
    reason = command.reason.strip()
    if not _MIN_REASON_LENGTH <= len(reason) <= _MAX_REASON_LENGTH:
        _error(
            "invalid_reason",
            f"A reason of {_MIN_REASON_LENGTH}-{_MAX_REASON_LENGTH} characters "
            "explaining the Finance determination is required.",
        )
    reference = command.evidence_reference.strip()
    digest = command.evidence_sha256.strip().lower()
    if (
        not reference
        or len(reference) > _MAX_EVIDENCE_REFERENCE_LENGTH
        or len(digest) != 64
        or not set(digest) <= _HEX
    ):
        _error(
            "invalid_evidence",
            "An evidence reference and the evidence's 64-hex SHA-256 are required.",
        )
    return digest


def _lock_chain(db: Session, query: PaidInvoicePeriodRepairQuery) -> None:
    """Lock account, subscription, invoice, line, then entitlements, in order."""
    invoice = db.get(Invoice, query.invoice_id)
    if invoice is None:
        _error("invoice_not_found", "The invoice was not found.")
    db.execute(
        select(Subscriber.id)
        .where(Subscriber.id == invoice.account_id)
        .with_for_update()
    ).all()
    db.execute(
        select(Subscription.id)
        .where(Subscription.id == query.subscription_id)
        .with_for_update()
    ).all()
    db.execute(
        select(Invoice.id).where(Invoice.id == query.invoice_id).with_for_update()
    ).all()
    db.execute(
        select(InvoiceLine.id).where(InvoiceLine.id == query.line_id).with_for_update()
    ).all()
    db.execute(
        select(ServiceEntitlement.id)
        .where(ServiceEntitlement.subscription_id == query.subscription_id)
        .order_by(ServiceEntitlement.id)
        .with_for_update()
    ).all()
    # Re-read every locked row so the recomputed preview sees committed state.
    db.expire_all()


def request_paid_invoice_period_repair(
    db: Session,
    command: RequestPaidInvoicePeriodRepairCommand,
    *,
    context: CommandContext,
) -> PaidInvoicePeriodRepairResult:
    """Record one staff member's fingerprint-bound proposal (changes no invoice)."""
    return execute_owner_command(
        db,
        definition=_REQUEST_COMMAND,
        context=context,
        operation=lambda: _request(db, command=command, context=context),
    )


def _result_from_request(
    request: PaidInvoicePeriodRepairRequest,
    *,
    projected: tuple[CoverageReconciliationReason, ...],
    replayed: bool,
) -> PaidInvoicePeriodRepairResult:
    return PaidInvoicePeriodRepairResult(
        request_id=request.request_id,
        invoice_id=request.query.invoice_id,
        line_id=request.query.line_id,
        subscription_id=request.query.subscription_id,
        status=request.status,
        preview_fingerprint=request.preview_fingerprint,
        billing_period_start=_utc(request.query.period_start),
        billing_period_end=_utc(request.query.period_end),
        disposition=request.query.disposition,
        entitlement_id=request.entitlement_id,
        projected_blocking_reasons=projected,
        replayed=replayed,
    )


def _request(
    db: Session,
    *,
    command: RequestPaidInvoicePeriodRepairCommand,
    context: CommandContext,
) -> PaidInvoicePeriodRepairResult:
    key = (context.idempotency_key or "").strip()
    if not key:
        _error(
            "missing_idempotency_key",
            "A period repair request requires a business idempotency key.",
        )
    _require_staff(
        db,
        context=context,
        system_user_id=command.requested_by,
        permission_granted=command.permission_granted,
    )
    _validate_query(command.query)
    digest = _validated_evidence(command)
    request_id = _request_event_id(key)

    existing = _load_request(db, request_id)
    if existing is not None:
        if (
            _query_payload(existing.query) != _query_payload(command.query)
            or existing.preview_fingerprint != command.preview_fingerprint
            or existing.evidence_sha256 != digest
            or existing.requested_by != command.requested_by
        ):
            _error(
                "idempotency_conflict",
                "This idempotency key already recorded a different proposal.",
            )
        return _result_from_request(existing, projected=(), replayed=True)

    _lock_chain(db, command.query)
    preview = preview_paid_invoice_period_repair(db, command.query)
    if preview.fingerprint != command.preview_fingerprint:
        _error(
            "stale_preview",
            "The paid-invoice evidence changed after preview; preview again.",
            expected_fingerprint=command.preview_fingerprint,
            current_fingerprint=preview.fingerprint,
        )
    if not preview.actionable:
        _error(
            "not_actionable",
            "The previewed repair has blockers and cannot be requested.",
            blockers=[value.value for value in preview.blockers],
        )
    now = datetime.now(UTC)
    emit_event(
        db,
        EventType.prepaid_paid_invoice_period_repair_requested,
        {
            "schema_version": _SCHEMA_VERSION,
            "request_id": str(request_id),
            "query": _query_payload(command.query),
            "account_id": str(preview.account_id),
            "invoice_number": preview.invoice_number,
            "preview_fingerprint": preview.fingerprint,
            "warnings": [value.value for value in preview.warnings],
            "reason": command.reason.strip(),
            "evidence_reference": command.evidence_reference.strip(),
            "evidence_sha256": digest,
            "requested_by_system_user_id": str(command.requested_by),
            "requested_at": now.isoformat(),
            "actor": context.actor,
            "command_id": str(context.command_id),
            "idempotency_key": key,
        },
        event_id=request_id,
        actor=context.actor,
        subscriber_id=preview.account_id,
        account_id=preview.account_id,
        subscription_id=command.query.subscription_id,
        invoice_id=command.query.invoice_id,
        record_only=True,
    )
    logger.info(
        "prepaid_paid_invoice_period_repair_requested: request=%s invoice=%s",
        request_id,
        command.query.invoice_id,
    )
    return PaidInvoicePeriodRepairResult(
        request_id=request_id,
        invoice_id=command.query.invoice_id,
        line_id=command.query.line_id,
        subscription_id=command.query.subscription_id,
        status=PaidInvoicePeriodRepairStatus.requested,
        preview_fingerprint=preview.fingerprint,
        billing_period_start=_utc(command.query.period_start),
        billing_period_end=_utc(command.query.period_end),
        disposition=command.query.disposition,
        entitlement_id=None,
        projected_blocking_reasons=preview.quarantine_effect.projected_blocking_reasons,
        replayed=False,
    )


def approve_paid_invoice_period_repair(
    db: Session,
    command: ApprovePaidInvoicePeriodRepairCommand,
    *,
    context: CommandContext,
) -> PaidInvoicePeriodRepairResult:
    """A different staff member approves and atomically applies a proposal."""
    return execute_owner_command(
        db,
        definition=_APPROVE_COMMAND,
        context=context,
        operation=lambda: _approve(db, command=command, context=context),
    )


def _approved_replay(
    request: PaidInvoicePeriodRepairRequest, approver: UUID
) -> PaidInvoicePeriodRepairResult:
    if request.approved_by != approver:
        _error(
            "request_already_decided",
            "This period repair request was already approved.",
        )
    return _result_from_request(request, projected=(), replayed=True)


def _approve(
    db: Session,
    *,
    command: ApprovePaidInvoicePeriodRepairCommand,
    context: CommandContext,
) -> PaidInvoicePeriodRepairResult:
    key = (context.idempotency_key or "").strip()
    if not key:
        _error(
            "missing_idempotency_key",
            "A period repair approval requires a business idempotency key.",
        )
    _require_staff(
        db,
        context=context,
        system_user_id=command.approved_by,
        permission_granted=command.permission_granted,
    )
    request = _load_request(db, command.request_id)
    if request is None:
        _error("request_not_found", "The period repair request was not found.")
    exception_grant: SoleApproverExceptionGrant | None = None
    if command.approved_by == request.requested_by:
        exception_grant = authorize_sole_approver(
            db,
            flow=OWNER,
            approver_id=command.approved_by,
            actor=context.actor,
            justification=command.sole_approver_justification,
        ).grant
        if exception_grant is None:
            _error(
                "self_approval_forbidden",
                "The approver must be a different staff member from the requester.",
            )
    if command.preview_fingerprint != request.preview_fingerprint:
        _error(
            "approval_fingerprint_mismatch",
            "The approved fingerprint does not match the requested repair.",
        )
    if request.status is PaidInvoicePeriodRepairStatus.applied:
        return _approved_replay(request, command.approved_by)

    query = request.query
    _lock_chain(db, query)
    # A concurrent approval may have committed while this one waited.
    locked_request = _load_request(db, command.request_id)
    if (
        locked_request is not None
        and locked_request.status is PaidInvoicePeriodRepairStatus.applied
    ):
        return _approved_replay(locked_request, command.approved_by)

    preview = preview_paid_invoice_period_repair(db, query)
    if preview.fingerprint != request.preview_fingerprint:
        _error(
            "stale_preview",
            "The paid-invoice evidence changed since the request; submit a new "
            "request against the current evidence.",
            expected_fingerprint=request.preview_fingerprint,
            current_fingerprint=preview.fingerprint,
        )
    if not preview.actionable:
        _error(
            "not_actionable",
            "The repair has blockers and cannot be applied.",
            blockers=[value.value for value in preview.blockers],
        )

    evidence_ref = f"{OWNER}:{request.request_id}"
    try:
        invoice = Invoices.restore_reviewed_paid_prepaid_period_for_owner(
            db,
            ReviewedPaidPrepaidInvoicePeriodRestoration(
                invoice_id=query.invoice_id,
                line_id=query.line_id,
                subscription_id=query.subscription_id,
                expected_billing_period_start=preview.before.billing_period_start,
                expected_billing_period_end=preview.before.billing_period_end,
                expected_line_subscription_id=preview.before.line_subscription_id,
                expected_line_amount=preview.terms.line_amount,
                billing_period_start=_utc(query.period_start),
                billing_period_end=_utc(query.period_end),
                evidence_ref=evidence_ref,
            ),
        )
    except InvoiceOwnerError as exc:
        _error(
            "participant_rejected",
            "The invoice owner rejected the reviewed period restoration.",
            participant_error=exc.code,
        )
    line = db.get(InvoiceLine, query.line_id)
    if line is None:
        _error("incomplete_repair", "The repaired invoice line disappeared.")

    planned = preview.planned_entitlement
    entitlement_id: UUID | None
    if planned.disposition is EntitlementDisposition.create_from_paid_line:
        from app.services.prepaid_service_renewals import (
            BillingAnchorAuthority,
            project_prepaid_billing_anchor_for_invoice,
        )
        from app.services.service_entitlements import (
            ensure_prepaid_entitlement_for_paid_invoice_line,
        )

        entitlement = ensure_prepaid_entitlement_for_paid_invoice_line(
            db,
            invoice=invoice,
            line=line,
            reconciliation_fingerprint=preview.fingerprint,
            reconciled_by=OWNER,
        )
        if (
            entitlement is None
            or entitlement.source_invoice_line_id != line.id
            or entitlement.subscription_id != planned.subscription_id
            or entitlement.account_id != planned.account_id
            or _utc(entitlement.starts_at) != planned.starts_at
            or _utc(entitlement.ends_at) != planned.ends_at
            or _money(entitlement.amount_funded) != planned.amount_funded
            or (entitlement.currency or "").upper() != planned.currency
            or entitlement.id
            in {row.entitlement_id for row in preview.overlapping_entitlements}
        ):
            _error(
                "incomplete_repair",
                "The entitlement writer did not produce the reviewed entitlement.",
            )
        entitlement_id = entitlement.id
        # Advancement is monotonic: a historical period never moves the anchor.
        project_prepaid_billing_anchor_for_invoice(
            db,
            invoice,
            evidence_ref=evidence_ref,
            authority=BillingAnchorAuthority.funding_observation,
        )
    else:
        entitlement_id = planned.existing_entitlement_id

    effect = preview.quarantine_effect
    now = datetime.now(UTC)
    shared = {
        "request_id": str(request.request_id),
        "invoice_id": str(query.invoice_id),
        "invoice_number": preview.invoice_number,
        "line_id": str(query.line_id),
        "subscription_id": str(query.subscription_id),
        "before": _state_json(preview.before),
        "after": _state_json(preview.after),
        "billing_period_start": planned.starts_at.isoformat(),
        "billing_period_end": planned.ends_at.isoformat(),
        "disposition": planned.disposition.value,
        "entitlement_id": str(entitlement_id) if entitlement_id else None,
        "entitlement_created": (
            planned.disposition is EntitlementDisposition.create_from_paid_line
        ),
        "acknowledged_warnings": [value.value for value in preview.warnings],
        "acknowledged_overlapping_entitlement_ids": [
            str(value) for value in query.acknowledged_overlapping_entitlement_ids
        ],
        "currency": preview.currency,
        "line_amount": str(preview.terms.line_amount),
        "economic_delta": "0.00",
        "preview_fingerprint": preview.fingerprint,
        "reason": request.reason,
        "evidence_reference": request.evidence_reference,
        "evidence_sha256": request.evidence_sha256,
        "requested_by_system_user_id": str(request.requested_by),
        "approved_by_system_user_id": str(command.approved_by),
        **(
            exception_grant.evidence()
            if exception_grant is not None
            else {"sole_approver_exception": False}
        ),
        "projected_blocking_reasons": [
            value.value for value in effect.projected_blocking_reasons
        ],
    }
    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=str(command.approved_by),
            action="repair_paid_prepaid_invoice_period",
            entity_type="invoice",
            entity_id=str(query.invoice_id),
            metadata_=dict(shared),
        ),
    )
    if exception_grant is not None:
        stage_sole_approver_exception_audit(
            db,
            exception_grant,
            entity_type="invoice",
            entity_id=str(query.invoice_id),
            evidence_ref=evidence_ref,
        )
    emit_event(
        db,
        EventType.prepaid_paid_invoice_period_repaired,
        {
            "schema_version": _SCHEMA_VERSION,
            **shared,
            "approved_at": now.isoformat(),
            "actor": context.actor,
            "command_id": str(context.command_id),
            "idempotency_key": key,
        },
        event_id=_approval_event_id(request.request_id),
        actor=context.actor,
        subscriber_id=preview.account_id,
        account_id=preview.account_id,
        subscription_id=query.subscription_id,
        invoice_id=query.invoice_id,
    )
    db.flush()
    logger.info(
        "prepaid_paid_invoice_period_repaired: request=%s invoice=%s "
        "disposition=%s work_item_resolves_on_next_sweep=%s",
        request.request_id,
        query.invoice_id,
        planned.disposition.value,
        effect.work_item_resolves_on_next_sweep,
    )
    return PaidInvoicePeriodRepairResult(
        request_id=request.request_id,
        invoice_id=query.invoice_id,
        line_id=query.line_id,
        subscription_id=query.subscription_id,
        status=PaidInvoicePeriodRepairStatus.applied,
        preview_fingerprint=preview.fingerprint,
        billing_period_start=planned.starts_at,
        billing_period_end=planned.ends_at,
        disposition=planned.disposition,
        entitlement_id=entitlement_id,
        projected_blocking_reasons=effect.projected_blocking_reasons,
        replayed=False,
    )


__all__ = [
    "CONCERN",
    "OWNER",
    "QUARANTINE_FINDING_PREFIX",
    "REPAIR_PERMISSION",
    "RUNBOOK",
    "ApprovePaidInvoicePeriodRepairCommand",
    "EntitlementDisposition",
    "InvoicePeriodState",
    "OverlappingEntitlement",
    "PaidInvoicePeriodRepairBlocker",
    "PaidInvoicePeriodRepairError",
    "PaidInvoicePeriodRepairPreview",
    "PaidInvoicePeriodRepairQuery",
    "PaidInvoicePeriodRepairRequest",
    "PaidInvoicePeriodRepairResult",
    "PaidInvoicePeriodRepairStatus",
    "PaidInvoicePeriodRepairWarning",
    "PlannedEntitlement",
    "QuarantineEffect",
    "RequestPaidInvoicePeriodRepairCommand",
    "SettlementCheck",
    "SubscriptionTermsCheck",
    "approve_paid_invoice_period_repair",
    "list_paid_invoice_period_repair_requests",
    "preview_paid_invoice_period_repair",
    "request_paid_invoice_period_repair",
]
