"""populate_radius_from_subs._radreply_attrs honors the per-customer captive
opt-in: only opted-in blocked subscribers get the Mikrotik-Address-List
walled-garden attribute."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from app.models.catalog import SubscriptionStatus

_spec = importlib.util.spec_from_file_location(
    "populate_radius_from_subs",
    Path(__file__).resolve().parents[1]
    / "scripts/migration/populate_radius_from_subs.py",
)
_pop = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pop)


def _sub(status):
    return SimpleNamespace(status=status, ipv4_address=None)


def _addrlist(attrs):
    return [v for (a, _o, v) in attrs if a == "Mikrotik-Address-List"]


def test_blocked_opt_in_gets_captive_address_list():
    attrs = _pop._radreply_attrs(
        _sub(SubscriptionStatus.suspended),
        None,
        None,
        subscriber_blocked=True,
        captive_redirect_enabled=True,
    )
    assert _addrlist(attrs) == [_pop.SUSPENDED_ADDRESS_LIST]


def test_blocked_not_opted_in_gets_no_address_list():
    # Not opted in → no captive radreply (the user is hard-rejected in radcheck).
    attrs = _pop._radreply_attrs(
        _sub(SubscriptionStatus.suspended),
        None,
        None,
        subscriber_blocked=True,
        captive_redirect_enabled=False,
    )
    assert _addrlist(attrs) == []


def test_active_never_gets_address_list_regardless_of_flag():
    attrs = _pop._radreply_attrs(
        _sub(SubscriptionStatus.active),
        None,
        None,
        subscriber_blocked=False,
        captive_redirect_enabled=True,
    )
    assert _addrlist(attrs) == []
