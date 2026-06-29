"""Canonical billing policy constants shared across billing services."""

from __future__ import annotations

from app.models.subscriber import SubscriberStatus

BILLABLE_SUBSCRIBER_STATUSES = (
    SubscriberStatus.active,
    SubscriberStatus.blocked,
    SubscriberStatus.suspended,
    SubscriberStatus.delinquent,
)

BILLABLE_SUBSCRIBER_STATUS_VALUES = tuple(
    status.value for status in BILLABLE_SUBSCRIBER_STATUSES
)
