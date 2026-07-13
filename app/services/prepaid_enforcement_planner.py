"""Side-effect-free planning for prepaid balance enforcement.

The production sweep and the operator dry-run consume the same account decision
function.  Planning never writes timers, queues notices, changes service state,
or sends network commands.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.catalog import BillingMode, Subscription, SubscriptionBundle
from app.models.domain_settings import SettingDomain
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import control_registry, enforcement_window, settings_spec
from app.services.access_resolution import (
    PrepaidFundingDecision,
    resolve_prepaid_funding,
)
from app.services.billing_communication_policy import (
    billing_communication_decisions,
)
from app.services.billing_enforcement_guards import (
    EnforcementHealth,
    billing_enforcement_health,
)
from app.services.billing_profile import resolve_billing_profile
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.collections._core import _bulk_dunning_shield_reasons
from app.services.common import coerce_uuid

PREPAID_BALANCE_ENFORCEMENT_CONTROL = "collections.prepaid_balance_enforcement"

_RELEVANT_STATUSES = tuple(COLLECTIBLE_SERVICE_STATUSES)


class PrepaidEnforcementAction(str, Enum):
    not_applicable = "not_applicable"
    billing_profile_invalid = "billing_profile_invalid"
    clear_stale_timers = "clear_stale_timers"
    restore = "restore"
    warn = "warn"
    waiting = "waiting"
    deferred = "deferred"
    suspend = "suspend"
    shielded = "shielded"
    health_blocked = "health_blocked"
    state_drift = "state_drift"
    already_suspended = "already_suspended"
    ok = "ok"


@dataclass(frozen=True)
class PrepaidEnforcementPolicy:
    deactivation_days: int
    warning_subject: str
    warning_body: str
    deactivation_subject: str
    deactivation_body: str
    blocking_time: str | None
    skip_weekends: bool
    skip_holidays: tuple[str, ...]

    def report_values(self) -> dict[str, Any]:
        """Return operational policy without customer-message templates."""
        return {
            "deactivation_days": self.deactivation_days,
            "blocking_time": self.blocking_time,
            "skip_weekends": self.skip_weekends,
            "skip_holidays": list(self.skip_holidays),
        }


@dataclass(frozen=True)
class PrepaidEnforcementPlanItem:
    account_id: str
    action: PrepaidEnforcementAction
    reason: str
    account_status: str
    derived_account_status: str
    account_status_drift: bool
    billing_mode: str | None
    available_balance: Decimal
    required_balance: Decimal
    active_subscription_count: int
    suspended_subscription_count: int
    active_prepaid_lock_count: int
    prepaid_low_balance_at: datetime | None
    deactivation_due_at: datetime | None
    prepaid_deactivation_at: datetime | None
    notice_suppression_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["action"] = self.action.value
        return result


@dataclass(frozen=True)
class PrepaidEnforcementPlan:
    generated_at: datetime
    control_enabled: bool
    policy: PrepaidEnforcementPolicy
    items: tuple[PrepaidEnforcementPlanItem, ...]

    @property
    def action_counts(self) -> dict[str, int]:
        counts = Counter(item.action.value for item in self.items)
        return dict(sorted(counts.items()))

    def to_dict(self, *, include_items: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "generated_at": self.generated_at,
            "control_enabled": self.control_enabled,
            "policy": self.policy.report_values(),
            "accounts": len(self.items),
            "action_counts": self.action_counts,
            "account_status_drift": sum(
                1 for item in self.items if item.account_status_drift
            ),
            "notice_suppressed": sum(
                1 for item in self.items if item.notice_suppression_reason
            ),
        }
        if include_items:
            result["items"] = [item.to_dict() for item in self.items]
        return result


def prepaid_balance_enforcement_enabled(db: Session) -> bool:
    return control_registry.is_enabled(db, PREPAID_BALANCE_ENFORCEMENT_CONTROL)


def resolve_prepaid_enforcement_policy(db: Session) -> PrepaidEnforcementPolicy:
    def _string(key: str) -> str:
        value = settings_spec.resolve_value(db, SettingDomain.collections, key)
        return str(value) if value is not None else ""

    days_raw = settings_spec.resolve_value(
        db, SettingDomain.collections, "prepaid_deactivation_days"
    )
    try:
        deactivation_days = max(0, int(str(days_raw))) if days_raw is not None else 0
    except (TypeError, ValueError):
        deactivation_days = 0

    holidays_raw = settings_spec.resolve_value(
        db, SettingDomain.collections, "prepaid_skip_holidays"
    )
    holidays = (
        tuple(str(day) for day in holidays_raw)
        if isinstance(holidays_raw, list)
        else ()
    )
    blocking_time = settings_spec.resolve_value(
        db, SettingDomain.collections, "prepaid_blocking_time"
    )
    return PrepaidEnforcementPolicy(
        deactivation_days=deactivation_days,
        warning_subject=_string("prepaid_warning_subject"),
        warning_body=_string("prepaid_warning_body"),
        deactivation_subject=_string("prepaid_deactivation_subject"),
        deactivation_body=_string("prepaid_deactivation_body"),
        blocking_time=str(blocking_time) if blocking_time is not None else None,
        skip_weekends=bool(
            settings_spec.resolve_value(
                db, SettingDomain.collections, "prepaid_skip_weekends"
            )
        ),
        skip_holidays=holidays,
    )


def candidate_prepaid_account_ids(db: Session) -> set[Any]:
    """Accounts with collectible prepaid service or stale prepaid timers."""
    ids: set[Any] = {
        row[0]
        for row in (
            db.query(Subscriber.id)
            .join(Subscription, Subscription.subscriber_id == Subscriber.id)
            .filter(
                or_(
                    Subscriber.billing_mode == BillingMode.prepaid,
                    Subscription.billing_mode == BillingMode.prepaid,
                )
            )
            .filter(Subscription.status.in_(_RELEVANT_STATUSES))
            .distinct()
            .all()
        )
    }
    ids.update(
        row[0]
        for row in (
            db.query(Subscriber.id)
            .filter(Subscriber.billing_mode == BillingMode.prepaid)
            .filter(
                or_(
                    Subscriber.prepaid_low_balance_at.is_not(None),
                    Subscriber.prepaid_deactivation_at.is_not(None),
                )
            )
            .all()
        )
    )
    return ids


def prepaid_notice_suppression_reasons(
    db: Session, account_ids: set[Any] | list[Any]
) -> dict[Any, str]:
    """Map accounts whose billing notices are fault-suppressed to the reason."""
    ids = set(account_ids)
    if not ids:
        return {}
    subscriptions = list(
        db.scalars(
            select(Subscription).where(Subscription.subscriber_id.in_(ids))
        ).all()
    )
    decisions = billing_communication_decisions(db, subscriptions)
    reasons: dict[Any, str] = {}
    for subscription in subscriptions:
        decision = decisions.get(subscription.id)
        if decision is None or not decision.suppress_suspension_notice:
            continue
        if decision.reason:
            reasons.setdefault(subscription.subscriber_id, decision.reason)
    return reasons


def _dedicated_bundle_account_ids(db: Session, account_ids: list[Any]) -> set[Any]:
    if not account_ids:
        return set()
    return {
        row[0]
        for row in db.execute(
            select(Subscription.subscriber_id)
            .join(
                SubscriptionBundle,
                Subscription.bundle_id == SubscriptionBundle.id,
            )
            .where(
                Subscription.subscriber_id.in_(account_ids),
                SubscriptionBundle.is_dedicated.is_(True),
            )
            .distinct()
        ).all()
    }


def _prepaid_lock_counts(db: Session, account_ids: list[Any]) -> dict[Any, int]:
    if not account_ids:
        return {}
    return {
        account_id: int(count)
        for account_id, count in db.execute(
            select(Subscription.subscriber_id, func.count(EnforcementLock.id))
            .join(
                EnforcementLock,
                EnforcementLock.subscription_id == Subscription.id,
            )
            .where(
                Subscription.subscriber_id.in_(account_ids),
                EnforcementLock.reason == EnforcementReason.prepaid,
                EnforcementLock.is_active.is_(True),
            )
            .group_by(Subscription.subscriber_id)
        ).all()
    }


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _window_block_reason(
    db: Session,
    *,
    now: datetime,
    policy: PrepaidEnforcementPolicy,
) -> str | None:
    local_now = enforcement_window.to_local(db, now)
    return enforcement_window.window_block_reason(
        local_now,
        start_time=enforcement_window.parse_time(policy.blocking_time),
        skip_weekends=policy.skip_weekends,
        skip_holidays=list(policy.skip_holidays),
    )


def plan_prepaid_account(
    db: Session,
    account: Subscriber,
    *,
    now: datetime,
    policy: PrepaidEnforcementPolicy,
    subscriptions: list[Subscription] | None = None,
    funding: PrepaidFundingDecision | None = None,
    active_prepaid_lock_count: int | None = None,
    dedicated_bundle: bool | None = None,
    shield_reason: str | None = None,
    shield_evaluated: bool = False,
    enforcement_health: EnforcementHealth | None = None,
    notice_suppression_reason: str | None = None,
) -> PrepaidEnforcementPlanItem:
    """Classify one account without mutating it."""
    from app.services.account_lifecycle import derive_account_status

    account_subscriptions = subscriptions
    if account_subscriptions is None:
        account_subscriptions = list(
            db.scalars(
                select(Subscription).where(Subscription.subscriber_id == account.id)
            ).all()
        )
    active_count = sum(
        1 for sub in account_subscriptions if sub.status.value == "active"
    )
    suspended_count = sum(
        1
        for sub in account_subscriptions
        if sub.status.value in {"blocked", "suspended"}
    )
    if active_prepaid_lock_count is None:
        active_prepaid_lock_count = int(
            db.scalar(
                select(func.count(EnforcementLock.id))
                .join(
                    Subscription,
                    Subscription.id == EnforcementLock.subscription_id,
                )
                .where(
                    Subscription.subscriber_id == account.id,
                    EnforcementLock.reason == EnforcementReason.prepaid,
                    EnforcementLock.is_active.is_(True),
                )
            )
            or 0
        )

    profile = resolve_billing_profile(db, account)
    funding = funding or resolve_prepaid_funding(db, account, now=now)
    balance = funding.available_balance
    threshold = funding.required_balance
    derived_status = derive_account_status(db, str(account.id))
    current_status = account.status or SubscriberStatus.new
    due_at = (
        _as_utc(account.prepaid_low_balance_at)
        + timedelta(days=policy.deactivation_days)
        if account.prepaid_low_balance_at is not None
        else None
    )

    action = PrepaidEnforcementAction.ok
    reason = "funded_and_aligned"
    if account.status == SubscriberStatus.canceled:
        action = PrepaidEnforcementAction.not_applicable
        reason = "account_canceled"
    elif not profile.automation_safe and profile.has_collectible_subscriptions:
        action = PrepaidEnforcementAction.billing_profile_invalid
        reason = profile.invalid_reason or "billing_profile_not_automation_safe"
    elif profile.effective_mode != BillingMode.prepaid:
        if (
            account.prepaid_low_balance_at is not None
            or account.prepaid_deactivation_at is not None
        ):
            action = PrepaidEnforcementAction.clear_stale_timers
            reason = "non_prepaid_account_has_prepaid_timers"
        else:
            action = PrepaidEnforcementAction.not_applicable
            reason = "effective_billing_mode_not_prepaid"
    elif balance >= threshold:
        if (
            account.prepaid_low_balance_at is not None
            or account.prepaid_deactivation_at is not None
            or active_prepaid_lock_count > 0
        ):
            action = PrepaidEnforcementAction.restore
            reason = "funding_threshold_reached"
    elif active_prepaid_lock_count > 0 and active_count > 0:
        action = PrepaidEnforcementAction.state_drift
        reason = "active_subscription_has_prepaid_lock"
    elif active_prepaid_lock_count > 0 and account.prepaid_deactivation_at is None:
        action = PrepaidEnforcementAction.state_drift
        reason = "prepaid_lock_missing_deactivation_marker"
    elif account.prepaid_deactivation_at is not None and active_prepaid_lock_count == 0:
        action = PrepaidEnforcementAction.state_drift
        reason = "deactivation_marker_missing_prepaid_lock"
    elif active_prepaid_lock_count > 0:
        action = PrepaidEnforcementAction.already_suspended
        reason = "prepaid_lock_and_deactivation_aligned"
    elif account.prepaid_low_balance_at is None:
        action = PrepaidEnforcementAction.warn
        reason = "low_balance_timer_not_armed"
    elif due_at is not None and now < due_at:
        action = PrepaidEnforcementAction.waiting
        reason = "deactivation_grace_not_elapsed"
    elif window_reason := _window_block_reason(db, now=now, policy=policy):
        action = PrepaidEnforcementAction.deferred
        reason = window_reason
    else:
        if dedicated_bundle is None:
            dedicated_bundle = account.id in _dedicated_bundle_account_ids(
                db, [account.id]
            )
        if dedicated_bundle:
            action = PrepaidEnforcementAction.shielded
            reason = "dedicated_bundle"
        else:
            if not shield_evaluated:
                shield_reason = _bulk_dunning_shield_reasons(db, {account.id}).get(
                    account.id
                )
            if shield_reason:
                action = PrepaidEnforcementAction.shielded
                reason = shield_reason
            else:
                health = enforcement_health or billing_enforcement_health(db)
                if not health.ok:
                    action = PrepaidEnforcementAction.health_blocked
                    reason = ",".join(health.reasons) or "enforcement_health_failed"
                else:
                    action = PrepaidEnforcementAction.suspend
                    reason = "low_balance_deactivation_due"

    return PrepaidEnforcementPlanItem(
        account_id=str(account.id),
        action=action,
        reason=reason,
        account_status=current_status.value,
        derived_account_status=derived_status.value,
        account_status_drift=current_status != derived_status,
        billing_mode=profile.effective_mode.value if profile.effective_mode else None,
        available_balance=balance,
        required_balance=threshold,
        active_subscription_count=active_count,
        suspended_subscription_count=suspended_count,
        active_prepaid_lock_count=active_prepaid_lock_count,
        prepaid_low_balance_at=account.prepaid_low_balance_at,
        deactivation_due_at=due_at,
        prepaid_deactivation_at=account.prepaid_deactivation_at,
        notice_suppression_reason=notice_suppression_reason,
    )


def plan_prepaid_enforcement(
    db: Session,
    *,
    now: datetime | None = None,
    account_ids: list[Any] | None = None,
    limit: int | None = None,
) -> PrepaidEnforcementPlan:
    """Build a deterministic, side-effect-free production readiness report."""
    generated_at = now or datetime.now(UTC)
    raw_ids = (
        list(account_ids)
        if account_ids is not None
        else list(candidate_prepaid_account_ids(db))
    )
    ids = sorted({coerce_uuid(str(value)) for value in raw_ids}, key=str)
    if limit is not None:
        ids = ids[: max(0, limit)]
    accounts = list(
        db.scalars(
            select(Subscriber).where(Subscriber.id.in_(ids)).order_by(Subscriber.id)
        ).all()
    )
    resolved_ids = [account.id for account in accounts]
    subscriptions = list(
        db.scalars(
            select(Subscription).where(Subscription.subscriber_id.in_(resolved_ids))
        ).all()
    )
    subscriptions_by_account: dict[Any, list[Subscription]] = defaultdict(list)
    for subscription in subscriptions:
        subscriptions_by_account[subscription.subscriber_id].append(subscription)

    policy = resolve_prepaid_enforcement_policy(db)
    funding_by_account = {
        account.id: resolve_prepaid_funding(db, account, now=generated_at)
        for account in accounts
    }
    lock_counts = _prepaid_lock_counts(db, resolved_ids)
    dedicated_accounts = _dedicated_bundle_account_ids(db, resolved_ids)
    shield_reasons = _bulk_dunning_shield_reasons(db, set(resolved_ids))
    notice_reasons = prepaid_notice_suppression_reasons(db, resolved_ids)
    health = billing_enforcement_health(db)

    items = tuple(
        plan_prepaid_account(
            db,
            account,
            now=generated_at,
            policy=policy,
            subscriptions=subscriptions_by_account.get(account.id, []),
            funding=funding_by_account[account.id],
            active_prepaid_lock_count=lock_counts.get(account.id, 0),
            dedicated_bundle=account.id in dedicated_accounts,
            shield_reason=shield_reasons.get(account.id),
            shield_evaluated=True,
            enforcement_health=health,
            notice_suppression_reason=notice_reasons.get(account.id),
        )
        for account in accounts
    )
    return PrepaidEnforcementPlan(
        generated_at=generated_at,
        control_enabled=prepaid_balance_enforcement_enabled(db),
        policy=policy,
        items=items,
    )
