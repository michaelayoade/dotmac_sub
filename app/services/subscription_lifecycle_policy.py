"""Shared subscription lifecycle policy predicates.

Different workflows need different meanings of "active": customer-impact,
portal visibility, billing collection, RADIUS projection, and reporting are not
the same rule. This module names those rules so callers do not re-invent status
sets inline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import and_, not_
from sqlalchemy.sql.elements import ColumnElement

from app.models.catalog import Subscription, SubscriptionStatus
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.radius_access_state import (
    ACTIVE_STATUSES as RADIUS_ACTIVE_STATUSES,
)
from app.services.radius_access_state import (
    BLOCKED_STATUSES as RADIUS_BLOCKED_STATUSES,
)
from app.services.radius_access_state import (
    TERMINATED_STATUSES as RADIUS_TERMINATED_STATUSES,
)

CUSTOMER_IMPACT_STATUSES = frozenset({SubscriptionStatus.active})
PORTAL_VISIBLE_SERVICE_STATUSES = frozenset(
    {
        SubscriptionStatus.pending,
        SubscriptionStatus.active,
        SubscriptionStatus.blocked,
        SubscriptionStatus.suspended,
        SubscriptionStatus.stopped,
        SubscriptionStatus.disabled,
    }
)
# A disabled or stopped row can still describe a current service (for example,
# while support is resolving a real administrative hold). It becomes historical
# for customer-health projections only when its explicit end instant has passed.
HISTORICAL_WHEN_ENDED_SERVICE_STATUSES = frozenset(
    {
        SubscriptionStatus.disabled,
        SubscriptionStatus.stopped,
    }
)
TERMINAL_SERVICE_STATUSES = frozenset(
    {
        SubscriptionStatus.expired,
        SubscriptionStatus.canceled,
        SubscriptionStatus.archived,
        SubscriptionStatus.hidden,
    }
)
BILLING_COLLECTIBLE_SERVICE_STATUSES = frozenset(COLLECTIBLE_SERVICE_STATUSES)
RADIUS_PROJECTABLE_SERVICE_STATUSES = frozenset(
    RADIUS_ACTIVE_STATUSES | RADIUS_BLOCKED_STATUSES
)
MRR_COUNTABLE_SERVICE_STATUSES = frozenset({SubscriptionStatus.active})
NO_NORMAL_ACCESS_SERVICE_STATUSES = frozenset(
    RADIUS_BLOCKED_STATUSES | RADIUS_TERMINATED_STATUSES
)


class OperationalSubscriptionCohortReason(StrEnum):
    """Why a subscription is included in or excluded from customer health."""

    operationally_current = "operationally_current"
    historical_explicit_end = "historical_explicit_end"
    terminal_lifecycle = "terminal_lifecycle"


@dataclass(frozen=True, slots=True)
class OperationalSubscriptionCohortInput:
    """Typed authoritative inputs for one operational-cohort decision."""

    status: SubscriptionStatus
    end_at: datetime | None
    as_of: datetime


@dataclass(frozen=True, slots=True)
class OperationalSubscriptionCohortDecision:
    """Deterministic customer-health cohort classification."""

    is_operationally_current: bool
    reason: OperationalSubscriptionCohortReason


def require_aware_utc(value: datetime, *, field_name: str = "as_of") -> datetime:
    """Return one UTC instant, rejecting ambiguous naive policy timestamps."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def classify_operational_subscription(
    policy_input: OperationalSubscriptionCohortInput,
) -> OperationalSubscriptionCohortDecision:
    """Classify one subscription for customer-visible operational health.

    Explicit end dates are not a general lifecycle authority. They only stop a
    disabled or stopped row from overriding a healthy replacement after that
    end instant has passed. Other lifecycle drift remains visible for review.
    """

    as_of = require_aware_utc(policy_input.as_of)
    if policy_input.status not in PORTAL_VISIBLE_SERVICE_STATUSES:
        return OperationalSubscriptionCohortDecision(
            is_operationally_current=False,
            reason=OperationalSubscriptionCohortReason.terminal_lifecycle,
        )
    if (
        policy_input.status in HISTORICAL_WHEN_ENDED_SERVICE_STATUSES
        and policy_input.end_at is not None
        and require_aware_utc(policy_input.end_at, field_name="end_at") <= as_of
    ):
        return OperationalSubscriptionCohortDecision(
            is_operationally_current=False,
            reason=OperationalSubscriptionCohortReason.historical_explicit_end,
        )
    return OperationalSubscriptionCohortDecision(
        is_operationally_current=True,
        reason=OperationalSubscriptionCohortReason.operationally_current,
    )


def operationally_current_subscription_filters(
    *, as_of: datetime
) -> tuple[ColumnElement[bool], ...]:
    """SQL predicates for the exact customer-health subscription cohort."""

    evaluated_at = require_aware_utc(as_of)
    return (
        Subscription.status.in_(PORTAL_VISIBLE_SERVICE_STATUSES),
        not_(
            and_(
                Subscription.status.in_(HISTORICAL_WHEN_ENDED_SERVICE_STATUSES),
                Subscription.end_at.is_not(None),
                Subscription.end_at <= evaluated_at,
            )
        ),
    )


def customer_impact_service_filters(subscription_model) -> tuple:
    return (subscription_model.status.in_(CUSTOMER_IMPACT_STATUSES),)


def portal_visible_service_filters(subscription_model) -> tuple:
    return (subscription_model.status.in_(PORTAL_VISIBLE_SERVICE_STATUSES),)


def billing_collectible_service_filters(subscription_model) -> tuple:
    return (subscription_model.status.in_(BILLING_COLLECTIBLE_SERVICE_STATUSES),)


def radius_projectable_service_filters(subscription_model) -> tuple:
    return (subscription_model.status.in_(RADIUS_PROJECTABLE_SERVICE_STATUSES),)


def mrr_countable_service_filters(subscription_model) -> tuple:
    return (subscription_model.status.in_(MRR_COUNTABLE_SERVICE_STATUSES),)


def is_customer_impact_service_status(status: SubscriptionStatus | None) -> bool:
    return status in CUSTOMER_IMPACT_STATUSES


def is_mrr_countable_service_status(status: SubscriptionStatus | None) -> bool:
    return status in MRR_COUNTABLE_SERVICE_STATUSES


def is_terminal_service_status(status: SubscriptionStatus | None) -> bool:
    return status in TERMINAL_SERVICE_STATUSES
