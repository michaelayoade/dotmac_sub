"""Tests for the authoritative single-writer radreply builder."""

import types

from app.models.catalog import SubscriptionStatus
from scripts.migration.populate_radius_from_subs import _radreply_attrs


def _sub(ipv4="10.0.0.5", status=SubscriptionStatus.active):
    return types.SimpleNamespace(
        ipv4_address=ipv4, status=status, subscriber_id="subscriber-x"
    )


def _routes(attrs):
    return [a for a in attrs if a[0] == "Framed-Route"]


class TestRadreplyAdditionalRoutes:
    """_radreply_attrs must emit Framed-Route for the single-writer sweep, or
    the periodic refresh wipes routes written elsewhere."""

    def test_active_emits_one_framed_route_per_block(self):
        attrs = _radreply_attrs(
            _sub(),
            offer=None,
            profile=None,
            subscriber_blocked=False,
            additional_routes=[("203.0.113.8/29", 1), ("198.51.100.0/30", 2)],
        )
        routes = _routes(attrs)
        assert {a[2] for a in routes} == {
            "203.0.113.8/29 0.0.0.0 1",
            "198.51.100.0/30 0.0.0.0 2",
        }
        assert all(a[1] == "+=" for a in routes)

    def test_blocked_subscriber_walled_garden_no_routes(self):
        attrs = _radreply_attrs(
            _sub(),
            None,
            None,
            subscriber_blocked=True,
            additional_routes=[("203.0.113.8/29", 1)],
        )
        assert _routes(attrs) == []
        assert any(a[0] == "Mikrotik-Address-List" for a in attrs)

    def test_suspended_subscription_no_routes(self):
        attrs = _radreply_attrs(
            _sub(status=SubscriptionStatus.suspended),
            None,
            None,
            additional_routes=[("203.0.113.8/29", 1)],
        )
        assert _routes(attrs) == []

    def test_primary_host_route_skipped(self):
        attrs = _radreply_attrs(
            _sub(ipv4="203.0.113.5"),
            None,
            None,
            additional_routes=[("203.0.113.5/32", 1), ("203.0.113.8/29", 1)],
        )
        routes = _routes(attrs)
        assert len(routes) == 1
        assert routes[0][2] == "203.0.113.8/29 0.0.0.0 1"

    def test_metric_defaults_to_one(self):
        attrs = _radreply_attrs(
            _sub(), None, None, additional_routes=[("203.0.113.8/29", None)]
        )
        assert ("Framed-Route", "+=", "203.0.113.8/29 0.0.0.0 1") in attrs

    def test_no_routes_no_framed_route(self):
        attrs = _radreply_attrs(_sub(), None, None, additional_routes=None)
        assert _routes(attrs) == []
