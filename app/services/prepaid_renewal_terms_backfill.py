"""Prepaid renewal-terms backfill (ADR 0007 stage 3, migration owner).

Prepaid enforcement fails closed with ``renewal_terms_unresolved`` when an
active prepaid subscription carries no frozen contracted amount
(``Subscription.unit_price`` NULL or <= 0). The contracted amount is never
inferred from the mutable catalog (ADR 0007 Phase 1): this owner restores it
only from the subscription's own exact evidence — the base-subscription lines
of its PAID invoices. A subscription whose paid evidence is absent or
self-contradictory becomes an owned, SLA-bound finance work item and stays
fail-closed.

TRANSITIONAL: retire at the ADR 0007 Phase 1 cutover, when
``billing.contracts`` becomes authoritative and ``Subscription.unit_price``
stops being the renewal-charge authority.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
from app.models.catalog import BillingMode, Subscription
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

logger = logging.getLogger(__name__)

OWNER = "financial.prepaid_renewal_terms_backfill"

_CAPTURE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="prepaid renewal-terms evidence backfill",
    name="capture_prepaid_renewal_terms_backfill",
)

_POLICY_VERSION = "prepaid-renewal-terms-backfill-v2"
#: Public so the prepaid enforcement snapshot can count these work items.
RENEWAL_TERMS_FINDING_PREFIX = "prepaid-renewal-terms:evidence:"
_FINDING_PREFIX = RENEWAL_TERMS_FINDING_PREFIX
#: Finance review window recorded on each unresolved-evidence work item.
_EVIDENCE_SLA_HOURS = 72
#: The team that owns finance-review work items and their alerts. It is the
#: same label the ``deploy/observability`` alert rules route on; an
#: architecture test pins the two together.
RENEWAL_TERMS_WORK_ITEM_OWNER = "financial-billing"
#: Operator runbook linked from every renewal-terms work item and alert.
RENEWAL_TERMS_RUNBOOK = "docs/runbooks/PREPAID_RENEWAL_TERMS_FINANCE_REVIEW.md"
#: Narrow RBAC permission both the requesting and the approving staff member
#: must hold for a finance-reviewed renewal-term record. Real access control
#: lives at the invocation boundary (the operator CLI resolves a named staff
#: principal's granted roles via ``has_permission`` and passes the result as
#: ``permission_granted``); this owner refuses when that evidence is missing.
RENEWAL_TERM_RECORD_PERMISSION = "billing:renewal_terms:record"


class PrepaidRenewalTermsBackfillError(DomainError):
    """Fail-closed renewal-terms backfill error."""


def _error(code: str, message: str) -> PrepaidRenewalTermsBackfillError:
    return PrepaidRenewalTermsBackfillError(code=code, message=message)


class RenewalTermsDecision(StrEnum):
    repairable = "repairable"
    ambiguous_amounts = "ambiguous_amounts"
    insufficient_cycle_evidence = "insufficient_cycle_evidence"
    missing_charge_inputs = "missing_charge_inputs"
    no_evidence = "no_evidence"


_PRORATION_MARKERS = ("proration", "prorated", "pro_rata")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _is_canonical_full_cycle(start: datetime | None, end: datetime | None) -> bool:
    """One canonical month by the renewal-billing owner's own arithmetic."""
    from app.services.billing_automation import _add_months

    if start is None or end is None:
        return False
    start_aware = _aware(start)
    end_aware = _aware(end)
    assert start_aware is not None and end_aware is not None
    return end_aware == _aware(_add_months(start_aware, 1))


@dataclass(frozen=True, slots=True)
class PaidLineEvidence:
    """One paid base-subscription invoice line, fully identity-bound.

    The v2 fingerprint covers every field, so any evidence change — even one
    that leaves the classified amount identical — invalidates a reviewed
    preview.
    """

    invoice_id: UUID
    invoice_line_id: UUID
    unit_price: Decimal
    quantity: Decimal
    amount: Decimal
    currency: str
    period_start: datetime | None
    period_end: datetime | None
    proration_marker: str | None
    full_cycle: bool
    compatible: bool
    incompatibility: str | None

    def as_payload(self) -> dict[str, str | None]:
        return {
            "invoice_id": str(self.invoice_id),
            "invoice_line_id": str(self.invoice_line_id),
            "unit_price": str(self.unit_price),
            "quantity": str(self.quantity),
            "amount": str(self.amount),
            "currency": self.currency,
            "period_start": (
                self.period_start.isoformat() if self.period_start else None
            ),
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "proration_marker": self.proration_marker,
            "full_cycle": str(self.full_cycle),
            "compatible": str(self.compatible),
            "incompatibility": self.incompatibility,
        }


@dataclass(frozen=True, slots=True)
class RenewalTermsEvidenceItem:
    """Exact-evidence verdict for one blocked prepaid subscription."""

    subscription_id: UUID
    account_id: UUID
    decision: RenewalTermsDecision
    contracted_amount: Decimal | None
    distinct_paid_amounts: tuple[Decimal, ...]
    paid_line_count: int
    evidence: tuple[PaidLineEvidence, ...] = ()
    insufficiency_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RenewalTermsBackfillPreview:
    as_of: datetime
    items: tuple[RenewalTermsEvidenceItem, ...]
    fingerprint: str

    @property
    def repairable_count(self) -> int:
        return sum(
            1 for i in self.items if i.decision is RenewalTermsDecision.repairable
        )

    @property
    def unresolved_count(self) -> int:
        return len(self.items) - self.repairable_count


@dataclass(frozen=True, slots=True)
class CaptureRenewalTermsBackfillCommand:
    preview_fingerprint: str
    as_of: datetime


@dataclass(frozen=True, slots=True)
class RenewalTermsBackfillResult:
    repaired_count: int
    work_item_count: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ChargeInputs:
    """Downstream charge-term inputs the renewal resolver needs."""

    has_active_recurring_price: bool
    effective_cycle: str | None
    price_currency: str | None

    def reasons(self, *, enforcement_currency: str) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.has_active_recurring_price:
            reasons.append("no_active_recurring_price")
        if self.effective_cycle is None:
            reasons.append("cadence_unproven")
        elif self.effective_cycle != "monthly":
            reasons.append(f"cadence_incompatible:{self.effective_cycle}")
        if (
            self.price_currency is not None
            and self.price_currency.upper() != enforcement_currency.upper()
        ):
            reasons.append("charge_currency_mismatch")
        return tuple(reasons)


