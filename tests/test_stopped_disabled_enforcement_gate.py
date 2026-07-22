"""Permanent stopped/disabled connectivity enforcement."""

from __future__ import annotations

from unittest.mock import patch

from app.models.catalog import Subscription, SubscriptionStatus
from app.services.catalog.subscriptions import (
    _enforce_stopped_or_disabled_connectivity,
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


def test_stopped_enforces_like_suspend(db_session, subscriber, catalog_offer):
    sub = _sub(db_session, subscriber, catalog_offer)
    with patch(
        "app.services.enforcement.cleanup_subscription_on_suspend"
    ) as mock_suspend:
        _enforce_stopped_or_disabled_connectivity(
            db_session, sub, SubscriptionStatus.stopped
        )
    mock_suspend.assert_called_once()


def test_disabled_enforces_like_cancel(db_session, subscriber, catalog_offer):
    sub = _sub(db_session, subscriber, catalog_offer)
    with patch(
        "app.services.enforcement.cleanup_subscription_on_cancel"
    ) as mock_cancel:
        _enforce_stopped_or_disabled_connectivity(
            db_session, sub, SubscriptionStatus.disabled
        )
    mock_cancel.assert_called_once()
