"""radius_population._radreply_attrs honors per-customer captive opt-in."""

from __future__ import annotations

from types import SimpleNamespace

from app.models.catalog import SubscriptionStatus
from app.services.radius_population import (
    SUSPENDED_ADDRESS_LIST,
    _radreply_attrs,
)


def _sub(status):
    return SimpleNamespace(status=status, ipv4_address=None)


def _addrlist(attrs):
    return [v for (a, _o, v) in attrs if a == "Mikrotik-Address-List"]


def test_blocked_opt_in_gets_captive_address_list():
    attrs = _radreply_attrs(
        _sub(SubscriptionStatus.suspended),
        None,
        None,
        subscriber_blocked=True,
        captive_redirect_enabled=True,
    )
    assert _addrlist(attrs) == [SUSPENDED_ADDRESS_LIST]


def test_blocked_not_opted_in_gets_no_address_list():
    # Not opted in → no captive radreply (the user is hard-rejected in radcheck).
    attrs = _radreply_attrs(
        _sub(SubscriptionStatus.suspended),
        None,
        None,
        subscriber_blocked=True,
        captive_redirect_enabled=False,
    )
    assert _addrlist(attrs) == []


def test_active_never_gets_address_list_regardless_of_flag():
    attrs = _radreply_attrs(
        _sub(SubscriptionStatus.active),
        None,
        None,
        subscriber_blocked=False,
        captive_redirect_enabled=True,
    )
    assert _addrlist(attrs) == []