def _charge_inputs(db: Session, subscription: Subscription) -> ChargeInputs:
    from app.models.catalog import OfferPrice, OfferVersionPrice, PriceType

    row_cycle: str | None = None
    row_currency: str | None = None
    has_row = False
    if subscription.offer_version_id is not None:
        version_price = db.scalars(
            select(OfferVersionPrice).where(
                OfferVersionPrice.offer_version_id == subscription.offer_version_id,
                OfferVersionPrice.price_type == PriceType.recurring,
                OfferVersionPrice.is_active.is_(True),
            )
        ).first()
        if version_price is not None:
            has_row = True
            row_cycle = (
                version_price.billing_cycle.value
                if version_price.billing_cycle
                else None
            )
            row_currency = version_price.currency
    if not has_row:
        offer_price = db.scalars(
            select(OfferPrice).where(
                OfferPrice.offer_id == subscription.offer_id,
                OfferPrice.price_type == PriceType.recurring,
                OfferPrice.is_active.is_(True),
            )
        ).first()
        if offer_price is not None:
            has_row = True
            row_cycle = (
                offer_price.billing_cycle.value if offer_price.billing_cycle else None
            )
            row_currency = offer_price.currency
    subscription_cycle = (
        subscription.billing_cycle.value if subscription.billing_cycle else None
    )
    # Missing subscription cadence is NOT assumed monthly: the effective
    # cadence must be proven by the subscription or its active price row.
    effective_cycle = subscription_cycle or row_cycle
    return ChargeInputs(
        has_active_recurring_price=has_row,
        effective_cycle=effective_cycle,
        price_currency=row_currency,
    )


def _unit_price_missing(subscription: Subscription) -> bool:
    return subscription.unit_price is None or subscription.unit_price <= Decimal("0.00")


def _blocked_subscriptions(
    db: Session, *, enforcement_currency: str, as_of: datetime | None = None
) -> list[tuple[Subscription, ChargeInputs]]:
    # The threshold owner evaluates every COLLECTIBLE status, not just
    # active: a suspended prepaid subscription with unresolved renewal terms
    # still blocks its account (including funded restoration). A subscription
    # is blocked when its frozen contracted amount is missing OR when the
    # downstream charge-term inputs (active recurring price row for
    # currency/cadence metadata, proven monthly cadence) are absent — both
    # yield charge=None in the renewal resolver.
    #
    # The cohort mirrors the threshold owner exactly: a subscription whose
    # customer billing is suppressed by an effective (or drift-protected)
    # billing treatment, or that the chargeability owner confirms is free
    # (one active recurring catalog price of ZERO, no contradictory positive
    # subscription price), is non-billable there and never needs renewal
    # terms — so it is not a finance work item here either, and an existing
    # item resolves on the next capture. A missing price row is review work
    # and stays in the cohort.
    from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
    from app.services.customer_chargeability import confirmed_free_subscription_ids
    from app.services.subscription_billing_treatments import (
        resolve_subscription_billing_treatments,
    )

    rows = list(
        db.scalars(
            select(Subscription).where(
                Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES),
                Subscription.billing_mode == BillingMode.prepaid,
            )
        ).all()
    )
    treatments = resolve_subscription_billing_treatments(db, rows, as_of=as_of)
    confirmed_free_ids = confirmed_free_subscription_ids(db, rows)
    blocked: list[tuple[Subscription, ChargeInputs]] = []
    for sub in sorted(rows, key=lambda item: str(item.id)):
        if treatments[sub.id].suppress_customer_billing or sub.id in confirmed_free_ids:
            continue
        inputs = _charge_inputs(db, sub)
        if _unit_price_missing(sub) or inputs.reasons(
            enforcement_currency=enforcement_currency
        ):
            blocked.append((sub, inputs))
    return blocked


def _paid_base_line_evidence(
    db: Session, subscription: Subscription, *, enforcement_currency: str
) -> tuple[PaidLineEvidence, ...]:
    rows = db.execute(
        select(InvoiceLine, Invoice)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(
            InvoiceLine.subscription_id == subscription.id,
            InvoiceLine.is_active.is_(True),
            Invoice.is_active.is_(True),
            Invoice.status == InvoiceStatus.paid,
        )
        .order_by(Invoice.id, InvoiceLine.id)
    ).all()
    evidence: list[PaidLineEvidence] = []
    for line, invoice in rows:
        metadata = line.metadata_ or {}
        if metadata.get("kind") != "base_subscription":
            continue
        unit_price = Decimal(str(line.unit_price)).quantize(Decimal("0.01"))
        if unit_price <= Decimal("0.00"):
            continue
        quantity = Decimal(str(line.quantity))
        amount = Decimal(str(line.amount)).quantize(Decimal("0.01"))
        proration_marker = next(
            (marker for marker in _PRORATION_MARKERS if metadata.get(marker)),
            None,
        )
        if proration_marker is None and "prorat" in (line.description or "").lower():
            # The repository's own proration path can mark a line only in
            # its description while its period still looks month-shaped.
            proration_marker = "description"
        period_start = invoice.billing_period_start
        period_end = invoice.billing_period_end
        full_cycle = _is_canonical_full_cycle(period_start, period_end)
        incompatibility: str | None = None
        if (invoice.currency or "").upper() != enforcement_currency.upper():
            incompatibility = "currency_mismatch"
        elif proration_marker is not None:
            incompatibility = "prorated"
        elif quantity != Decimal("1"):
            incompatibility = "quantity_not_one"
        elif amount != (unit_price * quantity).quantize(Decimal("0.01")):
            incompatibility = "amount_mismatch"
        evidence.append(
            PaidLineEvidence(
                invoice_id=invoice.id,
                invoice_line_id=line.id,
                unit_price=unit_price,
                quantity=quantity,
                amount=amount,
                currency=(invoice.currency or ""),
                period_start=period_start,
                period_end=period_end,
                proration_marker=proration_marker,
                full_cycle=full_cycle,
                compatible=incompatibility is None,
                incompatibility=incompatibility,
            )
        )
    return tuple(evidence)


def _classify(
    db: Session,
    subscription: Subscription,
    inputs: ChargeInputs,
    *,
    enforcement_currency: str,
) -> RenewalTermsEvidenceItem:
    evidence = _paid_base_line_evidence(
        db, subscription, enforcement_currency=enforcement_currency
    )
    compatible = [e for e in evidence if e.compatible]
    proven = [e for e in compatible if e.full_cycle]
    distinct_all = tuple(sorted({e.unit_price for e in evidence}))
    distinct_compatible = tuple(sorted({e.unit_price for e in compatible}))
    input_reasons = inputs.reasons(enforcement_currency=enforcement_currency)
    reasons: list[str] = []
    contracted: Decimal | None = None

    if input_reasons:
        # Even a proven contracted amount cannot unblock the account while
        # the downstream charge-term inputs are missing; these cases need
        # catalog/cadence work, so they are owned, not repaired.
        decision = RenewalTermsDecision.missing_charge_inputs
        reasons.extend(input_reasons)
    elif not _unit_price_missing(subscription):
        # In the cohort purely for charge inputs (handled above); a priced
        # subscription with intact inputs should not reach here.
        decision = RenewalTermsDecision.missing_charge_inputs
        reasons.append("charge_inputs_recovered")
    elif not evidence:
        decision = RenewalTermsDecision.no_evidence
    elif len(distinct_compatible) > 1:
        decision = RenewalTermsDecision.ambiguous_amounts
        reasons.append("conflicting_compatible_amounts")
    elif not compatible:
        decision = RenewalTermsDecision.insufficient_cycle_evidence
        reasons.extend(
            sorted({e.incompatibility for e in evidence if e.incompatibility})
        )
    elif proven:
        # At least one line proven against the canonical cadence boundary
        # establishes the contracted monthly amount.
        decision = RenewalTermsDecision.repairable
        contracted = distinct_compatible[0]
    else:
        # No line carries explicit canonical full-cycle proof — repetition
        # of unproven lines is not proof (they may all be prorated or
        # partial in the same way).
        decision = RenewalTermsDecision.insufficient_cycle_evidence
        reasons.append("no_canonical_full_cycle_proof")

    return RenewalTermsEvidenceItem(
        subscription_id=subscription.id,
        account_id=subscription.subscriber_id,
        decision=decision,
        contracted_amount=contracted,
        distinct_paid_amounts=distinct_all,
        paid_line_count=len(evidence),
        evidence=evidence,
        insufficiency_reasons=tuple(reasons),
    )


