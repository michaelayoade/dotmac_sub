"""Canonical policy for the exceptional captive-access tier.

Hard reject is the safe default. Captive access is only an effective outcome
when a direct-house residential customer explicitly opted in and the shared
RADIUS captive contract is configured. Persisted intent is revalidated at read
time so stale flags or broken network configuration fail closed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from ipaddress import ip_network
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.models.enforcement_lock import AccessRestrictionMode, EnforcementLock
from app.models.subscriber import (
    Reseller,
    Subscriber,
    SubscriberCategory,
    SubscriberStatus,
    UserType,
)
from app.services import settings_spec


@dataclass(frozen=True, slots=True)
class WalledGardenDecision:
    requested_mode: AccessRestrictionMode
    effective_mode: AccessRestrictionMode
    explicit_opt_in: bool
    eligible: bool
    network_ready: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["requested_mode"] = self.requested_mode.value
        result["effective_mode"] = self.effective_mode.value
        return result


def _raw_subscriber_category(account: Subscriber) -> str | None:
    value = (account.metadata_ or {}).get("subscriber_category")
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def captive_account_eligible(account: Subscriber) -> bool:
    """Pure eligibility check when the reseller relationship is already loaded."""

    return bool(
        account.user_type == UserType.customer
        and account.is_active
        and account.status
        in {
            SubscriberStatus.active,
            SubscriberStatus.delinquent,
            SubscriberStatus.blocked,
            SubscriberStatus.suspended,
        }
        and _raw_subscriber_category(account) == SubscriberCategory.residential.value
        and account.reseller is not None
        and account.reseller.is_active
        and account.reseller.is_house
    )


def captive_eligibility_reason(db: Session, account: Subscriber) -> str | None:
    """Return why captive is forbidden, or ``None`` when account-eligible."""

    if account.user_type != UserType.customer:
        return "user_type_not_customer"
    if not account.is_active or account.status not in {
        SubscriberStatus.active,
        SubscriberStatus.delinquent,
        SubscriberStatus.blocked,
        SubscriberStatus.suspended,
    }:
        return "account_not_service_eligible"
    if _raw_subscriber_category(account) != SubscriberCategory.residential.value:
        return "category_not_explicit_residential"
    reseller = account.reseller
    if reseller is None and account.reseller_id is not None:
        reseller = db.get(Reseller, account.reseller_id)
    if reseller is None or not reseller.is_active or not reseller.is_house:
        return "not_direct_house_account"
    return None


def _network_readiness_reason(db: Session) -> str | None:
    enabled = settings_spec.resolve_value(
        db, SettingDomain.radius, "captive_redirect_enabled"
    )
    if not (
        enabled is True
        or str(enabled or "").strip().lower() in {"1", "true", "yes", "on"}
    ):
        return "captive_globally_disabled"

    portal_ip = settings_spec.resolve_value(
        db, SettingDomain.radius, "captive_portal_ip"
    )
    try:
        ip_network(str(portal_ip or "").strip(), strict=False)
    except ValueError:
        return "captive_portal_ip_invalid"

    portal_url = str(
        settings_spec.resolve_value(db, SettingDomain.radius, "captive_portal_url")
        or ""
    ).strip()
    parsed = urlparse(portal_url)
    if parsed.scheme != "https" or not parsed.hostname:
        return "captive_portal_url_invalid"
    return None


def resolve_walled_garden_decision(
    db: Session,
    account: Subscriber,
    *,
    requested_mode: AccessRestrictionMode,
) -> WalledGardenDecision:
    """Resolve persisted access intent to a safe effective network mode."""

    explicit_opt_in = bool(account.captive_redirect_enabled)
    eligibility_reason = captive_eligibility_reason(db, account)
    eligible = eligibility_reason is None

    if requested_mode == AccessRestrictionMode.hard_reject:
        return WalledGardenDecision(
            requested_mode=requested_mode,
            effective_mode=AccessRestrictionMode.hard_reject,
            explicit_opt_in=explicit_opt_in,
            eligible=eligible,
            network_ready=False,
            reason="hard_reject_requested",
        )
    if not explicit_opt_in:
        reason = "captive_not_opted_in"
    elif eligibility_reason:
        reason = eligibility_reason
    else:
        readiness_reason = _network_readiness_reason(db)
        if readiness_reason is None:
            return WalledGardenDecision(
                requested_mode=requested_mode,
                effective_mode=AccessRestrictionMode.captive,
                explicit_opt_in=True,
                eligible=True,
                network_ready=True,
                reason="captive_ready",
            )
        reason = readiness_reason
    return WalledGardenDecision(
        requested_mode=requested_mode,
        effective_mode=AccessRestrictionMode.hard_reject,
        explicit_opt_in=explicit_opt_in,
        eligible=eligible,
        network_ready=False,
        reason=reason,
    )


def resolve_subscription_restriction(
    db: Session,
    subscription: Subscription,
    *,
    account: Subscriber | None = None,
) -> WalledGardenDecision | None:
    """Resolve all active locks using most-restrictive-wins semantics."""

    subscriber = account or subscription.subscriber
    if subscriber is None:
        subscriber = db.get(Subscriber, subscription.subscriber_id)
    if subscriber is None:
        return None

    if subscription.status in {
        SubscriptionStatus.disabled,
        SubscriptionStatus.hidden,
        SubscriptionStatus.archived,
        SubscriptionStatus.canceled,
        SubscriptionStatus.expired,
    }:
        return resolve_walled_garden_decision(
            db,
            subscriber,
            requested_mode=AccessRestrictionMode.hard_reject,
        )

    modes = list(
        db.scalars(
            select(EnforcementLock.access_mode)
            .where(EnforcementLock.subscription_id == subscription.id)
            .where(EnforcementLock.is_active.is_(True))
        ).all()
    )
    if AccessRestrictionMode.hard_reject in modes:
        requested = AccessRestrictionMode.hard_reject
    elif modes and all(mode == AccessRestrictionMode.captive for mode in modes):
        requested = AccessRestrictionMode.captive
    elif subscription.status in {
        SubscriptionStatus.blocked,
        SubscriptionStatus.suspended,
        SubscriptionStatus.stopped,
    }:
        # Historical restrictions without structured evidence fail closed.
        requested = AccessRestrictionMode.hard_reject
    else:
        return None
    return resolve_walled_garden_decision(
        db,
        subscriber,
        requested_mode=requested,
    )
