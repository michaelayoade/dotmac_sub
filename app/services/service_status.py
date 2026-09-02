"""Compute a customer's truthful service status.

Service expiry here is NOT date-driven (see unified billing enforcement in
``collections/_core.py:BillingEnforcementReconciler``):

* Prepaid monthly service is invoiced in advance; dunning policy drives cases
  and customer notices, while actual enforcing actions are gated by available
  ledger balance.
* Postpaid never lapses on a date; only dunning on overdue invoices suspends it.

This module mirrors those enforcement rules read-only so the customer app can
show the real "when does my service stop" date instead of guessing from a
billing date.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, selectinload

from app.models.billing import (
    Invoice,
    InvoiceStatus,
)
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import Subscriber
from app.schemas.service_status import (
    ServiceStatusAction,
    ServiceStatusActionKind,
    ServiceStatusItem,
    ServiceStatusResponse,
)
from app.services.access_resolution import (
    resolve_customer_access,
    resolve_prepaid_funding,
)
from app.services.collections import has_overdue_balance
from app.services.collections.grace_policy import resolve_grace_decision
from app.services.common import coerce_uuid
from app.services.invoice_collectibility import collection_due_date_eligible_filter
from app.services.status_presentation import subscription_status_presentation
from app.services.subscription_lifecycle_policy import (
    PORTAL_VISIBLE_SERVICE_STATUSES,
    OperationalSubscriptionCohortInput,
    OperationalSubscriptionCohortReason,
    classify_operational_subscription,
    operationally_current_subscription_filters,
    require_aware_utc,
)
from app.services.walled_garden_policy import resolve_subscription_restriction

_ENDED_STATUSES = frozenset(
    {
        SubscriptionStatus.expired,
        SubscriptionStatus.canceled,
        SubscriptionStatus.archived,
        SubscriptionStatus.hidden,
    }
)
_UNAVAILABLE_STATUSES = frozenset(
    {
        SubscriptionStatus.blocked,
        SubscriptionStatus.suspended,
        SubscriptionStatus.disabled,
    }
)
_UNAVAILABLE_STATUS_VALUES = frozenset(
    {status.value for status in _UNAVAILABLE_STATUSES}
    | {SubscriptionStatus.stopped.value}
)


class SubscriptionEndDriftRepairClass(StrEnum):
    """Fail-closed operator classification for an ended non-terminal row."""

    single_newer_active_replacement = "single_newer_active_replacement"
    no_newer_active_replacement = "no_newer_active_replacement"
    ambiguous_newer_active_replacements = "ambiguous_newer_active_replacements"
    chronology_unavailable = "chronology_unavailable"
    current_status_requires_review = "current_status_requires_review"


@dataclass(frozen=True, slots=True)
class OperationalSubscriptionCohort:
    """One resolved subscription cohort shared by composed health projections."""

    subscriber_id: UUID
    as_of: datetime
    subscriptions: tuple[Subscription, ...]


@dataclass(frozen=True, slots=True)
class SubscriptionEndDriftQuery:
    """Typed scope for deterministic subscription end-date drift evidence."""

    as_of: datetime
    subscriber_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SubscriptionEndDriftItem:
    """One non-terminal subscription whose explicit end instant has passed."""

    subscription_id: UUID
    subscriber_id: UUID
    offer_id: UUID
    status: SubscriptionStatus
    start_at: datetime | None
    end_at: datetime
    cohort_reason: OperationalSubscriptionCohortReason
    newer_active_replacement_ids: tuple[UUID, ...]
    repair_class: SubscriptionEndDriftRepairClass


@dataclass(frozen=True, slots=True)
class SubscriptionEndDriftReport:
    """Stable read-only evidence; applying lifecycle transitions is out of scope."""

    as_of: datetime
    subscriber_id: UUID | None
    items: tuple[SubscriptionEndDriftItem, ...]
    fingerprint: str


def _persisted_utc(value: datetime) -> datetime:
    """Normalize a persisted timestamp (SQLite tests may strip its zone)."""

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _newer_active_replacements(
    row: Subscription,
    candidates: tuple[Subscription, ...],
) -> tuple[UUID, ...] | None:
    if row.start_at is None:
        return None
    row_started_at = _persisted_utc(row.start_at)
    newer_ids = []
    for candidate in candidates:
        if candidate.start_at is None:
            return None
        if _persisted_utc(candidate.start_at) > row_started_at:
            newer_ids.append(candidate.id)
    return tuple(sorted(newer_ids, key=str))


def _drift_repair_class(
    *,
    cohort_reason: OperationalSubscriptionCohortReason,
    replacements: tuple[UUID, ...] | None,
) -> SubscriptionEndDriftRepairClass:
    if cohort_reason != OperationalSubscriptionCohortReason.historical_explicit_end:
        return SubscriptionEndDriftRepairClass.current_status_requires_review
    if replacements is None:
        return SubscriptionEndDriftRepairClass.chronology_unavailable
    if len(replacements) == 1:
        return SubscriptionEndDriftRepairClass.single_newer_active_replacement
    if not replacements:
        return SubscriptionEndDriftRepairClass.no_newer_active_replacement
    return SubscriptionEndDriftRepairClass.ambiguous_newer_active_replacements


def build_subscription_end_drift_report(
    db: Session,
    *,
    query: SubscriptionEndDriftQuery,
) -> SubscriptionEndDriftReport:
    """Report passed ends on non-terminal rows without changing lifecycle state."""

    as_of = require_aware_utc(query.as_of)
    rows_query = (
        db.query(Subscription)
        .filter(Subscription.status.in_(PORTAL_VISIBLE_SERVICE_STATUSES))
        .filter(Subscription.end_at.is_not(None))
        .filter(Subscription.end_at <= as_of)
    )
    if query.subscriber_id is not None:
        rows_query = rows_query.filter(
            Subscription.subscriber_id == query.subscriber_id
        )
    rows = rows_query.order_by(Subscription.id.asc()).all()

    subscriber_ids = {row.subscriber_id for row in rows}
    active_rows = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id.in_(subscriber_ids))
        .filter(Subscription.status == SubscriptionStatus.active)
        .order_by(Subscription.id.asc())
        .all()
        if subscriber_ids
        else []
    )
    active_by_account_offer: dict[tuple[UUID, UUID], list[Subscription]] = {}
    for active_row in active_rows:
        active_by_account_offer.setdefault(
            (active_row.subscriber_id, active_row.offer_id), []
        ).append(active_row)

    items: list[SubscriptionEndDriftItem] = []
    for row in rows:
        if row.end_at is None:  # Defensive against persistence/type drift.
            continue
        end_at = _persisted_utc(row.end_at)
        decision = classify_operational_subscription(
            OperationalSubscriptionCohortInput(
                status=row.status,
                end_at=end_at,
                as_of=as_of,
            )
        )
        candidates = tuple(
            candidate
            for candidate in active_by_account_offer.get(
                (row.subscriber_id, row.offer_id), []
            )
            if candidate.id != row.id
        )
        replacement_ids = _newer_active_replacements(row, candidates)
        items.append(
            SubscriptionEndDriftItem(
                subscription_id=row.id,
                subscriber_id=row.subscriber_id,
                offer_id=row.offer_id,
                status=row.status,
                start_at=(
                    _persisted_utc(row.start_at) if row.start_at is not None else None
                ),
                end_at=end_at,
                cohort_reason=decision.reason,
                newer_active_replacement_ids=replacement_ids or (),
                repair_class=_drift_repair_class(
                    cohort_reason=decision.reason,
                    replacements=replacement_ids,
                ),
            )
        )

    material = {
        "as_of": as_of.isoformat(),
        "subscriber_id": str(query.subscriber_id) if query.subscriber_id else None,
        "items": [
            {
                "subscription_id": str(item.subscription_id),
                "subscriber_id": str(item.subscriber_id),
                "offer_id": str(item.offer_id),
                "status": item.status.value,
                "start_at": item.start_at.isoformat() if item.start_at else None,
                "end_at": item.end_at.isoformat(),
                "cohort_reason": item.cohort_reason.value,
                "newer_active_replacement_ids": [
                    str(value) for value in item.newer_active_replacement_ids
                ],
                "repair_class": item.repair_class.value,
            }
            for item in items
        ],
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return SubscriptionEndDriftReport(
        as_of=as_of,
        subscriber_id=query.subscriber_id,
        items=tuple(items),
        fingerprint=f"sha256:{hashlib.sha256(encoded).hexdigest()}",
    )


def _prepaid_threshold(
    db: Session, account: Subscriber, *, now: datetime | None = None
) -> Decimal:
    """The min-balance threshold used by the prepaid enforcement gate.

    Thin adapter. ``app.services.prepaid_threshold`` owns the rule; this
    delegates so the enforcement sweep and every batch consumer resolve the
    threshold through one implementation. Re-deriving it here would let an audit
    disagree with the enforcement it exists to check.
    """
    from app.services.prepaid_threshold import resolve_prepaid_threshold

    return resolve_prepaid_threshold(db, account, now=now)


def _overdue_summary(
    db: Session, account_id: UUID, now: datetime
) -> tuple[Decimal, datetime | None]:
    """Total past-due owed and the oldest overdue due date (mirrors dunning)."""
    rows = (
        db.query(Invoice.balance_due, Invoice.due_at)
        .filter(Invoice.account_id == coerce_uuid(account_id))
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.balance_due > 0)
        .filter(collection_due_date_eligible_filter())
        .filter(
            or_(
                Invoice.status == InvoiceStatus.overdue,
                and_(
                    Invoice.status.in_(
                        [InvoiceStatus.issued, InvoiceStatus.partially_paid]
                    ),
                    Invoice.due_at.is_not(None),
                    Invoice.due_at <= now,
                ),
            )
        )
        .all()
    )
    if not rows:
        return Decimal("0.00"), None
    outstanding = sum((Decimal(str(b or 0)) for b, _ in rows), Decimal("0.00"))
    dues = [d for _, d in rows if d is not None]
    return outstanding, (min(dues) if dues else None)


def build_service_status(
    db: Session,
    subscriber_id: UUID,
    *,
    as_of: datetime | None = None,
    cohort: OperationalSubscriptionCohort | None = None,
) -> ServiceStatusResponse:
    """Truthful per-account + per-service status for the authenticated caller."""
    resolved_subscriber_id = coerce_uuid(subscriber_id)
    if cohort is not None:
        if cohort.subscriber_id != resolved_subscriber_id:
            raise ValueError("Operational cohort belongs to another subscriber")
        now = require_aware_utc(cohort.as_of)
        if as_of is not None and require_aware_utc(as_of) != now:
            raise ValueError("Operational cohort and as_of instants do not match")
    else:
        now = require_aware_utc(as_of or datetime.now(UTC))
    account = db.get(Subscriber, resolved_subscriber_id)
    if account is None:
        # Caller authenticated but no subscriber row — empty, not an error.
        return ServiceStatusResponse(as_of=now, billing_mode=BillingMode.prepaid.value)

    # Resolve billing mode through the SAME authority as dunning/enforcement
    # (collectible-subscription-derived, prepaid-wins) so the customer-facing
    # view can never disagree with how the account is actually enforced — e.g.
    # a mixed/drifted account showing a prepaid wallet while dunning treats it
    # as postpaid. Deferred import avoids the service_status <-> collections
    # import cycle. Falls back to the account flag when there are no collectible
    # subscriptions to derive from.
    from app.services.collections._core import _effective_billing_mode_for_account

    account_mode = (
        _effective_billing_mode_for_account(db, account)
        or account.billing_mode
        or BillingMode.prepaid
    )
    resp = ServiceStatusResponse(as_of=now, billing_mode=account_mode.value)

    is_prepaid = account_mode == BillingMode.prepaid
    grace_until: datetime | None = None
    deactivation_at: datetime | None = account.prepaid_deactivation_at

    if is_prepaid:
        funding = resolve_prepaid_funding(db, account, now=now)
        balance = funding.available_balance
        threshold = funding.required_balance
        low = balance < threshold
        resp.balance = balance
        resp.min_balance = threshold
        resp.low_balance = low
        resp.deactivation_at = deactivation_at
        if low:
            low_at = account.prepaid_low_balance_at or now
            grace_decision = resolve_grace_decision(
                db,
                account,
                starts_at=low_at,
                as_of=now,
            )
            grace_until = grace_decision.ends_at
            resp.grace_until = grace_until
    else:
        resp.in_dunning = has_overdue_balance(db, str(subscriber_id))
        outstanding, oldest_due = _overdue_summary(db, subscriber_id, now)
        resp.outstanding = outstanding
        resp.oldest_overdue_due_at = oldest_due

    resolved_cohort = cohort or resolve_operational_subscription_cohort(
        db,
        subscriber_id=resolved_subscriber_id,
        as_of=now,
    )
    subs = resolved_cohort.subscriptions
    lock_reasons = _active_lock_reasons(db, [subscription.id for subscription in subs])

    for s in subs:
        usable = s.status == SubscriptionStatus.active
        restriction = resolve_subscription_restriction(db, s, account=account)
        access_block_reason = (
            resolve_customer_access(
                s,
                subscriber=account,
                access_restriction_mode=(
                    restriction.effective_mode if restriction else None
                ),
            ).access_block_reason
            if s.status in _UNAVAILABLE_STATUSES
            else None
        )
        reason, action = _service_reason_and_action(
            s,
            is_prepaid,
            resp,
            lock_reasons.get(s.id, frozenset()),
            access_block_reason,
        )
        expires_at = _service_expires_at(
            s, is_prepaid, resp, grace_until, deactivation_at, usable
        )
        resp.services.append(
            ServiceStatusItem(
                subscription_id=s.id,
                offer_name=s.offer.name if s.offer else None,
                status=s.status.value,
                status_presentation=subscription_status_presentation(s.status),
                billing_mode=(s.billing_mode or account_mode).value,
                usable=usable,
                expires_at=expires_at,
                next_charge_at=s.next_billing_at,
                reason=reason,
                action=action,
            )
        )
    resp.primary_action = _primary_action(resp.services, resp.currency)
    return resp


def resolve_operational_subscription_cohort(
    db: Session,
    *,
    subscriber_id: UUID,
    as_of: datetime,
) -> OperationalSubscriptionCohort:
    """Return the lifecycle-owned operational cohort used by health views."""
    evaluated_at = require_aware_utc(as_of)
    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == coerce_uuid(subscriber_id))
        .filter(*operationally_current_subscription_filters(as_of=evaluated_at))
        .options(selectinload(Subscription.offer))
        .order_by(Subscription.start_at.desc().nullslast())
        .all()
    )
    return OperationalSubscriptionCohort(
        subscriber_id=coerce_uuid(subscriber_id),
        as_of=evaluated_at,
        subscriptions=tuple(subscriptions),
    )


def _active_lock_reasons(
    db: Session, subscription_ids: list[object]
) -> dict[object, frozenset[EnforcementReason]]:
    if not subscription_ids:
        return {}
    locks = (
        db.query(EnforcementLock)
        .filter(EnforcementLock.subscription_id.in_(subscription_ids))
        .filter(EnforcementLock.is_active.is_(True))
        .all()
    )
    grouped: dict[object, set[EnforcementReason]] = {}
    for lock in locks:
        grouped.setdefault(lock.subscription_id, set()).add(lock.reason)
    return {
        subscription_id: frozenset(reasons)
        for subscription_id, reasons in grouped.items()
    }


def _service_reason_and_action(
    s: Subscription,
    is_prepaid: bool,
    resp: ServiceStatusResponse,
    lock_reasons: frozenset[EnforcementReason],
    access_block_reason: str | None,
) -> tuple[str, ServiceStatusAction | None]:
    service_name = s.offer.name if s.offer else "Your service"
    if s.status in _UNAVAILABLE_STATUSES:
        return _unavailable_service_action(
            service_name,
            lock_reasons,
            resp,
            access_block_reason=access_block_reason,
        )
    if s.status == SubscriptionStatus.stopped:
        return (
            "stopped",
            _contact_support_action(
                f"{service_name} is stopped — contact support to reactivate it.",
                resp.currency,
            ),
        )
    if s.status in _ENDED_STATUSES:
        return "ended", None
    # active / pending and running:
    if is_prepaid and resp.low_balance:
        amount = _prepaid_shortfall(resp)
        return (
            "low_balance",
            ServiceStatusAction(
                kind=ServiceStatusActionKind.top_up,
                label="Top up",
                message=(
                    f"Balance low — top up {_money(amount, resp.currency)} to keep "
                    "your service."
                    if amount is not None and amount > 0
                    else "Balance low — top up to keep your service."
                ),
                amount=amount,
                currency=resp.currency,
            ),
        )
    if not is_prepaid and resp.in_dunning:
        amount = _positive_amount(resp.outstanding)
        return (
            "overdue",
            ServiceStatusAction(
                kind=ServiceStatusActionKind.pay_invoices,
                label="Pay invoices",
                message=(
                    f"Payment overdue — pay {_money(amount, resp.currency)} to avoid "
                    "suspension."
                    if amount is not None
                    else "Payment overdue — pay now to avoid suspension."
                ),
                amount=amount,
                currency=resp.currency,
            ),
        )
    return "ok", None


def _unavailable_service_action(
    service_name: str,
    lock_reasons: frozenset[EnforcementReason],
    resp: ServiceStatusResponse,
    *,
    access_block_reason: str | None,
) -> tuple[str, ServiceStatusAction]:
    if access_block_reason and not access_block_reason.startswith(
        "subscription_status_"
    ):
        return (
            "suspended",
            _contact_support_action(
                f"{service_name} has an account-level hold — payment alone will not "
                "restore it. Contact support.",
                resp.currency,
            ),
        )
    if len(lock_reasons) != 1:
        reason = "multiple_holds" if lock_reasons else "suspended"
        detail = (
            f"{service_name} has more than one active hold — payment alone will not "
            "restore it. Contact support."
            if lock_reasons
            else f"{service_name} is unavailable — contact support to resolve it."
        )
        return reason, _contact_support_action(detail, resp.currency)

    lock_reason = next(iter(lock_reasons))
    if lock_reason == EnforcementReason.overdue:
        amount = _positive_amount(resp.outstanding)
        if amount is not None:
            return (
                "overdue",
                ServiceStatusAction(
                    kind=ServiceStatusActionKind.pay_invoices,
                    label="Pay invoices",
                    message=(
                        f"{service_name} is suspended — pay "
                        f"{_money(amount, resp.currency)} to restore it."
                    ),
                    amount=amount,
                    currency=resp.currency,
                    restores_service=True,
                ),
            )
    elif lock_reason == EnforcementReason.prepaid:
        amount = _prepaid_shortfall(resp)
        if amount is not None and amount > 0:
            return (
                "low_balance",
                ServiceStatusAction(
                    kind=ServiceStatusActionKind.top_up,
                    label="Top up",
                    message=(
                        f"{service_name} is suspended — top up "
                        f"{_money(amount, resp.currency)} to restore it."
                    ),
                    amount=amount,
                    currency=resp.currency,
                    restores_service=True,
                ),
            )
    elif lock_reason == EnforcementReason.fup:
        return (
            "fair_usage",
            ServiceStatusAction(
                kind=ServiceStatusActionKind.view_usage,
                label="View usage",
                message=(
                    f"{service_name} is limited by its fair-use policy — review "
                    "usage options."
                ),
                currency=resp.currency,
            ),
        )

    nonfinancial_reasons = {
        EnforcementReason.admin: "administrative_hold",
        EnforcementReason.customer_hold: "customer_hold",
        EnforcementReason.fraud: "fraud_review",
        EnforcementReason.system: "system_hold",
    }
    reason = nonfinancial_reasons.get(lock_reason, "suspended")
    return (
        reason,
        _contact_support_action(
            f"{service_name} is suspended for a reason payment cannot clear — "
            "contact support.",
            resp.currency,
        ),
    )


def _primary_action(
    services: list[ServiceStatusItem], currency: str
) -> ServiceStatusAction | None:
    unavailable = [
        item.action
        for item in services
        if item.status in _UNAVAILABLE_STATUS_VALUES
        if item.action is not None
    ]
    if unavailable:
        kinds = {action.kind for action in unavailable}
        if len(kinds) > 1:
            return _contact_support_action(
                "Your services have different active holds — payment alone may not "
                "restore them. Contact support.",
                currency,
            )
        action = unavailable[0]
        if len(unavailable) == 1:
            return action
        if action.kind == ServiceStatusActionKind.contact_support:
            return _contact_support_action(
                f"{len(unavailable)} services need support before they can be restored.",
                currency,
            )
        if action.kind == ServiceStatusActionKind.pay_invoices and action.amount:
            message = (
                f"{len(unavailable)} services are suspended — pay "
                f"{_money(action.amount, currency)} to restore them."
            )
        elif action.kind == ServiceStatusActionKind.top_up and action.amount:
            message = (
                f"{len(unavailable)} services are suspended — top up "
                f"{_money(action.amount, currency)} to restore them."
            )
        else:
            message = f"{len(unavailable)} services need attention — {action.message}"
        return action.model_copy(
            update={
                "message": message,
                "restores_service": all(
                    candidate.restores_service for candidate in unavailable
                ),
            }
        )

    for item in services:
        if item.usable and item.action is not None:
            return item.action
    return None


def _contact_support_action(message: str, currency: str) -> ServiceStatusAction:
    return ServiceStatusAction(
        kind=ServiceStatusActionKind.contact_support,
        label="Contact support",
        message=message,
        currency=currency,
    )


def _prepaid_shortfall(resp: ServiceStatusResponse) -> Decimal | None:
    if resp.balance is None or resp.min_balance is None:
        return None
    return max(resp.min_balance - resp.balance, Decimal("0.00"))


def _positive_amount(amount: Decimal | None) -> Decimal | None:
    if amount is None or amount <= 0:
        return None
    return amount


def _money(amount: Decimal, currency: str) -> str:
    return f"{currency} {amount:,.2f}"


def _service_expires_at(
    s: Subscription,
    is_prepaid: bool,
    resp: ServiceStatusResponse,
    grace_until: datetime | None,
    deactivation_at: datetime | None,
    usable: bool,
) -> datetime | None:
    # An explicit contract end is always the real lapse date.
    if s.end_at is not None:
        return s.end_at
    # A running prepaid service in low-balance will be cut when grace ends (then
    # fully deactivated later) — surface the earliest concrete cut date.
    if is_prepaid and usable and resp.low_balance:
        candidates = [d for d in (grace_until, deactivation_at) if d is not None]
        return min(candidates) if candidates else None
    # Otherwise there is no date-based expiry (postpaid, or healthy prepaid).
    return None
