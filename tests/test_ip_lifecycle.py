"""Tests for the service-IP lifecycle forward fix + backlog planner.

Invariant: active service owns service IPs; terminal service does not. See
docs/POST_CUTOVER_HARDENING.md.
"""

from __future__ import annotations

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.models.subscriber import Subscriber
from app.services.ip_lifecycle import (
    plan_terminal_ip_backlog,
    release_service_ips_for_subscription,
)


def _subscriber(db, email):
    s = Subscriber(first_name="L", last_name="C", email=email)
    db.add(s)
    db.flush()
    return s


def _sub(db, subscriber, offer, *, status=SubscriptionStatus.active, ipv4=None):
    sub = Subscription(
        subscriber_id=subscriber.id, offer_id=offer.id, status=status, ipv4_address=ipv4
    )
    db.add(sub)
    db.flush()
    return sub


def _assign(db, subscriber, ip, *, active=True, allocation_type=None):
    addr = IPv4Address(address=ip, allocation_type=allocation_type)
    db.add(addr)
    db.flush()
    a = IPAssignment(
        subscriber_id=subscriber.id,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=addr.id,
        is_active=active,
    )
    db.add(a)
    db.flush()
    return a


class TestForwardFix:
    def test_release_on_terminal(self, db_session, catalog_offer):
        s = _subscriber(db_session, "t1@e.com")
        sub = _sub(
            db_session,
            s,
            catalog_offer,
            status=SubscriptionStatus.canceled,
            ipv4="10.0.0.1",
        )
        a = _assign(db_session, s, "10.0.0.1")
        db_session.commit()
        res = release_service_ips_for_subscription(db_session, sub)
        assert res["released"] == 1
        db_session.refresh(a)
        db_session.refresh(sub)
        assert a.is_active is False
        assert sub.ipv4_address is None

    def test_guard_when_subscriber_has_active_sub(self, db_session, catalog_offer):
        s = _subscriber(db_session, "t2@e.com")
        canceled = _sub(
            db_session, s, catalog_offer, status=SubscriptionStatus.canceled
        )
        _sub(
            db_session, s, catalog_offer, status=SubscriptionStatus.active
        )  # still active
        a = _assign(db_session, s, "10.0.0.2")
        db_session.commit()
        res = release_service_ips_for_subscription(db_session, canceled)
        assert res["skipped"] == "subscriber_has_active_service"
        db_session.refresh(a)
        assert a.is_active is True  # untouched

    def test_noop_when_not_terminal(self, db_session, catalog_offer):
        s = _subscriber(db_session, "t3@e.com")
        sub = _sub(db_session, s, catalog_offer, status=SubscriptionStatus.active)
        a = _assign(db_session, s, "10.0.0.3")
        db_session.commit()
        res = release_service_ips_for_subscription(db_session, sub)
        assert res["skipped"] == "not_terminal"
        db_session.refresh(a)
        assert a.is_active is True

    def test_management_ip_not_released(self, db_session, catalog_offer):
        s = _subscriber(db_session, "t4@e.com")
        sub = _sub(db_session, s, catalog_offer, status=SubscriptionStatus.disabled)
        a = _assign(db_session, s, "10.9.9.9", allocation_type="management")
        db_session.commit()
        res = release_service_ips_for_subscription(db_session, sub)
        assert res["released"] == 0
        assert res["reserved_skipped"] == 1
        db_session.refresh(a)
        assert a.is_active is True

    def test_idempotent(self, db_session, catalog_offer):
        s = _subscriber(db_session, "t5@e.com")
        sub = _sub(db_session, s, catalog_offer, status=SubscriptionStatus.expired)
        _assign(db_session, s, "10.0.0.5")
        db_session.commit()
        assert release_service_ips_for_subscription(db_session, sub)["released"] == 1
        assert release_service_ips_for_subscription(db_session, sub)["released"] == 0


class TestBacklogPlanner:
    def test_terminal_service_ip_is_safe_release(self, db_session, catalog_offer):
        s = _subscriber(db_session, "p1@e.com")
        _sub(db_session, s, catalog_offer, status=SubscriptionStatus.canceled)
        _assign(db_session, s, "10.1.0.1")
        db_session.commit()
        plan = plan_terminal_ip_backlog(db_session)
        assert plan["counts"]["safe_release_terminal"] == 1

    def test_management_ip_is_conflict(self, db_session, catalog_offer):
        s = _subscriber(db_session, "p2@e.com")
        _sub(db_session, s, catalog_offer, status=SubscriptionStatus.canceled)
        _assign(db_session, s, "10.9.9.8", allocation_type="management")
        db_session.commit()
        plan = plan_terminal_ip_backlog(db_session)
        assert plan["counts"]["conflict_management_or_ont"] == 1

    def test_active_elsewhere_is_conflict(self, db_session, catalog_offer):
        # terminal holder squats an IP that an active subscriber serves
        active_owner = _subscriber(db_session, "p3a@e.com")
        _sub(
            db_session,
            active_owner,
            catalog_offer,
            status=SubscriptionStatus.active,
            ipv4="10.1.0.3",
        )
        terminal_holder = _subscriber(db_session, "p3b@e.com")
        _sub(
            db_session,
            terminal_holder,
            catalog_offer,
            status=SubscriptionStatus.canceled,
        )
        _assign(db_session, terminal_holder, "10.1.0.3")
        db_session.commit()
        plan = plan_terminal_ip_backlog(db_session)
        assert plan["counts"]["conflict_active_service"] == 1

    def test_duplicate_extra_is_dedupe(self, db_session, catalog_offer):
        s = _subscriber(db_session, "p4@e.com")
        _sub(
            db_session,
            s,
            catalog_offer,
            status=SubscriptionStatus.active,
            ipv4="10.1.0.4",
        )  # served = .4
        _assign(db_session, s, "10.1.0.4")  # the served one
        _assign(db_session, s, "10.1.0.99")  # stale extra
        db_session.commit()
        plan = plan_terminal_ip_backlog(db_session)
        assert plan["counts"]["safe_dedupe_duplicate"] == 1  # the .99 extra
        assert plan["counts"]["manual_review"] == 1  # the served .4 (keep)

    def test_single_active_not_in_plan(self, db_session, catalog_offer):
        s = _subscriber(db_session, "p5@e.com")
        _sub(
            db_session,
            s,
            catalog_offer,
            status=SubscriptionStatus.active,
            ipv4="10.1.0.5",
        )
        _assign(db_session, s, "10.1.0.5")
        db_session.commit()
        plan = plan_terminal_ip_backlog(db_session)
        assert all(v == 0 for v in plan["counts"].values())
