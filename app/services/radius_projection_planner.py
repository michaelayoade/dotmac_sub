"""Plan customer RADIUS projection from shared billing/access state."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.catalog import AccessState, Subscription, SubscriptionStatus
from app.models.enforcement_lock import AccessRestrictionMode
from app.models.subscriber import Subscriber
from app.services.access_resolution import (
    CustomerBillingAccessState,
    resolve_customer_access,
)
from app.services.radius_access_state import ACTIVE_STATUSES, BLOCKED_STATUSES


@dataclass(frozen=True)
class RadiusProjectionPlan:
    """Decision consumed by RADIUS writers.

    ``mode`` maps directly to radcheck/radreply behavior:
    - active: write Cleartext-Password and normal radreply
    - captive: write Cleartext-Password and walled-garden radreply
    - reject: write Auth-Type := Reject and no radreply
    - none: no RADIUS projection expected
    """

    mode: str
    access_state: AccessState | None
    blocked: bool
    radius_allowed: bool
    write_password: bool
    write_radreply: bool
    captive: bool
    block_reason: str | None
    billing_access_state: CustomerBillingAccessState


@dataclass(frozen=True)
class LoginRadiusProjection:
    """Canonical desired RADIUS mode for one physical login slot."""

    login: str
    subscription_id: str
    subscription_status: SubscriptionStatus
    plan: RadiusProjectionPlan


@dataclass(frozen=True)
class RadiusProjectionDrift:
    """Bidirectional difference between desired and observed RADIUS state."""

    missing_auth: frozenset[str]
    stale_auth: frozenset[str]
    missing_reject: frozenset[str]
    stale_reject: frozenset[str]
    missing_captive: frozenset[str]
    stale_captive: frozenset[str]
    attribute_drift: frozenset[str]
    missing_concurrency_check: frozenset[str]
    stale_concurrency_check: frozenset[str]
    misplaced_concurrency_reply: frozenset[str]

    @property
    def usernames(self) -> frozenset[str]:
        return frozenset().union(
            self.missing_auth,
            self.stale_auth,
            self.missing_reject,
            self.stale_reject,
            self.missing_captive,
            self.stale_captive,
            self.attribute_drift,
            self.missing_concurrency_check,
            self.stale_concurrency_check,
            self.misplaced_concurrency_reply,
        )


def plan_radius_projection(
    subscription,
    *,
    restriction_mode: AccessRestrictionMode | None = None,
) -> RadiusProjectionPlan:
    decision = resolve_customer_access(
        subscription,
        access_restriction_mode=restriction_mode,
    )
    state = decision.state
    mode = state.radius_mode
    return RadiusProjectionPlan(
        mode=mode,
        access_state=state.radius_access_state,
        blocked=state.radius_blocked,
        radius_allowed=state.radius_allowed,
        write_password=mode in {"active", "captive"},
        write_radreply=mode in {"active", "captive"},
        captive=mode == "captive",
        block_reason=state.access_block_reason,
        billing_access_state=state,
    )


def _prefer_login_candidate(
    current: LoginRadiusProjection | None,
    candidate: LoginRadiusProjection,
) -> LoginRadiusProjection:
    """Choose one deterministic owner for a login shared by multiple services."""
    if current is None:
        return candidate
    current_active = current.subscription_status == SubscriptionStatus.active
    candidate_active = candidate.subscription_status == SubscriptionStatus.active
    if candidate_active != current_active:
        return candidate if candidate_active else current
    return candidate if candidate.subscription_id < current.subscription_id else current


def plan_login_radius_projections(
    db: Session,
    subscriptions: Iterable[Subscription] | None = None,
) -> dict[str, LoginRadiusProjection]:
    """Resolve the exact per-login access modes consumed by projection and audit.

    This is the shared comparator boundary.  RADIUS writers and drift checks
    must not independently reinterpret subscriber or subscription statuses.
    """
    if subscriptions is None:
        subscriptions = (
            db.execute(
                select(Subscription)
                .options(
                    joinedload(Subscription.subscriber).joinedload(Subscriber.reseller)
                )
                .where(
                    Subscription.status.in_(ACTIVE_STATUSES | BLOCKED_STATUSES),
                    Subscription.login.isnot(None),
                )
            )
            .unique()
            .scalars()
            .all()
        )

    selected: dict[str, LoginRadiusProjection] = {}
    from app.services.walled_garden_policy import resolve_subscription_restriction

    for subscription in subscriptions:
        login = str(subscription.login or "").strip()
        if not login:
            continue
        restriction = resolve_subscription_restriction(
            db,
            subscription,
            account=subscription.subscriber,
        )
        candidate = LoginRadiusProjection(
            login=login,
            subscription_id=str(subscription.id),
            subscription_status=subscription.status,
            plan=plan_radius_projection(
                subscription,
                restriction_mode=(
                    restriction.effective_mode if restriction is not None else None
                ),
            ),
        )
        selected[login] = _prefer_login_candidate(selected.get(login), candidate)
    return selected


def compare_radius_projection(
    desired: dict[str, LoginRadiusProjection],
    *,
    observed_auth: set[str],
    observed_reject: set[str],
    observed_captive: set[str],
    desired_fingerprints: Mapping[str, str] | None = None,
    observed_fingerprints: Mapping[str, str] | None = None,
    enforce_simultaneous_use: bool = False,
    observed_simultaneous_use_check: set[str] | None = None,
    observed_simultaneous_use_reply: set[str] | None = None,
) -> RadiusProjectionDrift:
    """Compare external rows with the exact plans consumed by the writer."""
    desired_auth = {
        login
        for login, projection in desired.items()
        if projection.plan.mode in {"active", "captive", "reject"}
    }
    desired_reject = {
        login
        for login, projection in desired.items()
        if projection.plan.mode == "reject"
    }
    desired_captive = {
        login
        for login, projection in desired.items()
        if projection.plan.mode == "captive"
    }
    attribute_drift = {
        login
        for login, fingerprint in (desired_fingerprints or {}).items()
        if (observed_fingerprints or {}).get(login) != fingerprint
    }
    desired_concurrency_check = (
        {
            login
            for login, projection in desired.items()
            if projection.plan.mode in {"active", "captive"}
        }
        if enforce_simultaneous_use
        else set()
    )
    observed_concurrency_check = (
        observed_simultaneous_use_check or set() if enforce_simultaneous_use else set()
    )
    observed_concurrency_reply = (
        observed_simultaneous_use_reply or set() if enforce_simultaneous_use else set()
    )
    return RadiusProjectionDrift(
        missing_auth=frozenset(desired_auth - observed_auth),
        stale_auth=frozenset(observed_auth - desired_auth),
        missing_reject=frozenset(desired_reject - observed_reject),
        stale_reject=frozenset(observed_reject - desired_reject),
        missing_captive=frozenset(desired_captive - observed_captive),
        stale_captive=frozenset(observed_captive - desired_captive),
        attribute_drift=frozenset(attribute_drift),
        missing_concurrency_check=frozenset(
            desired_concurrency_check - observed_concurrency_check
        ),
        stale_concurrency_check=frozenset(
            observed_concurrency_check - desired_concurrency_check
        ),
        misplaced_concurrency_reply=frozenset(observed_concurrency_reply),
    )
