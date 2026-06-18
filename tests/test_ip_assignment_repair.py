"""Tests for step-2a IPAM-to-served repair.

Repairs the IPAM ledger to match the served IPv4; never changes a served IP;
refuses conflicts. See docs/designs/SERVICE_LIFECYCLE_BUNDLE_INTEGRITY.md §5b.
"""

from __future__ import annotations

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IpPool, IPv4Address, IPVersion
from app.models.subscriber import Subscriber
from app.services.ip_assignment_repair import apply_repair, plan_repair


def _mk_subscriber(db, email):
    s = Subscriber(first_name="R", last_name="C", email=email)
    db.add(s)
    db.flush()
    return s


def _mk_sub(db, subscriber, offer, served_ip, status=SubscriptionStatus.active):
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        login=f"login-{subscriber.email.split('@')[0]}",
        ipv4_address=served_ip,
    )
    db.add(sub)
    db.flush()
    return sub


def _mk_assignment(
    db, subscriber, ip, *, active=True, allocation_type=None, ont_unit_id=None
):
    addr = db.query(IPv4Address).filter(IPv4Address.address == ip).first()
    if addr is None:
        addr = IPv4Address(
            address=ip, allocation_type=allocation_type, ont_unit_id=ont_unit_id
        )
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
    return a, addr


def _action_for(plan, subscriber):
    for it in plan["items"]:
        if it["subscriber_id"] == str(subscriber.id):
            return it["action"]
    return None


class TestPlan:
    def test_noop_when_ledger_matches(self, db_session, catalog_offer):
        s = _mk_subscriber(db_session, "noop@e.com")
        _mk_sub(db_session, s, catalog_offer, "10.0.0.5")
        _mk_assignment(db_session, s, "10.0.0.5")
        db_session.commit()
        plan = plan_repair(db_session)
        assert _action_for(plan, s) == "noop_already_correct"

    def test_backfill_when_no_assignment(self, db_session, catalog_offer):
        s = _mk_subscriber(db_session, "missing@e.com")
        _mk_sub(db_session, s, catalog_offer, "10.0.0.6")
        db_session.commit()
        plan = plan_repair(db_session)
        assert _action_for(plan, s) == "backfill_create"

    def test_repoint_when_assignment_differs(self, db_session, catalog_offer):
        s = _mk_subscriber(db_session, "mismatch@e.com")
        _mk_sub(db_session, s, catalog_offer, "10.0.0.7")
        _mk_assignment(db_session, s, "10.0.0.99")  # stale IPAM
        db_session.commit()
        plan = plan_repair(db_session)
        assert _action_for(plan, s) == "repoint"

    def test_conflict_live_contention(self, db_session, catalog_offer):
        """Two subscribers actually served the same IP — a real clash, refused."""
        owner = _mk_subscriber(db_session, "owner@e.com")
        _mk_sub(db_session, owner, catalog_offer, "10.0.0.8")
        _mk_assignment(db_session, owner, "10.0.0.8")  # owner holds AND is served it
        other = _mk_subscriber(db_session, "intruder@e.com")
        _mk_sub(db_session, other, catalog_offer, "10.0.0.8")  # also served same IP
        db_session.commit()
        plan = plan_repair(db_session)
        assert _action_for(plan, other) == "conflict_live_contention"

    def test_reclaim_stale_when_owner_not_served_it(self, db_session, catalog_offer):
        """Address held in IPAM by a subscriber who is now served a DIFFERENT IP
        (asymmetric-release bug) → safely reclaimable by the real served owner."""
        owner = _mk_subscriber(db_session, "staleowner@e.com")
        _mk_sub(db_session, owner, catalog_offer, "10.0.0.50")  # owner moved on
        _mk_assignment(db_session, owner, "10.0.0.8")  # stale IPAM row for .8
        other = _mk_subscriber(db_session, "realowner@e.com")
        _mk_sub(db_session, other, catalog_offer, "10.0.0.8")  # actually served .8
        db_session.commit()
        plan = plan_repair(db_session)
        assert _action_for(plan, other) == "reclaim_stale"

    def test_conflict_addr_reserved(self, db_session, catalog_offer):
        s = _mk_subscriber(db_session, "reserved@e.com")
        _mk_sub(db_session, s, catalog_offer, "10.9.9.9")
        # served IP exists as a management address
        db_session.add(IPv4Address(address="10.9.9.9", allocation_type="management"))
        db_session.commit()
        plan = plan_repair(db_session)
        assert _action_for(plan, s) == "conflict_addr_reserved"

    def test_conflict_ambiguous_multi_active(self, db_session, catalog_offer):
        s = _mk_subscriber(db_session, "ambig@e.com")
        _mk_sub(db_session, s, catalog_offer, "10.0.0.10")
        _mk_sub(db_session, s, catalog_offer, "10.0.0.11")  # second active, diff IP
        db_session.commit()
        plan = plan_repair(db_session)
        assert _action_for(plan, s) == "conflict_ambiguous_multi_active"


