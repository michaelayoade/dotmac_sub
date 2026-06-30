from app.models.subscriber import SubscriberStatus
from app.services.billing_statuses import (
    BILLABLE_SUBSCRIBER_STATUS_SQL,
    BILLABLE_SUBSCRIBER_STATUS_VALUES,
    BILLABLE_SUBSCRIBER_STATUSES,
)


def test_billable_subscriber_statuses_are_canonical():
    assert BILLABLE_SUBSCRIBER_STATUSES == (
        SubscriberStatus.active,
        SubscriberStatus.blocked,
        SubscriberStatus.suspended,
        SubscriberStatus.delinquent,
    )
    assert BILLABLE_SUBSCRIBER_STATUS_VALUES == (
        "active",
        "blocked",
        "suspended",
        "delinquent",
    )
    assert BILLABLE_SUBSCRIBER_STATUS_SQL == (
        "'active', 'blocked', 'suspended', 'delinquent'"
    )
