"""Canonical policy for the exceptional captive-access tier.

Hard reject is the safe default. Captive access is an effective outcome for a
SUBSCRIPTION only when every gate below passes, evaluated in this order:

1. the request is captive (a hard-reject request is never upgraded);
2. fixed safety rails: the account is a customer principal (never a system
   user, reseller principal or vendor), is active with a service-eligible
   status, and the subscription is not disabled, canceled or terminal;
3. ``access.captive_access_policy`` resolves an ``allow`` rule for the
   subscription (most specific scope wins, deny beats allow, default deny);
4. the shared RADIUS captive contract is configured (global readiness);
5. ``access.captive_router_gate``: every router serving the subscription is
   ``ready`` per ``access.walled_garden_router_readiness``.

Persisted intent is revalidated at read time so stale rules, routers, or
broken network configuration fail closed. ``Subscriber.captive_redirect_enabled``
is no longer a decision input; migration 667 converted every opt-in into an
``account`` allow rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from ipaddress import ip_network
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.captive_access_policy import CaptiveAccessRuleScope
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.models.enforcement_lock import AccessRestrictionMode, EnforcementLock
from app.models.subscriber import (
    Subscriber,
    SubscriberStatus,
    UserType,
)
from app.services import settings_spec
from app.services.captive_access_policy import (
    CaptivePolicySnapshot,
    load_captive_policy_snapshot,
    subscription_facts,
)
from app.services.captive_router_gate import (
    CaptiveRouterGate,
    CaptiveRouterGateDecision,
    CaptiveRouterGateStatus,
)


class WalledGardenReason(StrEnum):
    USER_TYPE_NOT_CUSTOMER = "user_type_not_customer"
    ACCOUNT_NOT_SERVICE_ELIGIBLE = "account_not_service_eligible"
    SUBSCRIPTION_SCOPE_REQUIRED = "subscription_scope_required"
    SUBSCRIPTION_NOT_CAPTIVE_ELIGIBLE = "subscription_not_captive_eligible"
    CAPTIVE_POLICY_DENIED = "captive_policy_denied"
    CAPTIVE_NO_POLICY_RULE = "captive_no_policy_rule"
    CAPTIVE_GLOBALLY_DISABLED = "captive_globally_disabled"
    CAPTIVE_PORTAL_IP_INVALID = "captive_portal_ip_invalid"
    CAPTIVE_PORTAL_URL_INVALID = "captive_portal_url_invalid"
    ROUTER_UNRESOLVED = "router_unresolved"
    ROUTER_NOT_READY = "router_not_ready"
    NO_TARGET_SUBSCRIPTIONS = "no_target_subscriptions"
    HARD_REJECT_REQUESTED = "hard_reject_requested"
    CAPTIVE_READY = "captive_ready"


#: Subscription statuses that never receive captive access (safety rail).
CAPTIVE_FORBIDDEN_SUBSCRIPTION_STATUSES = frozenset(
    {
        SubscriptionStatus.disabled,
        SubscriptionStatus.hidden,
        SubscriptionStatus.archived,
        SubscriptionStatus.canceled,
        SubscriptionStatus.expired,
    }
)

_SERVICE_ELIGIBLE_ACCOUNT_STATUSES = frozenset(
    {
        SubscriberStatus.active,
        SubscriberStatus.delinquent,
        SubscriberStatus.blocked,
        SubscriberStatus.suspended,
    }
)


@dataclass(frozen=True, slots=True)
class WalledGardenDecision:
    """Typed captive/hard-reject outcome for one subscription.

    ``explicit_opt_in`` means a captive access policy rule ALLOWS captive for
    the subscription (the field keeps its historical name for callers).
    """

    requested_mode: AccessRestrictionMode
    effective_mode: AccessRestrictionMode
    explicit_opt_in: bool
    eligible: bool
    network_ready: bool
    reason: WalledGardenReason
    subscription_id: UUID | None = None
    policy_rule_id: UUID | None = None
    policy_scope: CaptiveAccessRuleScope | None = None
    router_gate: CaptiveRouterGateStatus | None = None
    router_ids: tuple[UUID, ...] = ()

    def as_dict(self) -> dict[str, str | bool | list[str] | None]:
        return {
            "requested_mode": self.requested_mode.value,
            "effective_mode": self.effective_mode.value,
            "explicit_opt_in": self.explicit_opt_in,
            "eligible": self.eligible,
            "network_ready": self.network_ready,
            "reason": self.reason.value,
            "subscription_id": (
                str(self.subscription_id) if self.subscription_id else None
            ),
            "policy_rule_id": str(self.policy_rule_id) if self.policy_rule_id else None,
            "policy_scope": self.policy_scope.value if self.policy_scope else None,
            "router_gate": self.router_gate.value if self.router_gate else None,
            "router_ids": [str(item) for item in self.router_ids],
        }


@dataclass
class WalledGardenEvaluation:
    """Per-run cache of policy, network, and router evidence.

    Build one per evaluation run (RADIUS sweep, financial preview, policy
    preview/apply) so rules, settings, and router readiness are read once.
    ``policy`` may be injected to evaluate a candidate (not yet written)
    policy in a preview.
    """

    db: Session
    policy: CaptivePolicySnapshot | None = None
    router_gate: CaptiveRouterGate | None = None
    _network_reason: WalledGardenReason | None = field(default=None, init=False)
    _network_loaded: bool = field(default=False, init=False)

    def policy_snapshot(self) -> CaptivePolicySnapshot:
        if self.policy is None:
            self.policy = load_captive_policy_snapshot(self.db)
        return self.policy

    def gate(self) -> CaptiveRouterGate:
        if self.router_gate is None:
            self.router_gate = CaptiveRouterGate(self.db)
        return self.router_gate

    def network_readiness_reason(self) -> WalledGardenReason | None:
        if not self._network_loaded:
            self._network_reason = _network_readiness_reason(self.db)
            self._network_loaded = True
        return self._network_reason


def _account_rail_reason(account: Subscriber) -> WalledGardenReason | None:
    if account.user_type != UserType.customer:
        return WalledGardenReason.USER_TYPE_NOT_CUSTOMER
    if not account.is_active or account.status not in (
        _SERVICE_ELIGIBLE_ACCOUNT_STATUSES
    ):
        return WalledGardenReason.ACCOUNT_NOT_SERVICE_ELIGIBLE
    return None


def _network_readiness_reason(db: Session) -> WalledGardenReason | None:
    enabled = settings_spec.resolve_value(
        db, SettingDomain.radius, "captive_redirect_enabled"
    )
    if not (
        enabled is True
        or str(enabled or "").strip().lower() in {"1", "true", "yes", "on"}
    ):
        return WalledGardenReason.CAPTIVE_GLOBALLY_DISABLED

    portal_ip = settings_spec.resolve_value(
        db, SettingDomain.radius, "captive_portal_ip"
    )
    try:
        ip_network(str(portal_ip or "").strip(), strict=False)
    except ValueError:
        return WalledGardenReason.CAPTIVE_PORTAL_IP_INVALID

    portal_url = str(
        settings_spec.resolve_value(db, SettingDomain.radius, "captive_portal_url")
        or ""
    ).strip()
    parsed = urlparse(portal_url)
    if parsed.scheme != "https" or not parsed.hostname:
        return WalledGardenReason.CAPTIVE_PORTAL_URL_INVALID
    return None


def _hard_reject(
    requested_mode: AccessRestrictionMode,
    reason: WalledGardenReason,
    *,
    subscription: Subscription | None,
    explicit_opt_in: bool = False,
    eligible: bool = False,
    policy_rule_id: UUID | None = None,
    policy_scope: CaptiveAccessRuleScope | None = None,
    gate: CaptiveRouterGateDecision | None = None,
) -> WalledGardenDecision:
    return WalledGardenDecision(
        requested_mode=requested_mode,
        effective_mode=AccessRestrictionMode.hard_reject,
        explicit_opt_in=explicit_opt_in,
        eligible=eligible,
        network_ready=False,
        reason=reason,
        subscription_id=subscription.id if subscription is not None else None,
        policy_rule_id=policy_rule_id,
        policy_scope=policy_scope,
        router_gate=gate.status if gate is not None else None,
        router_ids=gate.router_ids if gate is not None else (),
    )


def resolve_walled_garden_decision(
    db: Session,
    account: Subscriber,
    *,
    requested_mode: AccessRestrictionMode,
    subscription: Subscription | None,
    evaluation: WalledGardenEvaluation | None = None,
) -> WalledGardenDecision:
    """Resolve one subscription's requested restriction to a safe effective mode."""

    if requested_mode == AccessRestrictionMode.hard_reject:
        return _hard_reject(
            requested_mode,
            WalledGardenReason.HARD_REJECT_REQUESTED,
            subscription=subscription,
        )
    rail = _account_rail_reason(account)
    if rail is not None:
        return _hard_reject(requested_mode, rail, subscription=subscription)
    if subscription is None:
        return _hard_reject(
            requested_mode,
            WalledGardenReason.SUBSCRIPTION_SCOPE_REQUIRED,
            subscription=None,
        )
    if subscription.status in CAPTIVE_FORBIDDEN_SUBSCRIPTION_STATUSES:
        return _hard_reject(
            requested_mode,
            WalledGardenReason.SUBSCRIPTION_NOT_CAPTIVE_ELIGIBLE,
            subscription=subscription,
        )

    context = evaluation or WalledGardenEvaluation(db)
    resolution = context.policy_snapshot().resolve(
        subscription_facts(db, subscription, account)
    )
    if not resolution.allows:
        return _hard_reject(
            requested_mode,
            (
                WalledGardenReason.CAPTIVE_NO_POLICY_RULE
                if resolution.defaulted
                else WalledGardenReason.CAPTIVE_POLICY_DENIED
            ),
            subscription=subscription,
            eligible=True,
            policy_rule_id=resolution.rule_id,
            policy_scope=resolution.scope,
        )
    network_reason = context.network_readiness_reason()
    if network_reason is not None:
        return _hard_reject(
            requested_mode,
            network_reason,
            subscription=subscription,
            explicit_opt_in=True,
            eligible=True,
            policy_rule_id=resolution.rule_id,
            policy_scope=resolution.scope,
        )
    gate = context.gate().decide(subscription)
    if not gate.is_ready:
        return _hard_reject(
            requested_mode,
            (
                WalledGardenReason.ROUTER_UNRESOLVED
                if gate.status is CaptiveRouterGateStatus.router_unresolved
                else WalledGardenReason.ROUTER_NOT_READY
            ),
            subscription=subscription,
            explicit_opt_in=True,
            eligible=True,
            policy_rule_id=resolution.rule_id,
            policy_scope=resolution.scope,
            gate=gate,
        )
    return WalledGardenDecision(
        requested_mode=requested_mode,
        effective_mode=AccessRestrictionMode.captive,
        explicit_opt_in=True,
        eligible=True,
        network_ready=True,
        reason=WalledGardenReason.CAPTIVE_READY,
        subscription_id=subscription.id,
        policy_rule_id=resolution.rule_id,
        policy_scope=resolution.scope,
        router_gate=gate.status,
        router_ids=gate.router_ids,
    )


