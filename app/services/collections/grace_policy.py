"""Canonical grace-period policy resolution for collections decisions.

This module is an implementation detail of the ``financial.dunning`` and
prepaid-enforcement owners.  It gives every decision and read projection the
same effective grace value, provenance, and deadline.  It does not write
subscriber, invoice, timer, lock, or access state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session, selectinload

from app.models.catalog import BillingMode, PolicySet, Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Reseller, Subscriber
from app.services import settings_spec
from app.services.billing_profile import (
    require_effective_billing_mode,
    resolve_billing_profile,
)
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.domain_errors import DomainError


class GracePolicySource(StrEnum):
    ACCOUNT_OVERRIDE = "account_override"
    POLICY_SET = "policy_set"
    BILLING_MODE_DEFAULT = "billing_mode_default"


class GracePolicySetSource(StrEnum):
    EXPLICIT = "explicit"
    ACCOUNT = "account"
    RESELLER = "reseller"
    OFFER_VERSION = "offer_version"
    OFFER = "offer"
    BILLING_MODE_DEFAULT = "billing_mode_default"
    NONE = "none"


class GracePhase(StrEnum):
    NOT_STARTED = "not_started"
    IN_GRACE = "in_grace"
    ACTIONABLE = "actionable"


class GracePolicyError(DomainError):
    """Stable failure for invalid or ambiguous grace-policy evidence."""


@dataclass(frozen=True, slots=True)
class ResolvedPolicySet:
    policy_set_id: UUID | None
    source: GracePolicySetSource


@dataclass(frozen=True, slots=True)
class EffectiveGracePolicy:
    """The effective grace duration and the exact source that supplied it."""

    days: int
    source: GracePolicySource
    billing_mode: BillingMode
    policy_set_id: UUID | None
    policy_set_source: GracePolicySetSource = GracePolicySetSource.NONE

    def as_dict(self) -> dict[str, object]:
        return {
            "days": self.days,
            "source": self.source.value,
            "billing_mode": self.billing_mode.value,
            "policy_set_id": str(self.policy_set_id) if self.policy_set_id else None,
            "policy_set_source": self.policy_set_source.value,
        }


@dataclass(frozen=True, slots=True)
class GraceDecision:
    """Side-effect-free grace phase for one due/low-balance observation."""

    policy: EffectiveGracePolicy
    starts_at: datetime | None
    ends_at: datetime | None
    as_of: datetime
    phase: GracePhase
    elapsed_days_after_grace: int

    def as_dict(self) -> dict[str, object]:
        return {
            "policy": self.policy.as_dict(),
            "starts_at": self.starts_at.isoformat() if self.starts_at else None,
            "ends_at": self.ends_at.isoformat() if self.ends_at else None,
            "as_of": self.as_of.isoformat(),
            "phase": self.phase.value,
            "elapsed_days_after_grace": self.elapsed_days_after_grace,
        }


def _grace_days(value: object, *, source: GracePolicySource) -> int:
    if isinstance(value, bool):
        days = -1
    else:
        try:
            days = int(str(value))
        except (TypeError, ValueError):
            days = -1
    if days < 0:
        raise GracePolicyError(
            code="financial.grace_policy.invalid_grace_days",
            message="Grace days must be a non-negative integer.",
            details={"source": source.value},
        )
    return days


def effective_billing_mode(db: Session, account: Subscriber) -> BillingMode:
    """Resolve the same billing mode used by collections automation."""

    profile = resolve_billing_profile(db, account)
    return require_effective_billing_mode(profile)


def resolve_policy_set_decision(db: Session, account: Subscriber) -> ResolvedPolicySet:
    """Resolve account -> reseller -> offer/version -> mode-default policy."""

    if account.policy_set_id:
        return ResolvedPolicySet(account.policy_set_id, GracePolicySetSource.ACCOUNT)
    if account.reseller_id:
        reseller = db.get(Reseller, account.reseller_id)
        if reseller and reseller.policy_set_id:
            return ResolvedPolicySet(
                reseller.policy_set_id,
                GracePolicySetSource.RESELLER,
            )

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
            return ResolvedPolicySet(
                subscription.offer_version.policy_set_id,
                GracePolicySetSource.OFFER_VERSION,
            )
        if subscription.offer and subscription.offer.policy_set_id:
            return ResolvedPolicySet(
                subscription.offer.policy_set_id,
                GracePolicySetSource.OFFER,
            )

    mode = effective_billing_mode(db, account)
    key = (
        "default_prepaid_policy_set_id"
        if mode == BillingMode.prepaid
        else "default_postpaid_policy_set_id"
    )
    raw = settings_spec.resolve_value(db, SettingDomain.collections, key)
    if not raw:
        return ResolvedPolicySet(None, GracePolicySetSource.NONE)
    try:
        policy_set_id = UUID(str(raw))
    except (TypeError, ValueError):
        raise GracePolicyError(
            code="financial.grace_policy.invalid_policy_set_id",
            message="The default grace policy-set identifier is invalid.",
            details={"setting": f"collections.{key}"},
        ) from None
    return ResolvedPolicySet(
        policy_set_id,
        GracePolicySetSource.BILLING_MODE_DEFAULT,
    )


def resolve_policy_set_for_account(db: Session, account: Subscriber) -> UUID | None:
    """Return the policy-set identifier projection for compatibility callers."""

    return resolve_policy_set_decision(db, account).policy_set_id


def resolve_effective_grace_policy(
    db: Session,
    account: Subscriber,
    *,
    policy_set_id: UUID | None = None,
) -> EffectiveGracePolicy:
    """Resolve explicit account override -> policy -> billing-mode default."""

    mode = effective_billing_mode(db, account)
    selected_policy = (
        ResolvedPolicySet(policy_set_id, GracePolicySetSource.EXPLICIT)
        if policy_set_id is not None
        else resolve_policy_set_decision(db, account)
    )
    selected_policy_id = selected_policy.policy_set_id
    if account.grace_period_days is not None:
        return EffectiveGracePolicy(
            days=_grace_days(
                account.grace_period_days,
                source=GracePolicySource.ACCOUNT_OVERRIDE,
            ),
            source=GracePolicySource.ACCOUNT_OVERRIDE,
            billing_mode=mode,
            policy_set_id=selected_policy_id,
            policy_set_source=selected_policy.source,
        )

    policy = db.get(PolicySet, selected_policy_id) if selected_policy_id else None
    if policy is not None and policy.is_active and policy.grace_days is not None:
        return EffectiveGracePolicy(
            days=_grace_days(
                policy.grace_days,
                source=GracePolicySource.POLICY_SET,
            ),
            source=GracePolicySource.POLICY_SET,
            billing_mode=mode,
            policy_set_id=policy.id,
            policy_set_source=selected_policy.source,
        )

    key = f"{mode.value}_default_grace_period_days"
    raw = settings_spec.resolve_value(db, SettingDomain.billing, key)
    return EffectiveGracePolicy(
        days=_grace_days(raw, source=GracePolicySource.BILLING_MODE_DEFAULT),
        source=GracePolicySource.BILLING_MODE_DEFAULT,
        billing_mode=mode,
        policy_set_id=selected_policy_id,
        policy_set_source=selected_policy.source,
    )


def decide_grace(
    policy: EffectiveGracePolicy,
    *,
    starts_at: datetime | None,
    as_of: datetime | None = None,
) -> GraceDecision:
    """Resolve the phase using the date-based semantics used by dunning."""

    now = as_of or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if starts_at is None:
        return GraceDecision(
            policy=policy,
            starts_at=None,
            ends_at=None,
            as_of=now,
            phase=GracePhase.NOT_STARTED,
            elapsed_days_after_grace=0,
        )
    start = starts_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if policy.days == 0:
        return GraceDecision(
            policy=policy,
            starts_at=start,
            ends_at=start,
            as_of=now,
            phase=(GracePhase.ACTIONABLE if now >= start else GracePhase.IN_GRACE),
            elapsed_days_after_grace=max((now.date() - start.date()).days, 0),
        )
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
        phase=(
            GracePhase.IN_GRACE if raw_days <= policy.days else GracePhase.ACTIONABLE
        ),
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
