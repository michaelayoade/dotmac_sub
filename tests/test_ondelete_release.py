"""Retired hard-delete writer must never return; migrated PG tests own behavior."""

from app.services import subscriber
from app.services.subscriber import Accounts, Subscribers


def test_legacy_parent_and_network_writers_retired():
    assert not hasattr(Subscribers, "delete")
    assert not hasattr(Accounts, "delete")
    assert not hasattr(subscriber, "_release_subscriber_network_records")