def aggregate_walled_garden_decisions(
    requested_mode: AccessRestrictionMode,
    decisions: list[WalledGardenDecision],
) -> WalledGardenDecision:
    """Account-level summary of per-subscription decisions.

    Captive only when every target subscription resolved captive; otherwise
    the first (by subscription id) hard-reject decision explains the summary.
    """

    if not decisions:
        return _hard_reject(
            requested_mode,
            (
                WalledGardenReason.HARD_REJECT_REQUESTED
                if requested_mode == AccessRestrictionMode.hard_reject
                else WalledGardenReason.NO_TARGET_SUBSCRIPTIONS
            ),
            subscription=None,
        )
    ordered = sorted(decisions, key=lambda item: str(item.subscription_id))
    rejected = next(
        (
            item
            for item in ordered
            if item.effective_mode == AccessRestrictionMode.hard_reject
        ),
        None,
    )
    return rejected or ordered[0]


def resolve_subscription_restriction(
    db: Session,
    subscription: Subscription,
    *,
    account: Subscriber | None = None,
    evaluation: WalledGardenEvaluation | None = None,
) -> WalledGardenDecision | None:
    """Resolve all active locks using most-restrictive-wins semantics."""

    subscriber = account or subscription.subscriber
    if subscriber is None:
        subscriber = db.get(Subscriber, subscription.subscriber_id)
    if subscriber is None:
        return None

    if subscription.status in CAPTIVE_FORBIDDEN_SUBSCRIPTION_STATUSES:
        return resolve_walled_garden_decision(
            db,
            subscriber,
            requested_mode=AccessRestrictionMode.hard_reject,
            subscription=subscription,
        )

    modes = list(
        db.scalars(
            select(EnforcementLock.access_mode)
            .where(EnforcementLock.subscription_id == subscription.id)
            .where(EnforcementLock.is_active.is_(True))
        ).all()
    )
    return resolve_restriction_from_lock_modes(
        db,
        subscription,
        account=subscriber,
        lock_modes=modes,
        evaluation=evaluation,
    )


