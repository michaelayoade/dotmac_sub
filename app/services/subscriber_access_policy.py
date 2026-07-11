"""Shared subscriber account-state policy for network access.

Subscription rows carry service state, but parent subscriber state can be more
restrictive. Keep this policy in one place so RADIUS population, reconciliation,
health metrics, and billing/access resolution do not drift.
"""

from __future__ import annotations

from app.models.subscriber import SubscriberStatus

# Parent account states that must not receive normal RADIUS service even when a
# child subscription row is stale-active. Delinquent is intentionally excluded:
# it is a pre-suspension state where service may still be running.
RADIUS_BLOCKING_SUBSCRIBER_STATUSES = frozenset(
    {
        SubscriberStatus.blocked,
        SubscriberStatus.suspended,
        SubscriberStatus.disabled,
        SubscriberStatus.canceled,
        SubscriberStatus.new,
    }
)
