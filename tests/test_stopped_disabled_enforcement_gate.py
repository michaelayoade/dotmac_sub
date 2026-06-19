"""Gated stopped/disabled connectivity enforcement (review task #14).

Default OFF: a transition to stopped/disabled records the would-be disconnect
but does NOT touch RADIUS. When the radius `enforce_stopped_disabled` flag is
on, it runs the same cleanup as suspend/cancel.
"""

from __future__ import annotations

from unittest.mock import patch

from app.models.catalog import Subscription, SubscriptionStatus
from app.services.catalog.subscriptions import (
    _enforce_or_record_connectivity_gap,
)


def _sub(db, subscriber, catalog_offer):
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.stopped,
    )
    db.add(sub)
    db.flush()
    return sub


def test_flag_off_does_not_enforce(db_session, subscriber, catalog_offer):
    sub = _sub(db_session, subscriber, catalog_offer)
    with (
        patch(
            "app.services.catalog.subscriptions.settings_spec.resolve_value",
            return_value=None,  # flag unset → off
        ),
        patch(
            "app.services.enforcement.cleanup_subscription_on_suspend"
        ) as mock_suspend,
        patch("app.services.enforcement.cleanup_subscription_on_cancel") as mock_cancel,
    ):
        _enforce_or_record_connectivity_gap(db_session, sub, SubscriptionStatus.stopped)
    mock_suspend.assert_not_called()
    mock_cancel.assert_not_called()


def test_flag_on_enforces_stopped_like_suspend(db_session, subscriber, catalog_offer):
    sub = _sub(db_session, subscriber, catalog_offer)
    with (
        patch(
            "app.services.catalog.subscriptions.settings_spec.resolve_value",
            return_value="true",
        ),
        patch(
            "app.services.enforcement.cleanup_subscription_on_suspend"
        ) as mock_suspend,
    ):
        _enforce_or_record_connectivity_gap(db_session, sub, SubscriptionStatus.stopped)
    mock_suspend.assert_called_once()


def test_flag_on_enforces_disabled_like_cancel(db_session, subscriber, catalog_offer):
    sub = _sub(db_session, subscriber, catalog_offer)
    with (
        patch(
            "app.services.catalog.subscriptions.settings_spec.resolve_value",
            return_value="true",
        ),
        patch("app.services.enforcement.cleanup_subscription_on_cancel") as mock_cancel,
    ):
        _enforce_or_record_connectivity_gap(
            db_session, sub, SubscriptionStatus.disabled
        )
    mock_cancel.assert_called_once()