def resolve_restriction_from_lock_modes(
    db: Session,
    subscription: Subscription,
    *,
    account: Subscriber,
    lock_modes: list[AccessRestrictionMode],
    evaluation: WalledGardenEvaluation | None = None,
) -> WalledGardenDecision | None:
    """Most-restrictive-wins over an explicit set of active lock modes.

    Shared by :func:`resolve_subscription_restriction` (modes read from the
    database) and the captive policy-change preview (modes as they would be
    after re-evaluation), so both apply identical semantics.
    """

    subscriber = account
    modes = lock_modes
    if subscription.status in CAPTIVE_FORBIDDEN_SUBSCRIPTION_STATUSES:
        return resolve_walled_garden_decision(
            db,
            subscriber,
            requested_mode=AccessRestrictionMode.hard_reject,
            subscription=subscription,
        )
    if AccessRestrictionMode.hard_reject in modes:
        requested = AccessRestrictionMode.hard_reject
    elif modes and all(mode == AccessRestrictionMode.captive for mode in modes):
        requested = AccessRestrictionMode.captive
    elif subscription.status in {
        SubscriptionStatus.blocked,
        SubscriptionStatus.paused,
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
        subscription=subscription,
        evaluation=evaluation,
    )


__all__ = [
    "CAPTIVE_FORBIDDEN_SUBSCRIPTION_STATUSES",
    "WalledGardenDecision",
    "WalledGardenEvaluation",
    "WalledGardenReason",
    "aggregate_walled_garden_decisions",
    "resolve_restriction_from_lock_modes",
    "resolve_subscription_restriction",
    "resolve_walled_garden_decision",
]
