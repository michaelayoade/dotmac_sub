"""Canonical grace-period policy resolution for collections decisions.

This module is an implementation detail of the ``financial.dunning`` and
prepaid-enforcement owners.  It gives every decision and read projection the
same effective grace value, provenance, and deadline.  It does not write
subscriber, invoice, timer, lock, or access state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, selectinload

from app.models.catalog import BillingMode, PolicySet, Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Reseller, Subscriber
from app.services import settings_spec
from app.services.billing_profile import resolve_billing_profile
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES


@dataclass(frozen=True, slots=True)
class EffectiveGracePolicy:
    """The effective grace duration and the exact source that supplied it."""

    days: int
    source: str
    billing_mode: BillingMode
    policy_set_id: UUID | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "source": self.source,
            "billing_mode": self.billing_mode.value,
            "policy_set_id": str(self.policy_set_id) if self.policy_set_id else None,
        }


@dataclass(frozen=True, slots=True)
class GraceDecision:
    """Side-effect-free grace phase for one due/low-balance observation."""

    policy: EffectiveGracePolicy
    starts_at: datetime | None
    ends_at: datetime | None
    as_of: datetime
    phase: str
    elapsed_days_after_grace: int

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["policy"] = self.policy.as_dict()
        for key in ("starts_at", "ends_at", "as_of"):
            value = result[key]
            result[key] = value.isoformat() if value is not None else None
        return result


def effective_billing_mode(db: Session, account: Subscriber) -> BillingMode:
    """Resolve the same billing mode used by collections automation."""

    profile = resolve_billing_profile(db, account)
    return profile.effective_mode or account.billing_mode or BillingMode.prepaid


def resolve_policy_set_for_account(db: Session, account: Subscriber) -> UUID | None:
    """Resolve account -> reseller -> offer/version -> mode-default policy."""

    if account.policy_set_id:
        return account.policy_set_id
    if account.reseller_id:
        reseller = db.get(Reseller, account.reseller_id)
        if reseller and reseller.policy_set_id:
            return reseller.policy_set_id

    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == account.id)
        .filter(Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES))
        .options(
            selectinload(Subscription.offer_version),
            selectinload(Subscription.offer),
        )
        .all()
    )
    priority = {
        SubscriptionStatus.active: 0,
        SubscriptionStatus.suspended: 1,
        SubscriptionStatus.pending: 2,
        SubscriptionStatus.blocked: 3,
    }
    subscriptions.sort(
        key=lambda subscription: (
            priority.get(subscription.status, 99),
            -(subscription.created_at.timestamp() if subscription.created_at else 0),
        )
    )
    for subscription in subscriptions:
        if subscription.offer_version and subscription.offer_version.policy_set_id:
            return subscription.offer_version.policy_set_id
        if subscription.offer and subscription.offer.policy_set_id:
            return subscription.offer.policy_set_id

    mode = effective_billing_mode(db, account)
    key = (
        "default_prepaid_policy_set_id"
        if mode == BillingMode.prepaid
        else "default_postpaid_policy_set_id"
    )
    from app.models.domain_settings import DomainSetting

    raw = (
        db.query(DomainSetting.value_text)
        .filter(DomainSetting.domain == SettingDomain.collections)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .scalar()
    )
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (TypeError, ValueError):
        return None


def resolve_effective_grace_policy(
    db: Session,
    account: Subscriber,
    *,
    policy_set_id: UUID | None = None,
) -> EffectiveGracePolicy:
    """Resolve explicit account override -> policy -> billing-mode default."""

    mode = effective_billing_mode(db, account)
    selected_policy_id = policy_set_id or resolve_policy_set_for_account(db, account)
    if account.grace_period_days is not None:
        return EffectiveGracePolicy(
            days=max(0, int(account.grace_period_days)),
            source="account_override",
            billing_mode=mode,
            policy_set_id=selected_policy_id,
        )

    policy = db.get(PolicySet, selected_policy_id) if selected_policy_id else None
    if policy is not None and policy.is_active and policy.grace_days is not None:
        return EffectiveGracePolicy(
            days=max(0, int(policy.grace_days)),
            source="policy_set",
            billing_mode=mode,
            policy_set_id=policy.id,
        )

    key = f"{mode.value}_default_grace_period_days"
    raw = settings_spec.resolve_value(db, SettingDomain.billing, key)
    try:
        days = max(0, int(str(raw or 0)))
    except (TypeError, ValueError):
        days = 0
    return EffectiveGracePolicy(
        days=days,
        source="billing_mode_default",
        billing_mode=mode,
        policy_set_id=selected_policy_id,
    )


def decide_grace(
    policy: EffectiveGracePolicy,
    *,
    starts_at: datetime | None,
    as_of: datetime | None = None,
) -> GraceDecision:
    """Resolve the phase using the date-based semantics used by dunning."""

    now = as_of or datetime.now(UTC)
    if starts_at is None:
        return GraceDecision(
            policy=policy,
            starts_at=None,
            ends_at=None,
            as_of=now,
            phase="not_started",
            elapsed_days_after_grace=0,
        )
    start = starts_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    raw_days = max((now.date() - start.date()).days, 0)
    elapsed = max(raw_days - policy.days, 0)
    ends_at = datetime.combine(
        start.date() + timedelta(days=policy.days + 1),
        time.min,
        tzinfo=start.tzinfo,
    )
    return GraceDecision(
        policy=policy,
        starts_at=start,
        ends_at=ends_at,
        as_of=now,
        phase="in_grace" if raw_days <= policy.days else "actionable",
        elapsed_days_after_grace=elapsed,
    )


def resolve_grace_decision(
    db: Session,
    account: Subscriber,
    *,
    starts_at: datetime | None,
    as_of: datetime | None = None,
    policy_set_id: UUID | None = None,
) -> GraceDecision:
    return decide_grace(
        resolve_effective_grace_policy(
            db,
            account,
            policy_set_id=policy_set_id,
        ),
        starts_at=starts_at,
        as_of=as_of,
    )