def _fingerprint(items: tuple[RenewalTermsEvidenceItem, ...]) -> str:
    payload = {
        "policy_version": _POLICY_VERSION,
        "items": [
            {
                "subscription_id": str(item.subscription_id),
                "decision": item.decision.value,
                "amount": (
                    str(item.contracted_amount)
                    if item.contracted_amount is not None
                    else None
                ),
                "reasons": list(item.insufficiency_reasons),
                "evidence": [e.as_payload() for e in item.evidence],
            }
            for item in items
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def preview_prepaid_renewal_terms_backfill(
    db: Session, *, now: datetime | None = None
) -> RenewalTermsBackfillPreview:
    """Classify every blocked prepaid subscription against its paid evidence."""
    from app.services.prepaid_currency import resolve_prepaid_enforcement_currency

    as_of = now or datetime.now(UTC)
    currency = resolve_prepaid_enforcement_currency(db)
    items = tuple(
        _classify(db, sub, inputs, enforcement_currency=currency)
        for sub, inputs in _blocked_subscriptions(
            db, enforcement_currency=currency, as_of=as_of
        )
    )
    return RenewalTermsBackfillPreview(
        as_of=as_of, items=items, fingerprint=_fingerprint(items)
    )


class RenewalTermsNextAction(StrEnum):
    """The sanctioned finance next step for one work-item decision."""

    reviewed_record = "reviewed_renewal_term_record"
    charge_inputs = "resolve_charge_inputs"


#: admin_alerts.summary is VARCHAR(255) in production PostgreSQL.
WORK_ITEM_SUMMARIES: dict[RenewalTermsNextAction, str] = {
    RenewalTermsNextAction.reviewed_record: (
        "Prepaid subscription has no contracted amount and its paid evidence "
        "is missing or conflicting. Request and approve a reviewed "
        "renewal-term record with evidence; never infer it from the catalog. "
        "See runbook."
    ),
    RenewalTermsNextAction.charge_inputs: (
        "Prepaid subscription lacks charge inputs (recurring price metadata "
        "or monthly cadence). Decide billable vs complimentary first; a "
        "price alone cannot clear this item. See runbook."
    ),
}


def _next_action(decision: RenewalTermsDecision) -> RenewalTermsNextAction:
    if decision is RenewalTermsDecision.missing_charge_inputs:
        return RenewalTermsNextAction.charge_inputs
    return RenewalTermsNextAction.reviewed_record


def _sync_evidence_work_items(
    db: Session,
    unresolved: tuple[RenewalTermsEvidenceItem, ...],
    *,
    now: datetime,
) -> None:
    from app.services.observability import resolve_findings

    for item in unresolved:
        _record_evidence_work_item(db, item, now=now)
    resolve_findings(
        db,
        managed_prefix=_FINDING_PREFIX,
        active_fingerprints={
            f"{_FINDING_PREFIX}{item.subscription_id}" for item in unresolved
        },
    )


def _record_evidence_work_item(
    db: Session, item: RenewalTermsEvidenceItem, *, now: datetime
) -> None:
    from app.models.network_monitoring import AlertSeverity
    from app.services.observability import Finding, record_finding

    next_action = _next_action(item.decision)
    record_finding(
        db,
        Finding(
            fingerprint=f"{_FINDING_PREFIX}{item.subscription_id}",
            domain="prepaid_enforcement",
            source="prepaid_renewal_terms_backfill",
            severity=AlertSeverity.warning,
            title="Prepaid renewal terms need finance review",
            summary=WORK_ITEM_SUMMARIES[next_action],
            details={
                "owner": RENEWAL_TERMS_WORK_ITEM_OWNER,
                "runbook": RENEWAL_TERMS_RUNBOOK,
                "next_action": next_action.value,
                "account_id": str(item.account_id),
                "subscription_id": str(item.subscription_id),
                "decision": item.decision.value,
                "insufficiency_reasons": list(item.insufficiency_reasons),
                "distinct_paid_amounts": [str(a) for a in item.distinct_paid_amounts],
                "sla_due_at": (now + timedelta(hours=_EVIDENCE_SLA_HOURS)).isoformat(),
            },
        ),
    )


def capture_prepaid_renewal_terms_backfill(
    db: Session,
    command: CaptureRenewalTermsBackfillCommand,
    *,
    context: CommandContext,
) -> RenewalTermsBackfillResult:
    """Apply the fingerprint-bound backfill through the owner boundary."""
    return execute_owner_command(
        db,
        definition=_CAPTURE_COMMAND,
        context=context,
        operation=lambda: _capture(db, command=command, context=context),
    )


def _capture(
    db: Session,
    *,
    command: CaptureRenewalTermsBackfillCommand,
    context: CommandContext,
) -> RenewalTermsBackfillResult:
    if not context.idempotency_key:
        raise _error(
            "missing_idempotency_key",
            "Capturing a renewal-terms backfill requires a business idempotency key.",
        )
    preview = preview_prepaid_renewal_terms_backfill(db, now=command.as_of)
    if preview.fingerprint != command.preview_fingerprint:
        raise _error(
            "stale_preview",
            "Evidence changed since the reviewed preview; re-run the preview "
            "and review the new fingerprint.",
        )
    repaired = 0
    unresolved: list[RenewalTermsEvidenceItem] = []
    for item in preview.items:
        if item.decision is RenewalTermsDecision.repairable:
            subscription = db.execute(
                select(Subscription)
                .where(Subscription.id == item.subscription_id)
                .with_for_update()
            ).scalar_one_or_none()
            if subscription is None:
                continue
            if (
                subscription.unit_price is not None
                and subscription.unit_price > Decimal("0.00")
            ):
                continue
            subscription.unit_price = item.contracted_amount
            repaired += 1
            from app.services.events import EventType, emit_event

            emit_event(
                db,
                EventType.prepaid_renewal_terms_backfilled,
                {
                    "schema_version": 1,
                    "account_id": str(item.account_id),
                    "subscription_id": str(item.subscription_id),
                    "contracted_amount": str(item.contracted_amount),
                    "paid_line_count": item.paid_line_count,
                    "preview_fingerprint": preview.fingerprint,
                },
                subscriber_id=item.account_id,
                account_id=item.account_id,
                subscription_id=item.subscription_id,
            )
            logger.info(
                "prepaid_renewal_terms_backfilled: subscription=%s amount=%s "
                "paid_lines=%d",
                item.subscription_id,
                item.contracted_amount,
                item.paid_line_count,
            )
        else:
            unresolved.append(item)
    _sync_evidence_work_items(db, tuple(unresolved), now=preview.as_of)
    return RenewalTermsBackfillResult(
        repaired_count=repaired,
        work_item_count=len(unresolved),
        fingerprint=preview.fingerprint,
    )


_CORRECT_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="prepaid renewal-terms evidence backfill",
    name="correct_prepaid_renewal_terms",
)


class RenewalTermsCorrectionAction(StrEnum):
    apply_reviewed_term = "apply_reviewed_term"
    restore_fail_closed = "restore_fail_closed"


class RenewalTermsCorrectionSource(StrEnum):
    audit = "audit"
    finance_review = "finance_review"


@dataclass(frozen=True, slots=True)
class CorrectRenewalTermsCommand:
    """Bound supersession of a previously backfilled amount.

    The target must belong to the prior backfill cohort; the caller must
    state the amount it believes is current (optimistic lock); provenance is
    typed — an audit-sourced correction is bound to a durable audit
    fingerprint and may only restore the fail-closed state, while a
    finance-review correction carries the review reference.
    """

    subscription_id: UUID
    action: RenewalTermsCorrectionAction
    source: RenewalTermsCorrectionSource
    expected_current_amount: Decimal | None
    audit_fingerprint: str | None = None
    review_reference: str | None = None
    reviewed_amount: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RenewalTermsCorrectionResult:
    subscription_id: UUID
    action: RenewalTermsCorrectionAction
    previous_amount: Decimal | None
    new_amount: Decimal | None
    replayed: bool


def _backfilled_subscription_ids(db: Session) -> set[UUID]:
    from app.models.event_store import EventStore
    from app.services.events import EventType

    ids: set[UUID] = set()
    for event in db.execute(
        select(EventStore).where(
            EventStore.event_type == EventType.prepaid_renewal_terms_backfilled.value
        )
    ).scalars():
        raw = (event.payload or {}).get("subscription_id")
        if raw:
            ids.add(UUID(str(raw)))
    return ids


def _latest_audit(db: Session) -> dict | None:
    from app.models.event_store import EventStore
    from app.services.events import EventType

    event = db.execute(
        select(EventStore)
        .where(EventStore.event_type == EventType.prepaid_renewal_terms_audited.value)
        .order_by(EventStore.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return event.payload if event is not None else None


def correct_prepaid_renewal_terms(
    db: Session,
    command: CorrectRenewalTermsCommand,
    *,
    context: CommandContext,
) -> RenewalTermsCorrectionResult:
    """Apply a finance-reviewed term or restore the fail-closed state.

    The only sanctioned way to supersede a backfilled amount — never direct
    SQL. ``restore_fail_closed`` re-opens the finance work item so the
    account cannot be silently parked without an owner.
    """
    return execute_owner_command(
        db,
        definition=_CORRECT_COMMAND,
        context=context,
        operation=lambda: _correct(db, command=command),
    )


def _correct(
    db: Session, *, command: CorrectRenewalTermsCommand
) -> RenewalTermsCorrectionResult:
    from app.services.events import EventType, emit_event

    if command.subscription_id not in _backfilled_subscription_ids(db):
        raise _error(
            "not_in_backfill_cohort",
            "Corrections are restricted to subscriptions previously "
            "restored by this owner.",
        )
    if command.source is RenewalTermsCorrectionSource.finance_review:
        if not (command.review_reference or "").strip():
            raise _error(
                "missing_review_reference",
                "A finance-review correction requires the review reference.",
            )
    else:
        if command.action is not RenewalTermsCorrectionAction.restore_fail_closed:
            raise _error(
                "invalid_audit_action",
                "An audit-sourced correction can only restore the "
                "fail-closed state; it never invents an amount.",
            )
        if not (command.audit_fingerprint or "").strip():
            raise _error(
                "missing_audit_fingerprint",
                "An audit-sourced correction requires the durable audit fingerprint.",
            )
        latest = _latest_audit(db)
        if latest is None or latest.get("audit_fingerprint") != (
            command.audit_fingerprint
        ):
            raise _error(
                "audit_mismatch",
                "The supplied audit fingerprint does not match the latest "
                "durable audit run.",
            )
        verdicts = {
            str(item.get("subscription_id")): item for item in latest.get("items", [])
        }
        verdict = verdicts.get(str(command.subscription_id))
        if verdict is None or verdict.get("amount_confirmed"):
            raise _error(
                "audit_mismatch",
                "The audited verdict for this subscription does not "
                "authorize a fail-closed restoration.",
            )
    subscription = db.execute(
        select(Subscription)
        .where(Subscription.id == command.subscription_id)
        .with_for_update()
    ).scalar_one_or_none()
    if subscription is None:
        raise _error("subscription_not_found", "Subscription was not found.")
    previous = (
        Decimal(str(subscription.unit_price))
        if subscription.unit_price is not None
        else None
    )
    if previous != command.expected_current_amount:
        if (
            command.action is RenewalTermsCorrectionAction.apply_reviewed_term
            and previous is not None
            and command.reviewed_amount is not None
            and previous == command.reviewed_amount.quantize(Decimal("0.01"))
        ):
            return RenewalTermsCorrectionResult(
                subscription_id=subscription.id,
                action=command.action,
                previous_amount=previous,
                new_amount=previous,
                replayed=True,
            )
        if (
            command.action is RenewalTermsCorrectionAction.restore_fail_closed
            and previous is None
        ):
            return RenewalTermsCorrectionResult(
                subscription_id=subscription.id,
                action=command.action,
                previous_amount=None,
                new_amount=None,
                replayed=True,
            )
        raise _error(
            "stale_current_amount",
            "The subscription's current amount changed since the correction "
            "was reviewed; re-audit before correcting.",
        )
    provenance = (
        (command.review_reference or "")
        if command.source is RenewalTermsCorrectionSource.finance_review
        else f"audit:{command.audit_fingerprint}"
    )
    if command.action is RenewalTermsCorrectionAction.apply_reviewed_term:
        if command.reviewed_amount is None or command.reviewed_amount <= Decimal(
            "0.00"
        ):
            raise _error(
                "invalid_reviewed_amount",
                "apply_reviewed_term requires a positive reviewed amount.",
            )
        new_amount: Decimal | None = command.reviewed_amount.quantize(Decimal("0.01"))
        subscription.unit_price = new_amount
        from app.services.observability import resolve_findings

        resolve_findings(
            db,
            managed_prefix=f"{_FINDING_PREFIX}{subscription.id}",
            active_fingerprints=set(),
        )
    else:
        new_amount = None
        subscription.unit_price = None
        _sync_correction_work_item(db, subscription, provenance=provenance)
    emit_event(
        db,
        EventType.prepaid_renewal_terms_corrected,
        {
            "schema_version": 2,
            "subscription_id": str(subscription.id),
            "account_id": str(subscription.subscriber_id),
            "action": command.action.value,
            "source": command.source.value,
            "previous_amount": str(previous) if previous is not None else None,
            "new_amount": str(new_amount) if new_amount is not None else None,
            "provenance": provenance,
        },
        subscriber_id=subscription.subscriber_id,
        account_id=subscription.subscriber_id,
        subscription_id=subscription.id,
    )
    logger.info(
        "prepaid_renewal_terms_corrected: subscription=%s action=%s source=%s "
        "previous=%s new=%s provenance=%s",
        subscription.id,
        command.action.value,
        command.source.value,
        previous,
        new_amount,
        provenance,
    )
    return RenewalTermsCorrectionResult(
        subscription_id=subscription.id,
        action=command.action,
        previous_amount=previous,
        new_amount=new_amount,
        replayed=False,
    )


def _sync_correction_work_item(
    db: Session, subscription: Subscription, *, provenance: str
) -> None:
    from app.models.network_monitoring import AlertSeverity
    from app.services.observability import Finding, record_finding

    record_finding(
        db,
        Finding(
            fingerprint=f"{_FINDING_PREFIX}{subscription.id}",
            domain="prepaid_enforcement",
            source="prepaid_renewal_terms_backfill",
            severity=AlertSeverity.warning,
            title="Prepaid renewal terms need finance review",
            summary=(
                "A previously restored contracted amount was reverted to the "
                "fail-closed state after finance review. Record the correct "
                "price via a reviewed renewal-term record. See runbook."
            ),
            details={
                "owner": RENEWAL_TERMS_WORK_ITEM_OWNER,
                "runbook": RENEWAL_TERMS_RUNBOOK,
                "next_action": RenewalTermsNextAction.reviewed_record.value,
                "account_id": str(subscription.subscriber_id),
                "subscription_id": str(subscription.id),
                "decision": "correction_fail_closed",
                "provenance": provenance,
                "sla_due_at": (
                    datetime.now(UTC) + timedelta(hours=_EVIDENCE_SLA_HOURS)
                ).isoformat(),
            },
        ),
    )


@dataclass(frozen=True, slots=True)
class RenewalTermsAuditItem:
    """v2 re-audit of one previously restored subscription."""

    subscription_id: UUID
    account_id: UUID
    current_unit_price: Decimal | None
    v2_decision: RenewalTermsDecision
    v2_amount: Decimal | None
    amount_confirmed: bool
    insufficiency_reasons: tuple[str, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "subscription_id": str(self.subscription_id),
            "account_id": str(self.account_id),
            "current_unit_price": (
                str(self.current_unit_price)
                if self.current_unit_price is not None
                else None
            ),
            "v2_decision": self.v2_decision.value,
            "v2_amount": str(self.v2_amount) if self.v2_amount is not None else None,
            "amount_confirmed": self.amount_confirmed,
            "insufficiency_reasons": list(self.insufficiency_reasons),
        }


@dataclass(frozen=True, slots=True)
class RenewalTermsAuditRun:
    as_of: datetime
    items: tuple[RenewalTermsAuditItem, ...]
    audit_fingerprint: str


_AUDIT_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="prepaid renewal-terms evidence backfill",
    name="audit_restored_prepaid_renewal_terms",
)


def audit_restored_renewal_terms(
    db: Session,
    *,
    context: CommandContext,
    now: datetime | None = None,
) -> RenewalTermsAuditRun:
    """Reclassify every backfilled subscription under the v2 proof contract.

    Read-only for subscriptions, durable for the audit itself: the run emits
    ``prepaid_renewal_terms.audited`` carrying the ordered verdicts and the
    audit fingerprint. Audit-sourced fail-closed corrections must present
    that fingerprint, so a stale audit can never erase a later correction.
    """
    return execute_owner_command(
        db,
        definition=_AUDIT_COMMAND,
        context=context,
        operation=lambda: _audit(db, now=now),
    )


def _audit(db: Session, *, now: datetime | None) -> RenewalTermsAuditRun:
    from app.services.events import EventType, emit_event
    from app.services.prepaid_currency import resolve_prepaid_enforcement_currency

    as_of = now or datetime.now(UTC)
    currency = resolve_prepaid_enforcement_currency(db)
    items: list[RenewalTermsAuditItem] = []
    for subscription_id in sorted(_backfilled_subscription_ids(db), key=str):
        subscription = db.get(Subscription, subscription_id)
        if subscription is None:
            continue
        inputs = _charge_inputs(db, subscription)
        verdict = _classify(db, subscription, inputs, enforcement_currency=currency)
        current = (
            Decimal(str(subscription.unit_price))
            if subscription.unit_price is not None
            else None
        )
        confirmed = (
            verdict.decision is RenewalTermsDecision.repairable
            and verdict.contracted_amount == current
        ) or (
            # A priced subscription with intact charge inputs whose evidence
            # still proves exactly its current amount.
            current is not None
            and not inputs.reasons(enforcement_currency=currency)
            and _confirms_current_amount(verdict, current)
        )
        items.append(
            RenewalTermsAuditItem(
                subscription_id=subscription.id,
                account_id=subscription.subscriber_id,
                current_unit_price=current,
                v2_decision=verdict.decision,
                v2_amount=verdict.contracted_amount,
                amount_confirmed=confirmed,
                insufficiency_reasons=verdict.insufficiency_reasons,
            )
        )
    ordered = tuple(sorted(items, key=lambda i: str(i.subscription_id)))
    payload_items = [item.as_payload() for item in ordered]
    audit_fingerprint = hashlib.sha256(
        json.dumps(
            {"policy_version": _POLICY_VERSION, "items": payload_items},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    emit_event(
        db,
        EventType.prepaid_renewal_terms_audited,
        {
            "schema_version": 1,
            "as_of": as_of.isoformat(),
            "audit_fingerprint": audit_fingerprint,
            "items": payload_items,
        },
    )
    return RenewalTermsAuditRun(
        as_of=as_of, items=ordered, audit_fingerprint=audit_fingerprint
    )


def _confirms_current_amount(
    verdict: RenewalTermsEvidenceItem, current: Decimal
) -> bool:
    proven = [e for e in verdict.evidence if e.compatible and e.full_cycle]
    return bool(proven) and all(e.unit_price == current for e in proven)


# ---------------------------------------------------------------------------
# Finance-reviewed renewal-term record (four-eyes, two-step).
#
# The backfill above restores an amount only from exact paid evidence and the
# correction command only supersedes amounts this owner already restored. A
# subscription that was never restored — no paid evidence, contradictory paid
# amounts, or no canonical full-cycle proof — had no sanctioned writer: the
# generic admin subscription form was the only path, with no evidence,
# reason, or approval contract. This is that writer.
#
# ``request`` records a proposal (subscription, positive amount, expected
# current value, reason, evidence reference + SHA-256, requesting staff
# member) as durable record-only evidence and changes nothing. ``approve`` by
# a DIFFERENT staff member re-validates every precondition under the
# subscription lock, writes ``Subscription.unit_price``, emits
# ``prepaid_renewal_terms.recorded``, and resolves the finance work item in
# the same transaction. Catalog prices are never read for the amount and
# never written.
# ---------------------------------------------------------------------------

_RECORD_CONCERN = "finance-reviewed prepaid renewal-term record"
_REQUEST_RECORD_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=_RECORD_CONCERN,
    name="request_reviewed_renewal_term_record",
)
_APPROVE_RECORD_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=_RECORD_CONCERN,
    name="approve_reviewed_renewal_term_record",
)
_RECORD_SCHEMA_VERSION = 1
#: Stable namespace for deterministic request/approval event identities.
_RECORD_NAMESPACE = UUID("0f6b7a2e-3c1d-4f5e-9a8b-6c7d8e9f0a1b")
#: Decisions whose amount cannot be restored from evidence and therefore
#: needs a finance-reviewed record. ``missing_charge_inputs`` is deliberately
#: absent: a price alone cannot unblock it, and on an offer that has no
#: recurring price at all it would turn unpriced service into billed service.
#: ``repairable`` is absent because the backfill restores it from evidence.
RECORDABLE_DECISIONS: frozenset[RenewalTermsDecision] = frozenset(
    {
        RenewalTermsDecision.no_evidence,
        RenewalTermsDecision.ambiguous_amounts,
        RenewalTermsDecision.insufficient_cycle_evidence,
    }
)
_MAX_REASON_LENGTH = 500
_MAX_EVIDENCE_REFERENCE_LENGTH = 200
_HEX = frozenset("0123456789abcdef")


class RenewalTermRecordStatus(StrEnum):
    requested = "requested"
    recorded = "recorded"


@dataclass(frozen=True, slots=True)
class RequestRenewalTermRecordCommand:
    """Finance proposal for one never-restored subscription's contracted amount.

    ``requested_by`` is the real staff principal (``SystemUser.id``) whose
    granted role authorized ``permission_granted``; ``context.actor`` stays a
    free-text audit label. ``expected_current_amount`` is the optimistic
    check: ``None`` when the stored price is NULL, else the stored value.
    """

    subscription_id: UUID
    reviewed_amount: Decimal
    expected_current_amount: Decimal | None
    reason: str
    evidence_reference: str
    evidence_sha256: str
    requested_by: UUID
    permission_granted: bool


@dataclass(frozen=True, slots=True)
class ApproveRenewalTermRecordCommand:
    """Second-person approval of one recorded proposal.

    The approver restates the amount being approved, so an approval can never
    be given without seeing what it applies.
    """

    request_id: UUID
    approved_amount: Decimal
    approved_by: UUID
    permission_granted: bool


@dataclass(frozen=True, slots=True)
class RenewalTermRecordRequest:
    """Durable read model of one proposal and its decision, if any."""

    request_id: UUID
    subscription_id: UUID
    account_id: UUID
    decision: RenewalTermsDecision
    reviewed_amount: Decimal
    expected_current_amount: Decimal | None
    reason: str
    evidence_reference: str
    evidence_sha256: str
    requested_by: UUID
    requested_at: datetime
    status: RenewalTermRecordStatus
    approved_by: UUID | None = None


@dataclass(frozen=True, slots=True)
class RenewalTermRecordResult:
    request_id: UUID
    subscription_id: UUID
    status: RenewalTermRecordStatus
    previous_amount: Decimal | None
    new_amount: Decimal | None
    work_item_resolved: bool
    remaining_reasons: tuple[str, ...]
    replayed: bool


def _request_event_id(idempotency_key: str) -> UUID:
    return uuid5(_RECORD_NAMESPACE, f"{OWNER}:record-request:{idempotency_key}")


def _approval_event_id(request_id: UUID) -> UUID:
    return uuid5(_RECORD_NAMESPACE, f"{OWNER}:record-approval:{request_id}")


def _money(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if not value.is_finite():
        raise _error("invalid_reviewed_amount", "Amounts must be finite decimals.")
    return value.quantize(Decimal("0.01"))


def _stored_money(raw: object) -> Decimal | None:
    return None if raw is None else Decimal(str(raw))


def _require_staff(
    db: Session,
    *,
    context: CommandContext,
    system_user_id: UUID,
    permission_granted: bool,
) -> None:
    from app.models.system_user import SystemUser

    if context.scope != RENEWAL_TERM_RECORD_PERMISSION or not permission_granted:
        raise _error(
            "permission_denied",
            f"The {RENEWAL_TERM_RECORD_PERMISSION} permission is required.",
        )
    user = db.get(SystemUser, system_user_id)
    if user is None or not user.is_active:
        raise _error(
            "invalid_actor",
            "The staff member must be an existing, active system user.",
        )


def _validate_request(command: RequestRenewalTermRecordCommand) -> Decimal:
    amount = _money(command.reviewed_amount)
    if amount is None or amount <= Decimal("0.00"):
        # Zero is a claim, not an absent price: genuinely complimentary
        # service goes through the billing-treatment owner instead.
        raise _error(
            "invalid_reviewed_amount",
            "A reviewed renewal-term record requires a positive amount; "
            "complimentary service uses a billing treatment.",
        )
    reason = command.reason.strip()
    if not reason or len(reason) > _MAX_REASON_LENGTH:
        raise _error(
            "invalid_review_reason",
            f"A reason of 1-{_MAX_REASON_LENGTH} characters is required.",
        )
    reference = command.evidence_reference.strip()
    digest = command.evidence_sha256.strip().lower()
    if (
        not reference
        or len(reference) > _MAX_EVIDENCE_REFERENCE_LENGTH
        or len(digest) != 64
        or not set(digest) <= _HEX
    ):
        raise _error(
            "invalid_evidence",
            "An evidence reference and the evidence's 64-hex SHA-256 are required.",
        )
    return amount


def _locked_subscription(db: Session, subscription_id: UUID) -> Subscription:
    subscription = db.execute(
        select(Subscription).where(Subscription.id == subscription_id).with_for_update()
    ).scalar_one_or_none()
    if subscription is None:
        raise _error("subscription_not_found", "Subscription was not found.")
    return subscription


def _current_amount(subscription: Subscription) -> Decimal | None:
    return (
        Decimal(str(subscription.unit_price))
        if subscription.unit_price is not None
        else None
    )


def _recordable_item(
    db: Session, subscription: Subscription, *, now: datetime
) -> RenewalTermsEvidenceItem:
    """Re-derive cohort membership and the live decision; fail closed."""
    from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
    from app.services.customer_chargeability import confirmed_free_subscription_ids
    from app.services.prepaid_currency import resolve_prepaid_enforcement_currency
    from app.services.subscription_billing_treatments import (
        subscription_has_open_billing_treatment,
    )

    if subscription_has_open_billing_treatment(db, subscription.id, as_of=now):
        raise _error(
            "billing_treatment_open",
            "The subscription has an open complimentary or sponsored billing "
            "treatment; its price cannot change while the treatment is open.",
        )
    if (
        subscription.billing_mode != BillingMode.prepaid
        or subscription.status not in COLLECTIBLE_SERVICE_STATUSES
        or not _unit_price_missing(subscription)
    ):
        raise _error(
            "not_in_record_cohort",
            "Only collectible prepaid subscriptions without a contracted "
            "amount can receive a reviewed renewal-term record.",
        )
    if subscription.id in confirmed_free_subscription_ids(db, [subscription]):
        raise _error(
            "not_in_record_cohort",
            "The catalog declares this service free (explicit zero recurring "
            "price); a positive record would contradict it. Change the plan "
            "through the catalog/plan-change owner if it should be billed.",
        )
    currency = resolve_prepaid_enforcement_currency(db)
    item = _classify(
        db,
        subscription,
        _charge_inputs(db, subscription),
        enforcement_currency=currency,
    )
    if item.decision is RenewalTermsDecision.missing_charge_inputs:
        raise _error(
            "charge_inputs_missing",
            "The subscription lacks charge inputs ("
            + ", ".join(item.insufficiency_reasons)
            + "); decide billable vs complimentary and fix the price metadata "
            "first. A price alone cannot clear this.",
        )
    if item.decision not in RECORDABLE_DECISIONS:
        raise _error(
            "evidence_repairable",
            "Paid evidence proves the contracted amount; the scheduled "
            "backfill restores it. A reviewed record must not override it.",
        )
    return item


def _event_payload(db: Session, *, event_id: UUID, event_type: str) -> dict | None:
    from app.models.event_store import EventStore

    event = db.execute(
        select(EventStore).where(
            EventStore.event_id == event_id,
            EventStore.event_type == event_type,
        )
    ).scalar_one_or_none()
    return dict(event.payload or {}) if event is not None else None


def _load_request(db: Session, request_id: UUID) -> RenewalTermRecordRequest | None:
    from app.services.events import EventType

    payload = _event_payload(
        db,
        event_id=request_id,
        event_type=EventType.prepaid_renewal_terms_record_requested.value,
    )
    if payload is None:
        return None
    return _request_from_payload(payload, approval=_load_approval(db, request_id))


def _load_approval(db: Session, request_id: UUID) -> dict | None:
    from app.services.events import EventType

    return _event_payload(
        db,
        event_id=_approval_event_id(request_id),
        event_type=EventType.prepaid_renewal_terms_recorded.value,
    )


def _request_from_payload(
    payload: dict, *, approval: dict | None
) -> RenewalTermRecordRequest:
    reviewed = Decimal(str(payload["reviewed_amount"]))
    return RenewalTermRecordRequest(
        request_id=UUID(str(payload["request_id"])),
        subscription_id=UUID(str(payload["subscription_id"])),
        account_id=UUID(str(payload["account_id"])),
        decision=RenewalTermsDecision(str(payload["decision"])),
        reviewed_amount=reviewed,
        expected_current_amount=_stored_money(payload.get("expected_current_amount")),
        reason=str(payload["reason"]),
        evidence_reference=str(payload["evidence_reference"]),
        evidence_sha256=str(payload["evidence_sha256"]),
        requested_by=UUID(str(payload["requested_by_system_user_id"])),
        requested_at=datetime.fromisoformat(str(payload["requested_at"])),
        status=(
            RenewalTermRecordStatus.recorded
            if approval is not None
            else RenewalTermRecordStatus.requested
        ),
        approved_by=(
            UUID(str(approval["approved_by_system_user_id"]))
            if approval is not None
            else None
        ),
    )


def list_renewal_term_record_requests(
    db: Session,
    *,
    subscription_id: UUID | None = None,
    include_recorded: bool = False,
) -> tuple[RenewalTermRecordRequest, ...]:
    """Read-only: proposals (pending only by default), oldest first."""
    from app.models.event_store import EventStore
    from app.services.events import EventType

    query = select(EventStore).where(
        EventStore.event_type == EventType.prepaid_renewal_terms_record_requested.value
    )
    if subscription_id is not None:
        query = query.where(EventStore.subscription_id == subscription_id)
    rows: list[RenewalTermRecordRequest] = []
    for event in db.execute(query.order_by(EventStore.created_at)).scalars():
        payload = dict(event.payload or {})
        request = _request_from_payload(
            payload,
            approval=_load_approval(db, UUID(str(payload["request_id"]))),
        )
        if include_recorded or request.status is RenewalTermRecordStatus.requested:
            rows.append(request)
    return tuple(rows)


def request_reviewed_renewal_term_record(
    db: Session,
    command: RequestRenewalTermRecordCommand,
    *,
    context: CommandContext,
) -> RenewalTermRecordResult:
    """Record a finance proposal; changes no price (step 1 of 2)."""
    return execute_owner_command(
        db,
        definition=_REQUEST_RECORD_COMMAND,
        context=context,
        operation=lambda: _request_record(db, command=command, context=context),
    )


def _request_record(
    db: Session,
    *,
    command: RequestRenewalTermRecordCommand,
    context: CommandContext,
) -> RenewalTermRecordResult:
    from app.services.events import EventType, emit_event

    if not context.idempotency_key:
        raise _error(
            "missing_idempotency_key",
            "A renewal-term record request requires a business idempotency key.",
        )
    _require_staff(
        db,
        context=context,
        system_user_id=command.requested_by,
        permission_granted=command.permission_granted,
    )
    amount = _validate_request(command)
    expected = _money(command.expected_current_amount)
    digest = command.evidence_sha256.strip().lower()
    request_id = _request_event_id(context.idempotency_key)

    existing = _load_request(db, request_id)
    if existing is not None:
        if (
            existing.subscription_id != command.subscription_id
            or existing.reviewed_amount != amount
            or existing.expected_current_amount != expected
            or existing.evidence_sha256 != digest
            or existing.requested_by != command.requested_by
        ):
            raise _error(
                "idempotency_conflict",
                "This idempotency key already recorded a different proposal.",
            )
        return RenewalTermRecordResult(
            request_id=request_id,
            subscription_id=existing.subscription_id,
            status=existing.status,
            previous_amount=existing.expected_current_amount,
            new_amount=(
                existing.reviewed_amount
                if existing.status is RenewalTermRecordStatus.recorded
                else None
            ),
            work_item_resolved=False,
            remaining_reasons=(),
            replayed=True,
        )

    subscription = _locked_subscription(db, command.subscription_id)
    now = datetime.now(UTC)
    item = _recordable_item(db, subscription, now=now)
    current = _current_amount(subscription)
    if current != expected:
        raise _error(
            "stale_current_amount",
            "The subscription's current amount differs from the expected value; "
            "re-read it before requesting a record.",
        )
    emit_event(
        db,
        EventType.prepaid_renewal_terms_record_requested,
        {
            "schema_version": _RECORD_SCHEMA_VERSION,
            "request_id": str(request_id),
            "subscription_id": str(subscription.id),
            "account_id": str(subscription.subscriber_id),
            "decision": item.decision.value,
            "insufficiency_reasons": list(item.insufficiency_reasons),
            "distinct_paid_amounts": [str(a) for a in item.distinct_paid_amounts],
            "reviewed_amount": str(amount),
            "expected_current_amount": (
                str(expected) if expected is not None else None
            ),
            "reason": command.reason.strip(),
            "evidence_reference": command.evidence_reference.strip(),
            "evidence_sha256": digest,
            "requested_by_system_user_id": str(command.requested_by),
            "requested_at": now.isoformat(),
            "actor": context.actor,
            "command_id": str(context.command_id),
            "idempotency_key": context.idempotency_key,
        },
        event_id=request_id,
        actor=context.actor,
        subscriber_id=subscription.subscriber_id,
        account_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        record_only=True,
    )
    logger.info(
        "prepaid_renewal_term_record_requested: request=%s subscription=%s decision=%s",
        request_id,
        subscription.id,
        item.decision.value,
    )
    return RenewalTermRecordResult(
        request_id=request_id,
        subscription_id=subscription.id,
        status=RenewalTermRecordStatus.requested,
        previous_amount=current,
        new_amount=None,
        work_item_resolved=False,
        remaining_reasons=item.insufficiency_reasons,
        replayed=False,
    )


def approve_reviewed_renewal_term_record(
    db: Session,
    command: ApproveRenewalTermRecordCommand,
    *,
    context: CommandContext,
) -> RenewalTermRecordResult:
    """A second staff member approves and applies a proposal (step 2 of 2)."""
    return execute_owner_command(
        db,
        definition=_APPROVE_RECORD_COMMAND,
        context=context,
        operation=lambda: _approve_record(db, command=command, context=context),
    )


def _approve_record(
    db: Session,
    *,
    command: ApproveRenewalTermRecordCommand,
    context: CommandContext,
) -> RenewalTermRecordResult:
    from app.services.events import EventType, emit_event
    from app.services.observability import resolve_findings
    from app.services.prepaid_currency import resolve_prepaid_enforcement_currency

    if not context.idempotency_key:
        raise _error(
            "missing_idempotency_key",
            "A renewal-term record approval requires a business idempotency key.",
        )
    _require_staff(
        db,
        context=context,
        system_user_id=command.approved_by,
        permission_granted=command.permission_granted,
    )
    request = _load_request(db, command.request_id)
    if request is None:
        raise _error(
            "request_not_found", "The renewal-term record request was not found."
        )
    if command.approved_by == request.requested_by:
        raise _error(
            "self_approval_forbidden",
            "The approver must be a different staff member from the requester.",
        )
    if _money(command.approved_amount) != request.reviewed_amount:
        raise _error(
            "approval_amount_mismatch",
            "The approved amount does not match the requested amount.",
        )
    if request.status is RenewalTermRecordStatus.recorded:
        if request.approved_by == command.approved_by:
            return RenewalTermRecordResult(
                request_id=request.request_id,
                subscription_id=request.subscription_id,
                status=RenewalTermRecordStatus.recorded,
                previous_amount=request.expected_current_amount,
                new_amount=request.reviewed_amount,
                work_item_resolved=False,
                remaining_reasons=(),
                replayed=True,
            )
        raise _error(
            "request_already_decided",
            "This renewal-term record request was already approved.",
        )

    subscription = _locked_subscription(db, request.subscription_id)
    now = datetime.now(UTC)
    item = _recordable_item(db, subscription, now=now)
    previous = _current_amount(subscription)
    if previous != request.expected_current_amount:
        raise _error(
            "stale_current_amount",
            "The subscription's amount changed since the request; submit a new "
            "request against the current value.",
        )
    subscription.unit_price = request.reviewed_amount
    db.flush()

    # Re-derive the work item from the post-write state in this transaction.
    # Charge inputs were intact for every recordable decision, so the item
    # closes; if they were not, it stays open with its new reasons rather
    # than being silently resolved by a price alone.
    currency = resolve_prepaid_enforcement_currency(db)
    inputs = _charge_inputs(db, subscription)
    remaining = inputs.reasons(enforcement_currency=currency)
    if remaining:
        _record_evidence_work_item(
            db,
            _classify(db, subscription, inputs, enforcement_currency=currency),
            now=now,
        )
        work_item_resolved = False
    else:
        resolve_findings(
            db,
            managed_prefix=f"{_FINDING_PREFIX}{subscription.id}",
            active_fingerprints=set(),
        )
        work_item_resolved = True

    emit_event(
        db,
        EventType.prepaid_renewal_terms_recorded,
        {
            "schema_version": _RECORD_SCHEMA_VERSION,
            "request_id": str(request.request_id),
            "subscription_id": str(subscription.id),
            "account_id": str(subscription.subscriber_id),
            "decision_at_request": request.decision.value,
            "decision_at_approval": item.decision.value,
            "previous_amount": str(previous) if previous is not None else None,
            "new_amount": str(request.reviewed_amount),
            "reason": request.reason,
            "evidence_reference": request.evidence_reference,
            "evidence_sha256": request.evidence_sha256,
            "requested_by_system_user_id": str(request.requested_by),
            "approved_by_system_user_id": str(command.approved_by),
            "approved_at": now.isoformat(),
            "actor": context.actor,
            "command_id": str(context.command_id),
            "idempotency_key": context.idempotency_key,
            "work_item_resolved": work_item_resolved,
            "remaining_reasons": list(remaining),
        },
        event_id=_approval_event_id(request.request_id),
        actor=context.actor,
        subscriber_id=subscription.subscriber_id,
        account_id=subscription.subscriber_id,
        subscription_id=subscription.id,
    )
    logger.info(
        "prepaid_renewal_term_recorded: request=%s subscription=%s "
        "work_item_resolved=%s",
        request.request_id,
        subscription.id,
        work_item_resolved,
    )
    return RenewalTermRecordResult(
        request_id=request.request_id,
        subscription_id=subscription.id,
        status=RenewalTermRecordStatus.recorded,
        previous_amount=previous,
        new_amount=request.reviewed_amount,
        work_item_resolved=work_item_resolved,
        remaining_reasons=remaining,
        replayed=False,
    )
