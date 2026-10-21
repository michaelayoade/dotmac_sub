"""Finance-reviewed correction of a malformed prepaid-renewal adjustment reference.

``financial.prepaid_service_coverage_reconciliation`` quarantines an account
with ``malformed_renewal_origin`` when an unreversed ``prepaid_service_renewal``
debit carries an ``origin_ref`` that is not the canonical
``<subscription id>:<period start>:<period end>``. Nothing can tell which
service period that debit bought, so enforcement may not act on the account.

When Finance has decided the debit itself is legitimate (the customer received
the service), only its reference is wrong. This owner is the sanctioned way to
fix the reference. It never reverses, re-prices, or re-posts the debit: the
ledger debit, the adjustment's amount, and every balance stay exactly as they
are.

The canonical reference is never typed in by an operator. It is derived from
structured coverage evidence, one of three reviewed dispositions:

``entitlement_already_linked``
    Exactly one active entitlement is already linked to the debit's ledger
    entry. Its subscription and period are the proof.

``link_existing_entitlement``
    Finance names one existing active entitlement that the debit funded but
    that carries no ledger-debit link. The owner records the link through the
    entitlement writer's flush-only participant; no coverage is created.

``create_entitlement_from_debit``
    No entitlement exists. Finance supplies the subscription and the period;
    the entitlement is created only through the existing wallet-debit
    entitlement writer, and only when no active entitlement overlaps it
    (unless each is named).

Flow: ``preview_renewal_origin_correction`` (read-only: before/after, planned
entitlement action, blockers, warnings, projected quarantine effect, and a
fingerprint), then ``correct_renewal_origin`` which rechecks everything under
account, adjustment, subscription, ledger, and entitlement locks, requires the
identical fingerprint, applies the change through the adjustment owner's and
the entitlement writer's flush-only participants, and stages audit and a
domain event in the same transaction. Memo, description, and reason text are
never evidence.
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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.admin_alert import AdminAlert, AlertStatus
from app.models.audit import AuditActorType
from app.models.billing import (
    AccountAdjustment,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingMode, Subscription
from app.models.event_store import EventStore
from app.models.subscriber import Subscriber
from app.schemas.audit import AuditEventCreate
from app.services.audit import AuditEvents
from app.services.billing._common import (
    lock_account,
    resolve_invoice_settlement_amounts,
)
from app.services.billing.adjustments import (
    AccountAdjustmentError,
    AccountAdjustmentOrigin,
    ReviewedRenewalOriginRefCorrection,
    stage_reviewed_renewal_origin_ref_correction_for_owner,
)
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.common import round_money, to_decimal
from app.services.customer_financial_ledger import (
    invoices_entering_direct_renewal_documentary_set,
)
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
    malformed_prepaid_renewal_origin_adjustment_ids,
    parse_prepaid_renewal_origin_ref,
    preview_prepaid_coverage_reconciliation,
)
from app.services.prepaid_funding_reconstruction import (
    PrepaidFundingBaselineMissingError,
)
from app.services.service_entitlements import (
    EntitlementLinkError,
    ReviewedFundingDebitLink,
    current_prepaid_entitlement_end,
    ensure_prepaid_entitlement_for_wallet_debit,
    link_prepaid_entitlement_to_funding_debit_for_owner,
)

logger = logging.getLogger(__name__)

OWNER = "financial.prepaid_renewal_origin_correction"
CONCERN = "reviewed prepaid renewal adjustment origin correction"
#: The existing narrowly scoped grant for repairing prepaid coverage evidence
#: (seeded by migration 597); the same grant the paid-invoice period repair uses.
CORRECTION_PERMISSION = "billing:prepaid_reconciliation:repair"
RUNBOOK = "docs/runbooks/PREPAID_COVERAGE_QUARANTINE_FINANCE_REVIEW.md"
#: Must equal ``collections.scheduled.PREPAID_COVERAGE_QUARANTINE_FINDING_PREFIX``
#: (asserted by tests); duplicated so this owner does not import the runner.
QUARANTINE_FINDING_PREFIX = "prepaid-coverage:quarantine:"

_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="correct_renewal_origin",
)
_SCHEMA_VERSION = 1
#: Stable namespace for deterministic correction event identities.
_NAMESPACE = UUID("2f7d9a14-6b3e-4c58-a1d0-8e5f3c7b9a42")
_RENEWAL_ORIGIN = AccountAdjustmentOrigin.prepaid_service_renewal
_MAX_REASON_LENGTH = 500
_MIN_REASON_LENGTH = 16
_MAX_EVIDENCE_REFERENCE_LENGTH = 200
_HEX = frozenset("0123456789abcdef")
_ZERO = Decimal("0.00")


class RenewalOriginCorrectionError(DomainError):
    """Stable fail-closed error raised by this owner."""


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise RenewalOriginCorrectionError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
        retryable=False,
    )


class RenewalOriginDisposition(StrEnum):
    """How the canonical period is proven from structured coverage evidence."""

    #: One active entitlement is already linked to the debit's ledger entry.
    entitlement_already_linked = "entitlement_already_linked"
    #: Finance names an existing active entitlement the debit funded; the owner
    #: records the debit link on it. No coverage is created or extended.
    link_existing_entitlement = "link_existing_entitlement"
    #: No entitlement exists; the existing wallet-debit writer creates one for
    #: the Finance-supplied subscription and period.
    create_entitlement_from_debit = "create_entitlement_from_debit"


class EntitlementAction(StrEnum):
    none = "none"
    link_debit_to_existing = "link_debit_to_existing"
    create_from_debit = "create_from_debit"


class RenewalOriginBlocker(StrEnum):
    adjustment_not_renewal_debit = "adjustment_not_renewal_debit"
    adjustment_reversed = "adjustment_reversed"
    origin_ref_already_canonical = "origin_ref_already_canonical"
    ledger_evidence_inconsistent = "ledger_evidence_inconsistent"
    entitlement_not_found = "entitlement_not_found"
    entitlement_not_active = "entitlement_not_active"
    entitlement_account_mismatch = "entitlement_account_mismatch"
    entitlement_currency_mismatch = "entitlement_currency_mismatch"
    entitlement_period_invalid = "entitlement_period_invalid"
    entitlement_not_linked_to_debit = "entitlement_not_linked_to_debit"
    entitlement_linked_to_other_debit = "entitlement_linked_to_other_debit"
    entitlement_has_other_funding_source = "entitlement_has_other_funding_source"
    multiple_entitlements_linked_to_debit = "multiple_entitlements_linked_to_debit"
    entitlement_already_linked_to_debit = "entitlement_already_linked_to_debit"
    subscription_not_found = "subscription_not_found"
    subscription_account_mismatch = "subscription_account_mismatch"
    subscription_not_prepaid = "subscription_not_prepaid"
    overlapping_entitlement_unresolved = "overlapping_entitlement_unresolved"
    acknowledged_overlap_not_found = "acknowledged_overlap_not_found"
    canonical_origin_used_by_other_adjustment = (
        "canonical_origin_used_by_other_adjustment"
    )
    #: The entitlement's source invoice is already fully settled by active
    #: payment allocations, credit notes or opening consumption, so linking the
    #: wallet debit would fund an already-paid period twice.
    entitlement_invoice_already_settled = "entitlement_invoice_already_settled"
    #: The change would add a paid prepaid invoice to the direct-renewal
    #: documentary set, silently removing its customer-position consumption.
    would_make_invoice_documentary = "would_make_invoice_documentary"
    period_not_one_billing_cycle = "period_not_one_billing_cycle"
    period_start_outside_debit_cycle = "period_start_outside_debit_cycle"
    period_exceeds_one_billing_cycle = "period_exceeds_one_billing_cycle"
    cycle_already_covered_by_invoice = "cycle_already_covered_by_invoice"
    unacknowledged_warning = "unacknowledged_warning"
    acknowledged_warning_not_present = "acknowledged_warning_not_present"


class RenewalOriginWarning(StrEnum):
    """Facts that may be legitimate but need an explicit Finance acknowledgement."""

    #: The entitlement funds a different amount than the debit (for example the
    #: entitlement records the pre-tax price and the debit the tax-inclusive
    #: renewal charge).
    entitlement_amount_differs_from_debit = "entitlement_amount_differs_from_debit"
    #: The entitlement is also sourced from an invoice; the debit is recorded
    #: as a second funding source for the same single coverage interval.
    entitlement_invoice_backed = "entitlement_invoice_backed"


@dataclass(frozen=True, slots=True)
class RenewalOriginCorrectionQuery:
    """The operator's complete proposal; every field is part of the fingerprint."""

    adjustment_id: UUID
    disposition: RenewalOriginDisposition
    #: Required for ``entitlement_already_linked`` and ``link_existing_entitlement``.
    entitlement_id: UUID | None = None
    #: Required, with the period, for ``create_entitlement_from_debit``.
    subscription_id: UUID | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    acknowledged_overlapping_entitlement_ids: tuple[UUID, ...] = ()
    acknowledged_warnings: tuple[RenewalOriginWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class AdjustmentState:
    adjustment_id: UUID
    account_id: UUID
    amount: Decimal
    currency: str
    origin_ref: str | None
    ledger_entry_id: UUID
    ledger_amount: Decimal
    ledger_currency: str


@dataclass(frozen=True, slots=True)
class EntitlementState:
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
class PlannedEntitlementAction:
    """What the correction does to coverage evidence for this debit."""

    action: EntitlementAction
    entitlement_id: UUID | None
    subscription_id: UUID | None
    starts_at: datetime | None
    ends_at: datetime | None
    amount_funded: Decimal | None
    currency: str | None


@dataclass(frozen=True, slots=True)
class OriginQuarantineEffect:
    """Projected effect on the account's prepaid coverage quarantine.

    Informational and time-dependent, so it is not part of the fingerprint.
    """

    account_id: UUID
    as_of: datetime
    work_item_open: bool
    current_blocking_reasons: tuple[CoverageReconciliationReason, ...]
    malformed_adjustment_ids_before: tuple[UUID, ...]
    malformed_adjustment_ids_after: tuple[UUID, ...]
    corrected_period_is_current: bool
    projected_blocking_reasons: tuple[CoverageReconciliationReason, ...]

    @property
    def work_item_resolves_on_next_sweep(self) -> bool:
        return not self.projected_blocking_reasons


@dataclass(frozen=True, slots=True)
class CustomerPositionImpact:
    """Effect on the customer-position projection and prepaid coverage.

    The invoice ids are deterministic evidence and part of the fingerprint. The
    balances and coverage ends depend on ``as_of``, so they are informational.
    """

    #: Paid prepaid invoices that would enter the direct-renewal documentary
    #: set (dropping their customer-position consumption) after the change.
    invoices_made_documentary: tuple[UUID, ...]
    prepaid_available_balance_before: Decimal | None
    #: Projected: before plus the totals of the invoices made documentary.
    prepaid_available_balance_after: Decimal | None
    coverage_end_before: datetime | None
    coverage_end_after: datetime | None


@dataclass(frozen=True, slots=True)
class RenewalOriginCorrectionPreview:
    query: RenewalOriginCorrectionQuery
    account_id: UUID
    subscription_id: UUID | None
    adjustment: AdjustmentState
    entitlement: EntitlementState | None
    overlapping_entitlements: tuple[EntitlementState, ...]
    origin_ref_before: str | None
    origin_ref_after: str | None
    planned: PlannedEntitlementAction
    warnings: tuple[RenewalOriginWarning, ...]
    blockers: tuple[RenewalOriginBlocker, ...]
    quarantine_effect: OriginQuarantineEffect
    position_impact: CustomerPositionImpact
    fingerprint: str

    @property
    def actionable(self) -> bool:
        return not self.blockers


@dataclass(frozen=True, slots=True)
class CorrectRenewalOriginCommand:
    """Apply a previewed correction; the fingerprint binds it to the preview."""

    query: RenewalOriginCorrectionQuery
    preview_fingerprint: str
    reason: str
    evidence_reference: str
    evidence_sha256: str
    corrected_by: UUID
    permission_granted: bool


@dataclass(frozen=True, slots=True)
class RenewalOriginCorrectionResult:
    correction_id: UUID
    adjustment_id: UUID
    account_id: UUID
    disposition: RenewalOriginDisposition
    origin_ref_before: str | None
    origin_ref_after: str
    entitlement_id: UUID | None
    entitlement_action: EntitlementAction
    preview_fingerprint: str
    projected_blocking_reasons: tuple[CoverageReconciliationReason, ...]
    replayed: bool


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _money(value: Decimal | int | float | str | None) -> Decimal:
    return round_money(to_decimal(value))


def canonical_origin_ref(
    subscription_id: UUID, starts_at: datetime, ends_at: datetime
) -> str:
    """The exact reference shape the reconciliation owner parses."""
    return (
        f"{subscription_id}:{_utc(starts_at).isoformat()}:{_utc(ends_at).isoformat()}"
    )


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


def _validate_query(query: RenewalOriginCorrectionQuery) -> None:
    uses_entitlement = query.disposition in {
        RenewalOriginDisposition.entitlement_already_linked,
        RenewalOriginDisposition.link_existing_entitlement,
    }
    if uses_entitlement:
        if query.entitlement_id is None:
            _error("invalid_disposition", "This disposition requires an entitlement.")
        if (
            query.subscription_id is not None
            or query.period_start is not None
            or query.period_end is not None
            or query.acknowledged_overlapping_entitlement_ids
        ):
            _error(
                "invalid_disposition",
                "An existing entitlement proves its own subscription and period.",
            )
    else:
        if query.entitlement_id is not None:
            _error(
                "invalid_disposition",
                "Creating coverage from the debit cannot name an entitlement.",
            )
        if (
            query.subscription_id is None
            or query.period_start is None
            or query.period_end is None
        ):
            _error(
                "invalid_disposition",
                "Creating coverage requires a subscription and an exact period.",
            )
        start, end = query.period_start, query.period_end
        if start.utcoffset() is None or end.utcoffset() is None:
            _error(
                "invalid_period", "Period boundaries must include a timezone offset."
            )
        if _utc(end) <= _utc(start):
            _error("invalid_period", "The period end must be after its start.")
    if len(set(query.acknowledged_overlapping_entitlement_ids)) != len(
        query.acknowledged_overlapping_entitlement_ids
    ) or len(set(query.acknowledged_warnings)) != len(query.acknowledged_warnings):
        _error(
            "invalid_acknowledgement", "Acknowledgements must not contain duplicates."
        )


def _query_payload(query: RenewalOriginCorrectionQuery) -> dict[str, object]:
    return {
        "adjustment_id": str(query.adjustment_id),
        "disposition": query.disposition.value,
        "entitlement_id": (
            str(query.entitlement_id) if query.entitlement_id is not None else None
        ),
        "subscription_id": (
            str(query.subscription_id) if query.subscription_id is not None else None
        ),
        "period_start": (
            _utc(query.period_start).isoformat()
            if query.period_start is not None
            else None
        ),
        "period_end": (
            _utc(query.period_end).isoformat() if query.period_end is not None else None
        ),
        "acknowledged_overlapping_entitlement_ids": sorted(
            str(value) for value in query.acknowledged_overlapping_entitlement_ids
        ),
        "acknowledged_warnings": sorted(
            value.value for value in query.acknowledged_warnings
        ),
    }


def _entitlement_state(row: ServiceEntitlement) -> EntitlementState:
    status = row.status
    return EntitlementState(
        entitlement_id=row.id,
        subscription_id=row.subscription_id,
        account_id=row.account_id,
        status=status.value if isinstance(status, ServiceEntitlementStatus) else "",
        starts_at=_utc(row.starts_at),
        ends_at=_utc(row.ends_at),
        amount_funded=_money(row.amount_funded),
        currency=(row.currency or "").upper(),
        source_invoice_id=row.source_invoice_id,
        source_invoice_line_id=row.source_invoice_line_id,
        source_ledger_entry_id=row.source_ledger_entry_id,
    )


def _entitlement_payload(state: EntitlementState | None) -> dict[str, object] | None:
    if state is None:
        return None
    return {
        "id": state.entitlement_id,
        "subscription_id": state.subscription_id,
        "account_id": state.account_id,
        "status": state.status,
        "starts_at": state.starts_at,
        "ends_at": state.ends_at,
        "amount_funded": state.amount_funded,
        "currency": state.currency,
        "source_invoice_id": state.source_invoice_id,
        "source_invoice_line_id": state.source_invoice_line_id,
        "source_ledger_entry_id": state.source_ledger_entry_id,
    }


# ---------------------------------------------------------------------------
# Preview (read-only query)
# ---------------------------------------------------------------------------


def _other_funding_source(row: ServiceEntitlement) -> bool:
    return any(
        value is not None
        for value in (
            row.source_billing_grant_id,
            row.source_pause_episode_id,
            row.source_outage_compensation_id,
        )
    )


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
    adjustment: AccountAdjustment,
    corrected: bool,
    period: tuple[datetime, datetime] | None,
    as_of: datetime,
) -> OriginQuarantineEffect:
    """Project the quarantine using the reconciliation owner's own queries."""
    cohort = list(
        db.scalars(
            select(Subscription)
            .where(
                Subscription.subscriber_id == adjustment.account_id,
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
    current = {
        item.reason
        for item in preview.items
        if item.decision is CoverageReconciliationDecision.quarantined
        and item.reason in blocking
    }
    before = malformed_prepaid_renewal_origin_adjustment_ids(db, cohort, as_of=as_of)
    # The corrected adjustment stops being malformed only when the canonical
    # reference is accepted by the owner's parser. Its ledger pair is verified
    # by the preview, so a ledger disagreement is a blocker, not a projection.
    after = before - {adjustment.id} if corrected else before
    # Subscriptions that were quarantined for the renewal origin resolve to
    # whatever their remaining evidence says; the other blocking classes keep
    # their reason regardless of this correction.
    projected: set[CoverageReconciliationReason] = {
        item.reason
        for item in preview.items
        if item.decision is CoverageReconciliationDecision.quarantined
        and item.reason in blocking
        and item.reason is not CoverageReconciliationReason.malformed_renewal_origin
    }
    if after:
        projected.add(CoverageReconciliationReason.malformed_renewal_origin)
    is_current = bool(
        corrected and period is not None and period[0] <= as_of < period[1]
    )
    return OriginQuarantineEffect(
        account_id=adjustment.account_id,
        as_of=as_of,
        work_item_open=_work_item_open(db, adjustment.account_id),
        current_blocking_reasons=tuple(sorted(current, key=lambda r: r.value)),
        malformed_adjustment_ids_before=tuple(sorted(before, key=str)),
        malformed_adjustment_ids_after=tuple(sorted(after, key=str)),
        corrected_period_is_current=is_current,
        projected_blocking_reasons=tuple(sorted(projected, key=lambda r: r.value)),
    )


def _position_impact(
    db: Session,
    *,
    query: RenewalOriginCorrectionQuery,
    adjustment: AccountAdjustment,
    subscription: Subscription | None,
    planned: PlannedEntitlementAction,
    as_of: datetime,
) -> CustomerPositionImpact:
    """Compare the customer-position projection and coverage before and after.

    Only the link and create dispositions change which entitlement is linked to
    the debit, so only they can move an invoice into the documentary set.
    """
    from app.services.customer_financial_position import prepaid_available_balance

    changes_linkage = planned.action is not EntitlementAction.none
    made_documentary: tuple[UUID, ...] = ()
    if (
        changes_linkage
        and subscription is not None
        and planned.starts_at is not None
        and planned.ends_at is not None
    ):
        made_documentary = invoices_entering_direct_renewal_documentary_set(
            db,
            account_id=adjustment.account_id,
            subscription_id=subscription.id,
            starts_at=planned.starts_at,
            ends_at=planned.ends_at,
            amount=adjustment.amount,
            currency=adjustment.currency,
        )
    balance_before: Decimal | None = None
    balance_after: Decimal | None = None
    try:
        balance_before = _money(
            prepaid_available_balance(
                db, adjustment.account_id, currency=adjustment.currency
            )
        )
        removed = _money(
            sum(
                (
                    _money(total)
                    for total in db.scalars(
                        select(Invoice.total).where(Invoice.id.in_(made_documentary))
                    ).all()
                ),
                _ZERO,
            )
            if made_documentary
            else _ZERO
        )
        balance_after = _money(balance_before + removed)
    except (ValueError, DomainError, PrepaidFundingBaselineMissingError):
        # No materialized funding authority: the balance is informational only.
        balance_before = balance_after = None
    coverage_before: datetime | None = None
    coverage_after: datetime | None = None
    if subscription is not None:
        current = current_prepaid_entitlement_end(
            db,
            subscription_id=subscription.id,
            account_id=adjustment.account_id,
            now=as_of,
        )
        coverage_before = _utc(current) if current is not None else None
        coverage_after = coverage_before
        if (
            planned.action is EntitlementAction.create_from_debit
            and planned.starts_at is not None
            and planned.ends_at is not None
            and planned.starts_at <= as_of < planned.ends_at
        ):
            coverage_after = max(
                value for value in (coverage_before, planned.ends_at) if value
            )
    return CustomerPositionImpact(
        invoices_made_documentary=made_documentary,
        prepaid_available_balance_before=balance_before,
        prepaid_available_balance_after=balance_after,
        coverage_end_before=coverage_before,
        coverage_end_after=coverage_after,
    )


def preview_renewal_origin_correction(
    db: Session,
    query: RenewalOriginCorrectionQuery,
    *,
    as_of: datetime | None = None,
) -> RenewalOriginCorrectionPreview:
    """Validate one proposed origin correction without changing state."""
    _validate_query(query)
    observed_at = _utc(as_of or datetime.now(UTC))

    adjustment = db.get(AccountAdjustment, query.adjustment_id)
    if adjustment is None:
        _error("adjustment_not_found", "The account adjustment was not found.")
    entry = db.get(LedgerEntry, adjustment.ledger_entry_id)
    if entry is None:
        _error(
            "ledger_entry_not_found",
            "The adjustment's ledger debit was not found.",
            adjustment_id=str(adjustment.id),
        )

    blockers: set[RenewalOriginBlocker] = set()
    warnings: set[RenewalOriginWarning] = set()
    currency = (adjustment.currency or "").upper()
    amount = _money(adjustment.amount)

    if (
        adjustment.origin != _RENEWAL_ORIGIN.value
        or adjustment.category is not LedgerCategory.internet_service
    ):
        blockers.add(RenewalOriginBlocker.adjustment_not_renewal_debit)
    if adjustment.reversed_at is not None or adjustment.reversal_ledger_entry_id:
        blockers.add(RenewalOriginBlocker.adjustment_reversed)
    if parse_prepaid_renewal_origin_ref(adjustment.origin_ref) is not None:
        blockers.add(RenewalOriginBlocker.origin_ref_already_canonical)
    if (
        not entry.is_active
        or entry.entry_type is not LedgerEntryType.debit
        or entry.source is not LedgerSource.adjustment
        or entry.invoice_id is not None
        or entry.account_id != adjustment.account_id
        or _money(entry.amount) != amount
        or (entry.currency or "").upper() != currency
    ):
        blockers.add(RenewalOriginBlocker.ledger_evidence_inconsistent)

    linked_rows = list(
        db.scalars(
            select(ServiceEntitlement)
            .where(
                ServiceEntitlement.source_ledger_entry_id == entry.id,
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
            )
            .order_by(ServiceEntitlement.starts_at, ServiceEntitlement.id)
        ).all()
    )

    subscription: Subscription | None = None
    entitlement_row: ServiceEntitlement | None = None
    period: tuple[datetime, datetime] | None = None
    planned = PlannedEntitlementAction(
        action=EntitlementAction.none,
        entitlement_id=None,
        subscription_id=None,
        starts_at=None,
        ends_at=None,
        amount_funded=None,
        currency=None,
    )
    overlap_rows: list[ServiceEntitlement] = []
    acknowledged = set(query.acknowledged_overlapping_entitlement_ids)

    if query.disposition in {
        RenewalOriginDisposition.entitlement_already_linked,
        RenewalOriginDisposition.link_existing_entitlement,
    }:
        assert query.entitlement_id is not None
        entitlement_row = db.get(ServiceEntitlement, query.entitlement_id)
        if entitlement_row is None:
            blockers.add(RenewalOriginBlocker.entitlement_not_found)
        else:
            if entitlement_row.status is not ServiceEntitlementStatus.active:
                blockers.add(RenewalOriginBlocker.entitlement_not_active)
            if entitlement_row.account_id != adjustment.account_id:
                blockers.add(RenewalOriginBlocker.entitlement_account_mismatch)
            if (entitlement_row.currency or "").upper() != currency:
                blockers.add(RenewalOriginBlocker.entitlement_currency_mismatch)
            if _utc(entitlement_row.ends_at) <= _utc(entitlement_row.starts_at):
                blockers.add(RenewalOriginBlocker.entitlement_period_invalid)
            if _other_funding_source(entitlement_row):
                blockers.add(RenewalOriginBlocker.entitlement_has_other_funding_source)
            subscription = db.get(Subscription, entitlement_row.subscription_id)
            if query.disposition is RenewalOriginDisposition.entitlement_already_linked:
                if len(linked_rows) > 1:
                    blockers.add(
                        RenewalOriginBlocker.multiple_entitlements_linked_to_debit
                    )
                elif entitlement_row.source_ledger_entry_id != entry.id:
                    blockers.add(RenewalOriginBlocker.entitlement_not_linked_to_debit)
                planned = PlannedEntitlementAction(
                    action=EntitlementAction.none,
                    entitlement_id=entitlement_row.id,
                    subscription_id=entitlement_row.subscription_id,
                    starts_at=_utc(entitlement_row.starts_at),
                    ends_at=_utc(entitlement_row.ends_at),
                    amount_funded=_money(entitlement_row.amount_funded),
                    currency=(entitlement_row.currency or "").upper(),
                )
            else:
                if linked_rows:
                    blockers.add(
                        RenewalOriginBlocker.entitlement_already_linked_to_debit
                    )
                if (
                    entitlement_row.source_ledger_entry_id is not None
                    and entitlement_row.source_ledger_entry_id != entry.id
                ):
                    blockers.add(RenewalOriginBlocker.entitlement_linked_to_other_debit)
                elif entitlement_row.source_ledger_entry_id == entry.id:
                    blockers.add(
                        RenewalOriginBlocker.entitlement_already_linked_to_debit
                    )
                planned = PlannedEntitlementAction(
                    action=EntitlementAction.link_debit_to_existing,
                    entitlement_id=entitlement_row.id,
                    subscription_id=entitlement_row.subscription_id,
                    starts_at=_utc(entitlement_row.starts_at),
                    ends_at=_utc(entitlement_row.ends_at),
                    amount_funded=_money(entitlement_row.amount_funded),
                    currency=(entitlement_row.currency or "").upper(),
                )
            if _money(entitlement_row.amount_funded) != amount:
                warnings.add(RenewalOriginWarning.entitlement_amount_differs_from_debit)
            if entitlement_row.source_invoice_id is not None:
                warnings.add(RenewalOriginWarning.entitlement_invoice_backed)
                if (
                    query.disposition
                    is RenewalOriginDisposition.link_existing_entitlement
                ):
                    source_invoice = db.get(Invoice, entitlement_row.source_invoice_id)
                    if (
                        source_invoice is not None
                        and _money(source_invoice.total) > _ZERO
                        and resolve_invoice_settlement_amounts(
                            db, source_invoice.id
                        ).total_applied
                        >= _money(source_invoice.total)
                    ):
                        blockers.add(
                            RenewalOriginBlocker.entitlement_invoice_already_settled
                        )
            period = (_utc(entitlement_row.starts_at), _utc(entitlement_row.ends_at))
    else:
        assert query.subscription_id is not None
        assert query.period_start is not None and query.period_end is not None
        start, end = _utc(query.period_start), _utc(query.period_end)
        subscription = db.get(Subscription, query.subscription_id)
        if linked_rows:
            blockers.add(RenewalOriginBlocker.entitlement_already_linked_to_debit)
        if subscription is not None:
            overlap_rows = list(
                db.scalars(
                    select(ServiceEntitlement)
                    .where(
                        ServiceEntitlement.subscription_id == subscription.id,
                        ServiceEntitlement.status == ServiceEntitlementStatus.active,
                        ServiceEntitlement.starts_at < end,
                        ServiceEntitlement.ends_at > start,
                    )
                    .order_by(ServiceEntitlement.starts_at, ServiceEntitlement.id)
                ).all()
            )
            overlap_ids = {row.id for row in overlap_rows}
            if acknowledged - overlap_ids:
                blockers.add(RenewalOriginBlocker.acknowledged_overlap_not_found)
            if overlap_ids - acknowledged:
                blockers.add(RenewalOriginBlocker.overlapping_entitlement_unresolved)
            from app.services.catalog.subscriptions import (
                _resolve_billing_cycle,
                billing_cycle_end,
            )

            cycle = _resolve_billing_cycle(
                db,
                str(subscription.offer_id),
                str(subscription.offer_version_id)
                if subscription.offer_version_id
                else None,
                override=subscription.billing_cycle,
            )
            if _utc(billing_cycle_end(start, cycle)) != end:
                blockers.add(RenewalOriginBlocker.period_not_one_billing_cycle)
            if end > _utc(billing_cycle_end(start, cycle)):
                blockers.add(RenewalOriginBlocker.period_exceeds_one_billing_cycle)
            debit_at = _utc(entry.effective_date or entry.created_at)
            if (
                start > _utc(billing_cycle_end(debit_at, cycle))
                or _utc(billing_cycle_end(start, cycle)) < debit_at
            ):
                blockers.add(RenewalOriginBlocker.period_start_outside_debit_cycle)
            if any(row.source_invoice_id is not None for row in overlap_rows) or (
                db.scalar(
                    select(Invoice.id)
                    .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
                    .where(
                        Invoice.account_id == adjustment.account_id,
                        Invoice.is_active.is_(True),
                        Invoice.status == InvoiceStatus.paid,
                        InvoiceLine.is_active.is_(True),
                        InvoiceLine.subscription_id == subscription.id,
                        Invoice.billing_period_start < end,
                        Invoice.billing_period_end > start,
                    )
                    .limit(1)
                )
                is not None
            ):
                blockers.add(RenewalOriginBlocker.cycle_already_covered_by_invoice)
        planned = PlannedEntitlementAction(
            action=EntitlementAction.create_from_debit,
            entitlement_id=None,
            subscription_id=query.subscription_id,
            starts_at=start,
            ends_at=end,
            amount_funded=amount,
            currency=currency,
        )
        period = (start, end)

    if subscription is None:
        blockers.add(RenewalOriginBlocker.subscription_not_found)
    else:
        if subscription.subscriber_id != adjustment.account_id:
            blockers.add(RenewalOriginBlocker.subscription_account_mismatch)
        if subscription.billing_mode != BillingMode.prepaid:
            blockers.add(RenewalOriginBlocker.subscription_not_prepaid)

    origin_after: str | None = None
    if subscription is not None and period is not None:
        origin_after = canonical_origin_ref(subscription.id, period[0], period[1])
        duplicate = db.scalar(
            select(AccountAdjustment.id).where(
                AccountAdjustment.account_id == adjustment.account_id,
                AccountAdjustment.id != adjustment.id,
                AccountAdjustment.origin == _RENEWAL_ORIGIN.value,
                AccountAdjustment.reversed_at.is_(None),
                AccountAdjustment.origin_ref == origin_after,
            )
        )
        if duplicate is not None:
            blockers.add(RenewalOriginBlocker.canonical_origin_used_by_other_adjustment)
        if parse_prepaid_renewal_origin_ref(origin_after) is None:
            blockers.add(RenewalOriginBlocker.entitlement_period_invalid)

    position_impact = _position_impact(
        db,
        query=query,
        adjustment=adjustment,
        subscription=subscription,
        planned=planned,
        as_of=observed_at,
    )
    if position_impact.invoices_made_documentary:
        blockers.add(RenewalOriginBlocker.would_make_invoice_documentary)

    acknowledged_warnings = set(query.acknowledged_warnings)
    if warnings - acknowledged_warnings:
        blockers.add(RenewalOriginBlocker.unacknowledged_warning)
    if acknowledged_warnings - warnings:
        blockers.add(RenewalOriginBlocker.acknowledged_warning_not_present)

    adjustment_state = AdjustmentState(
        adjustment_id=adjustment.id,
        account_id=adjustment.account_id,
        amount=amount,
        currency=currency,
        origin_ref=adjustment.origin_ref,
        ledger_entry_id=entry.id,
        ledger_amount=_money(entry.amount),
        ledger_currency=(entry.currency or "").upper(),
    )
    entitlement_state = (
        _entitlement_state(entitlement_row) if entitlement_row is not None else None
    )
    overlap_states = tuple(_entitlement_state(row) for row in overlap_rows)
    ordered_warnings = tuple(sorted(warnings, key=lambda value: value.value))
    ordered_blockers = tuple(sorted(blockers, key=lambda value: value.value))
    effect = _quarantine_effect(
        db,
        adjustment=adjustment,
        corrected=origin_after is not None and not blockers,
        period=period,
        as_of=observed_at,
    )
    fingerprint = _hash(
        {
            "owner": OWNER,
            "schema_version": _SCHEMA_VERSION,
            "query": _query_payload(query),
            "adjustment": {
                "id": adjustment_state.adjustment_id,
                "account_id": adjustment_state.account_id,
                "amount": adjustment_state.amount,
                "currency": adjustment_state.currency,
                "origin_ref": adjustment_state.origin_ref,
                "ledger_entry_id": adjustment_state.ledger_entry_id,
                "ledger_amount": adjustment_state.ledger_amount,
                "ledger_currency": adjustment_state.ledger_currency,
                "reversed": adjustment.reversed_at is not None,
            },
            "entitlement": _entitlement_payload(entitlement_state),
            "linked_to_debit": [row.id for row in linked_rows],
            "overlaps": [_entitlement_payload(row) for row in overlap_states],
            "origin_ref_after": origin_after,
            "planned": {
                "action": planned.action,
                "entitlement_id": planned.entitlement_id,
                "subscription_id": planned.subscription_id,
                "starts_at": planned.starts_at,
                "ends_at": planned.ends_at,
                "amount_funded": planned.amount_funded,
                "currency": planned.currency,
            },
            "invoices_made_documentary": [
                str(value) for value in position_impact.invoices_made_documentary
            ],
            "malformed_before": [
                str(value) for value in effect.malformed_adjustment_ids_before
            ],
            "warnings": list(ordered_warnings),
            "blockers": list(ordered_blockers),
        }
    )
    return RenewalOriginCorrectionPreview(
        query=query,
        account_id=adjustment.account_id,
        subscription_id=subscription.id if subscription is not None else None,
        adjustment=adjustment_state,
        entitlement=entitlement_state,
        overlapping_entitlements=overlap_states,
        origin_ref_before=adjustment.origin_ref,
        origin_ref_after=origin_after,
        planned=planned,
        warnings=ordered_warnings,
        blockers=ordered_blockers,
        quarantine_effect=effect,
        position_impact=position_impact,
        fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def _correction_event_id(idempotency_key: str) -> UUID:
    return uuid5(_NAMESPACE, f"{OWNER}:correct:{idempotency_key}")


def _stored_event(db: Session, event_id: UUID) -> dict[str, object] | None:
    event = db.execute(
        select(EventStore).where(
            EventStore.event_id == event_id,
            EventStore.event_type == EventType.prepaid_renewal_origin_corrected.value,
        )
    ).scalar_one_or_none()
    return dict(event.payload or {}) if event is not None else None


def _require_staff(
    db: Session,
    *,
    context: CommandContext,
    system_user_id: UUID,
    permission_granted: bool,
) -> None:
    from app.models.system_user import SystemUser

    if context.scope != CORRECTION_PERMISSION or not permission_granted:
        _error(
            "permission_denied", f"The {CORRECTION_PERMISSION} permission is required."
        )
    user = db.get(SystemUser, system_user_id)
    if user is None or not user.is_active:
        _error(
            "invalid_actor", "The staff member must be an existing, active system user."
        )


def _validated_evidence(command: CorrectRenewalOriginCommand) -> str:
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


def _result_from_event(
    payload: Mapping[str, object], *, replayed: bool
) -> RenewalOriginCorrectionResult:
    entitlement = payload.get("entitlement_id")
    return RenewalOriginCorrectionResult(
        correction_id=UUID(str(payload["correction_id"])),
        adjustment_id=UUID(str(payload["adjustment_id"])),
        account_id=UUID(str(payload["account_id"])),
        disposition=RenewalOriginDisposition(str(payload["disposition"])),
        origin_ref_before=(
            str(payload["origin_ref_before"])
            if payload.get("origin_ref_before") is not None
            else None
        ),
        origin_ref_after=str(payload["origin_ref_after"]),
        entitlement_id=UUID(str(entitlement)) if entitlement else None,
        entitlement_action=EntitlementAction(str(payload["entitlement_action"])),
        preview_fingerprint=str(payload["preview_fingerprint"]),
        projected_blocking_reasons=tuple(
            CoverageReconciliationReason(str(value))
            for value in _string_list(payload.get("projected_blocking_reasons"))
        ),
        replayed=replayed,
    )


def _string_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _replay(
    db: Session,
    correction_id: UUID,
    *,
    command: CorrectRenewalOriginCommand,
    digest: str,
) -> RenewalOriginCorrectionResult | None:
    """Return the stored outcome for this key, or refuse a different proposal."""
    stored = _stored_event(db, correction_id)
    if stored is None:
        return None
    if (
        stored.get("query") != _query_payload(command.query)
        or stored.get("preview_fingerprint") != command.preview_fingerprint
        or stored.get("evidence_sha256") != digest
        or stored.get("corrected_by_system_user_id") != str(command.corrected_by)
    ):
        _error(
            "idempotency_conflict",
            "This idempotency key already recorded a different correction.",
        )
    return _result_from_event(stored, replayed=True)


def _lock_chain(db: Session, query: RenewalOriginCorrectionQuery) -> None:
    """Lock account, adjustment, ledger entry, subscription, then entitlements."""
    adjustment = db.get(AccountAdjustment, query.adjustment_id)
    if adjustment is None:
        _error("adjustment_not_found", "The account adjustment was not found.")
    lock_account(db, str(adjustment.account_id))
    db.execute(
        select(Subscriber.id)
        .where(Subscriber.id == adjustment.account_id)
        .with_for_update()
    ).all()
    db.execute(
        select(AccountAdjustment.id)
        .where(AccountAdjustment.id == adjustment.id)
        .with_for_update()
    ).all()
    db.execute(
        select(LedgerEntry.id)
        .where(LedgerEntry.id == adjustment.ledger_entry_id)
        .with_for_update()
    ).all()
    subscription_id = query.subscription_id
    if subscription_id is None and query.entitlement_id is not None:
        subscription_id = db.scalar(
            select(ServiceEntitlement.subscription_id).where(
                ServiceEntitlement.id == query.entitlement_id
            )
        )
    if subscription_id is not None:
        db.execute(
            select(Subscription.id)
            .where(Subscription.id == subscription_id)
            .with_for_update()
        ).all()
        db.execute(
            select(ServiceEntitlement.id)
            .where(ServiceEntitlement.subscription_id == subscription_id)
            .order_by(ServiceEntitlement.id)
            .with_for_update()
        ).all()
    # Re-read every locked row so the recomputed preview sees committed state.
    db.expire_all()


def correct_renewal_origin(
    db: Session,
    command: CorrectRenewalOriginCommand,
    *,
    context: CommandContext,
) -> RenewalOriginCorrectionResult:
    """Apply one fingerprint-bound origin correction atomically."""
    return execute_owner_command(
        db,
        definition=_COMMAND,
        context=context,
        operation=lambda: _correct(db, command=command, context=context),
    )


def _correct(
    db: Session,
    *,
    command: CorrectRenewalOriginCommand,
    context: CommandContext,
) -> RenewalOriginCorrectionResult:
    key = (context.idempotency_key or "").strip()
    if not key:
        _error(
            "missing_idempotency_key",
            "A renewal origin correction requires a business idempotency key.",
        )
    _require_staff(
        db,
        context=context,
        system_user_id=command.corrected_by,
        permission_granted=command.permission_granted,
    )
    _validate_query(command.query)
    digest = _validated_evidence(command)
    correction_id = _correction_event_id(key)

    replay = _replay(db, correction_id, command=command, digest=digest)
    if replay is not None:
        return replay

    _lock_chain(db, command.query)
    # A concurrent confirmation with this key may have committed while this one
    # waited for the locks: converge on its stored outcome instead of reporting
    # the (now corrected) evidence as stale.
    replay = _replay(db, correction_id, command=command, digest=digest)
    if replay is not None:
        return replay
    preview = preview_renewal_origin_correction(db, command.query)
    if preview.fingerprint != command.preview_fingerprint:
        _error(
            "stale_preview",
            "The renewal evidence changed after preview; preview again.",
            expected_fingerprint=command.preview_fingerprint,
            current_fingerprint=preview.fingerprint,
        )
    if not preview.actionable or preview.origin_ref_after is None:
        _error(
            "not_actionable",
            "The previewed correction has blockers and cannot be applied.",
            blockers=[value.value for value in preview.blockers],
        )
    planned = preview.planned
    evidence_ref = f"{OWNER}:{correction_id}"
    entitlement_id = planned.entitlement_id

    if planned.action is EntitlementAction.link_debit_to_existing:
        assert planned.entitlement_id is not None
        try:
            link_prepaid_entitlement_to_funding_debit_for_owner(
                db,
                ReviewedFundingDebitLink(
                    entitlement_id=planned.entitlement_id,
                    ledger_entry_id=preview.adjustment.ledger_entry_id,
                    adjustment_id=preview.adjustment.adjustment_id,
                    evidence_ref=evidence_ref,
                ),
            )
        except EntitlementLinkError as exc:
            _error(
                "participant_rejected",
                "The entitlement writer rejected the reviewed debit link.",
                participant_error=str(exc),
            )
    elif planned.action is EntitlementAction.create_from_debit:
        assert planned.subscription_id is not None
        assert planned.starts_at is not None and planned.ends_at is not None
        subscription = db.get(Subscription, planned.subscription_id)
        entry = db.get(LedgerEntry, preview.adjustment.ledger_entry_id)
        if subscription is None or entry is None:
            _error("incomplete_correction", "Reviewed evidence disappeared.")
        entitlement = ensure_prepaid_entitlement_for_wallet_debit(
            db,
            subscription=subscription,
            ledger_entry=entry,
            starts_at=planned.starts_at,
            ends_at=planned.ends_at,
        )
        if (
            entitlement is None
            or entitlement.source_ledger_entry_id != entry.id
            or entitlement.subscription_id != planned.subscription_id
            or entitlement.account_id != preview.account_id
            or _utc(entitlement.starts_at) != planned.starts_at
            or _utc(entitlement.ends_at) != planned.ends_at
            or _money(entitlement.amount_funded) != planned.amount_funded
            or (entitlement.currency or "").upper() != planned.currency
        ):
            _error(
                "incomplete_correction",
                "The entitlement writer did not produce the reviewed entitlement.",
            )
        entitlement_id = entitlement.id

    try:
        stage_reviewed_renewal_origin_ref_correction_for_owner(
            db,
            ReviewedRenewalOriginRefCorrection(
                adjustment_id=preview.adjustment.adjustment_id,
                expected_origin_ref=preview.origin_ref_before,
                canonical_origin_ref=preview.origin_ref_after,
                evidence_ref=evidence_ref,
            ),
        )
    except AccountAdjustmentError as exc:
        _error(
            "participant_rejected",
            "The adjustment owner rejected the reviewed reference correction.",
            participant_error=exc.code,
        )

    effect = preview.quarantine_effect
    now = datetime.now(UTC)
    shared: dict[str, object] = {
        "correction_id": str(correction_id),
        "query": _query_payload(command.query),
        "adjustment_id": str(preview.adjustment.adjustment_id),
        "account_id": str(preview.account_id),
        "subscription_id": (
            str(preview.subscription_id) if preview.subscription_id else None
        ),
        "disposition": command.query.disposition.value,
        "origin_ref_before": preview.origin_ref_before,
        "origin_ref_after": preview.origin_ref_after,
        "entitlement_id": str(entitlement_id) if entitlement_id else None,
        "entitlement_action": planned.action.value,
        "ledger_entry_id": str(preview.adjustment.ledger_entry_id),
        "amount": str(preview.adjustment.amount),
        "currency": preview.adjustment.currency,
        "acknowledged_warnings": [value.value for value in preview.warnings],
        "economic_delta": "0.00",
        "preview_fingerprint": preview.fingerprint,
        "reason": command.reason.strip(),
        "evidence_reference": command.evidence_reference.strip(),
        "evidence_sha256": digest,
        "corrected_by_system_user_id": str(command.corrected_by),
        "projected_blocking_reasons": [
            value.value for value in effect.projected_blocking_reasons
        ],
    }
    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=str(command.corrected_by),
            action="correct_prepaid_renewal_origin_ref",
            entity_type="account_adjustment",
            entity_id=str(preview.adjustment.adjustment_id),
            metadata_=dict(shared),
        ),
    )
    emit_event(
        db,
        EventType.prepaid_renewal_origin_corrected,
        {
            "schema_version": _SCHEMA_VERSION,
            **shared,
            "corrected_at": now.isoformat(),
            "actor": context.actor,
            "command_id": str(context.command_id),
            "idempotency_key": key,
        },
        event_id=correction_id,
        actor=context.actor,
        subscriber_id=preview.account_id,
        account_id=preview.account_id,
        subscription_id=preview.subscription_id,
    )
    db.flush()
    logger.info(
        "prepaid_renewal_origin_corrected: correction=%s adjustment=%s "
        "action=%s work_item_resolves_on_next_sweep=%s",
        correction_id,
        preview.adjustment.adjustment_id,
        planned.action.value,
        effect.work_item_resolves_on_next_sweep,
    )
    return RenewalOriginCorrectionResult(
        correction_id=correction_id,
        adjustment_id=preview.adjustment.adjustment_id,
        account_id=preview.account_id,
        disposition=command.query.disposition,
        origin_ref_before=preview.origin_ref_before,
        origin_ref_after=preview.origin_ref_after,
        entitlement_id=entitlement_id,
        entitlement_action=planned.action,
        preview_fingerprint=preview.fingerprint,
        projected_blocking_reasons=effect.projected_blocking_reasons,
        replayed=False,
    )


__all__ = [
    "CONCERN",
    "CORRECTION_PERMISSION",
    "OWNER",
    "QUARANTINE_FINDING_PREFIX",
    "RUNBOOK",
    "AdjustmentState",
    "CorrectRenewalOriginCommand",
    "CustomerPositionImpact",
    "EntitlementAction",
    "EntitlementState",
    "OriginQuarantineEffect",
    "PlannedEntitlementAction",
    "RenewalOriginBlocker",
    "RenewalOriginCorrectionError",
    "RenewalOriginCorrectionPreview",
    "RenewalOriginCorrectionQuery",
    "RenewalOriginCorrectionResult",
    "RenewalOriginDisposition",
    "RenewalOriginWarning",
    "canonical_origin_ref",
    "correct_renewal_origin",
    "preview_renewal_origin_correction",
]