class TestApply:
    def test_backfill_creates_assignment(self, db_session, catalog_offer):
        s = _mk_subscriber(db_session, "bf@e.com")
        _mk_sub(db_session, s, catalog_offer, "10.0.0.20")
        db_session.commit()
        plan = plan_repair(db_session)
        result = apply_repair(db_session, plan)
        assert result["backfill_create"] == 1
        a = (
            db_session.query(IPAssignment)
            .filter(
                IPAssignment.subscriber_id == s.id, IPAssignment.is_active.is_(True)
            )
            .one()
        )
        assert a.ipv4_address.address == "10.0.0.20"

    def test_repoint_deactivates_stale_and_activates_served(
        self, db_session, catalog_offer
    ):
        s = _mk_subscriber(db_session, "rp@e.com")
        _mk_sub(db_session, s, catalog_offer, "10.0.0.21")
        stale, _ = _mk_assignment(db_session, s, "10.0.0.222")
        db_session.commit()
        plan = plan_repair(db_session)
        result = apply_repair(db_session, plan)
        assert result["repoint"] == 1
        db_session.refresh(stale)
        assert stale.is_active is False
        active = (
            db_session.query(IPAssignment)
            .filter(
                IPAssignment.subscriber_id == s.id, IPAssignment.is_active.is_(True)
            )
            .one()
        )
        assert active.ipv4_address.address == "10.0.0.21"

    def test_backfill_matches_pool_for_gateway(self, db_session, catalog_offer):
        db_session.add(
            IpPool(
                name="p1",
                ip_version=IPVersion.ipv4,
                cidr="10.0.0.0/24",
                gateway="10.0.0.1",
                dns_primary="8.8.8.8",
                is_active=True,
            )
        )
        s = _mk_subscriber(db_session, "pool@e.com")
        _mk_sub(db_session, s, catalog_offer, "10.0.0.30")
        db_session.commit()
        plan = plan_repair(db_session)
        apply_repair(db_session, plan)
        a = (
            db_session.query(IPAssignment)
            .filter(
                IPAssignment.subscriber_id == s.id, IPAssignment.is_active.is_(True)
            )
            .one()
        )
        assert a.gateway == "10.0.0.1"
        assert a.prefix_length == 24

    def test_apply_is_idempotent(self, db_session, catalog_offer):
        s = _mk_subscriber(db_session, "idem@e.com")
        _mk_sub(db_session, s, catalog_offer, "10.0.0.40")
        db_session.commit()
        apply_repair(db_session, plan_repair(db_session))
        # second run: now already correct → nothing actionable
        plan2 = plan_repair(db_session)
        assert _action_for(plan2, s) == "noop_already_correct"
        result2 = apply_repair(db_session, plan2)
        assert result2["backfill_create"] == 0
        assert result2["repoint"] == 0

    def test_reclaim_repoints_stale_row_to_real_owner(self, db_session, catalog_offer):
        owner = _mk_subscriber(db_session, "stale2@e.com")
        _mk_sub(db_session, owner, catalog_offer, "10.0.0.51")
        stale, addr = _mk_assignment(db_session, owner, "10.0.0.60")
        other = _mk_subscriber(db_session, "real2@e.com")
        _mk_sub(db_session, other, catalog_offer, "10.0.0.60")
        db_session.commit()
        plan = plan_repair(db_session)
        result = apply_repair(db_session, plan)
        assert result["reclaim_stale"] == 1
        db_session.refresh(stale)
        # the single address row was repointed to the real served owner
        assert str(stale.subscriber_id) == str(other.id)
        assert stale.is_active is True

    def test_live_contention_not_applied(self, db_session, catalog_offer):
        owner = _mk_subscriber(db_session, "live1@e.com")
        _mk_sub(db_session, owner, catalog_offer, "10.0.0.70")
        _mk_assignment(db_session, owner, "10.0.0.70")
        other = _mk_subscriber(db_session, "live2@e.com")
        _mk_sub(db_session, other, catalog_offer, "10.0.0.70")
        db_session.commit()
        plan = plan_repair(db_session)
        result = apply_repair(db_session, plan)
        assert result["backfill_create"] == 0
        assert result["repoint"] == 0
        assert result["reclaim_stale"] == 0

    def test_dedupe_active_keeps_served_drops_extras(self, db_session, catalog_offer):
        s = _mk_subscriber(db_session, "dup@e.com")
        _mk_sub(db_session, s, catalog_offer, "10.0.0.80")
        served_a, _ = _mk_assignment(db_session, s, "10.0.0.80")  # correct served
        stale_a, _ = _mk_assignment(db_session, s, "10.0.0.81")  # stale extra, active
        db_session.commit()
        plan = plan_repair(db_session)
        assert _action_for(plan, s) == "dedupe_active"
        result = apply_repair(db_session, plan)
        assert result["dedupe_active"] == 1
        db_session.refresh(served_a)
        db_session.refresh(stale_a)
        assert served_a.is_active is True  # served-matching kept
        assert stale_a.is_active is False  # extra deactivated

    def test_limit_caps_repairs(self, db_session, catalog_offer):
        for i in range(3):
            s = _mk_subscriber(db_session, f"lim{i}@e.com")
            _mk_sub(db_session, s, catalog_offer, f"10.0.1.{i + 1}")
        db_session.commit()
        plan = plan_repair(db_session)
        result = apply_repair(db_session, plan, limit=2)
        assert result["subscribers_repaired"] == 2
