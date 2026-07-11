"""Shared subscription lifecycle policy predicates.

Different workflows need different meanings of "active": customer-impact,
portal visibility, billing collection, RADIUS projection, and reporting are not
the same rule. This module names those rules so callers do not re-invent status
sets inline.
"""

from __future__ import annotations

from app.models.catalog import SubscriptionStatus
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
    }
)
TERMINAL_SERVICE_STATUSES = frozenset(
    {
        SubscriptionStatus.expired,
        SubscriptionStatus.canceled,
        SubscriptionStatus.disabled,
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


def is_terminal_service_status(status: SubscriptionStatus | None) -> bool:
    return status in TERMINAL_SERVICE_STATUSES
