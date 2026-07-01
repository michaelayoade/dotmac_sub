"""The collectible-service gate keeps ``blocked`` (recoverable non-payment) in
scope for collections, distinct from the narrower live-service gate.

Regression guard for the 2026-06-26 collections leak: the live-service gate
excluded ``blocked``, so once enforcement walled a non-payer, reminders/dunning/
autopay stopped chasing them entirely.
"""

import pytest

from app.models.catalog import SubscriptionStatus
from app.services import billing_settings


@pytest.mark.parametrize(
    "status, collectible, live",
    [
        (SubscriptionStatus.active, True, True),
        (SubscriptionStatus.suspended, True, True),
        (SubscriptionStatus.pending, True, True),
        # The fix: blocked is collectible but NOT "live".
        (SubscriptionStatus.blocked, True, False),
        # Truly-terminal — excluded from both.
        (SubscriptionStatus.stopped, False, False),
        (SubscriptionStatus.disabled, False, False),
        (SubscriptionStatus.canceled, False, False),
        (SubscriptionStatus.expired, False, False),
    ],
)
def test_collectible_vs_live_gate(
    db_session, subscriber, subscription, status, collectible, live
):
    subscription.status = status
    db_session.add(subscription)
    db_session.commit()

    assert (
        billing_settings.account_has_collectible_service(db_session, subscriber.id)
        is collectible
    )
    assert billing_settings.account_has_live_service(db_session, subscriber.id) is live
    in_set = subscriber.id in billing_settings.accounts_with_collectible_service(
        db_session
    )
    assert in_set is collectible


def test_blocked_in_collectible_not_in_live_constant():
    assert SubscriptionStatus.blocked in billing_settings.COLLECTIBLE_SERVICE_STATUSES
    assert SubscriptionStatus.blocked not in billing_settings.LIVE_SERVICE_STATUSES
    # Truly-terminal stay out of the collectible set.
    for terminal in (
        SubscriptionStatus.disabled,
        SubscriptionStatus.canceled,
        SubscriptionStatus.stopped,
        SubscriptionStatus.expired,
    ):
        assert terminal not in billing_settings.COLLECTIBLE_SERVICE_STATUSES
