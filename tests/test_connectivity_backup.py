"""Tests for the pre-change connectivity backup (capture + restore).

RADIUS rows are exercised with ``include_radius=False`` (the local DB half) so
the tests need no RADIUS database; the RADIUS read/write is best-effort and
separately guarded.
"""

from __future__ import annotations

from app.models.audit import AuditEvent
from app.models.catalog import (
    AccessCredential,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.models.radius import RadiusUser
from app.models.subscriber import Subscriber
from app.services.connectivity_backup import (
    capture_connectivity_state,
    restore_connectivity_state,
)


def _seed_full(db_session, *, login, offer, ip="10.0.0.9"):
    subscriber = Subscriber(
        first_name="Bk", last_name="Up", email=f"{login}@example.com"
    )
    db_session.add(subscriber)
    db_session.flush()
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        login=login,
        ipv4_address=ip,
    )
    db_session.add(sub)
    db_session.flush()
    cred = AccessCredential(subscriber_id=subscriber.id, username=login, is_active=True)
    db_session.add(cred)
    db_session.flush()
    db_session.add(
        RadiusUser(
            subscriber_id=subscriber.id,
            subscription_id=sub.id,
            access_credential_id=cred.id,
            username=login,
            is_active=True,
        )
    )
    addr = IPv4Address(address=ip)
    db_session.add(addr)
    db_session.flush()
    assign = IPAssignment(
        subscriber_id=subscriber.id,
        subscription_id=sub.id,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=addr.id,
        is_active=True,
    )
    db_session.add(assign)
    db_session.commit()
    return subscriber, sub, cred, assign


def test_capture_records_local_state(db_session, catalog_offer):
    subscriber, sub, cred, assign = _seed_full(
        db_session, login="cap1", offer=catalog_offer
    )
    backup = capture_connectivity_state(
        db_session, subscriber.id, reason="suspend", include_radius=False
    )
    db_session.commit()
    assert backup is not None
    assert backup.reason == "suspend"
    # credential + radius-user flags captured
    creds = {c["username"]: c for c in backup.credentials}
    assert creds["cap1"]["credential_active"] is True
    assert creds["cap1"]["radius_user_active"] is True
    # IP state captured
    assert backup.ip_state["subscriptions"][0]["ipv4_address"] == "10.0.0.9"
    assert backup.ip_state["assignments"][0]["address"] == "10.0.0.9"
    assert backup.ip_state["assignments"][0]["is_active"] is True
    # an audit row was written
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "connectivity_backup.capture")
        .count()
        == 1
    )


def test_restore_dry_run_writes_nothing(db_session, catalog_offer):
    subscriber, sub, cred, assign = _seed_full(
        db_session, login="cap2", offer=catalog_offer
    )
    backup = capture_connectivity_state(
        db_session, subscriber.id, reason="suspend", include_radius=False
    )
    db_session.commit()
    # mutate
    cred.is_active = False
    sub.ipv4_address = None
    db_session.commit()

    plan = restore_connectivity_state(
        db_session, backup.id, dry_run=True, include_radius=False
    )
    assert plan["dry_run"] is True and plan["applied"] is False
    db_session.refresh(cred)
    db_session.refresh(sub)
    assert cred.is_active is False  # unchanged
    assert sub.ipv4_address is None


def test_restore_apply_reverts_local_state(db_session, catalog_offer):
    subscriber, sub, cred, assign = _seed_full(
        db_session, login="cap3", offer=catalog_offer
    )
    backup = capture_connectivity_state(
        db_session, subscriber.id, reason="cancel", include_radius=False
    )
    db_session.commit()
    # simulate the destructive cancel
    cred.is_active = False
    sub.ipv4_address = None
    assign.is_active = False
    ru = db_session.query(RadiusUser).filter_by(subscriber_id=subscriber.id).one()
    ru.is_active = False
    db_session.commit()

    plan = restore_connectivity_state(
        db_session,
        backup.id,
        dry_run=False,
        include_radius=False,
        restored_by="tester",
    )
    assert plan["applied"] is True
    db_session.refresh(cred)
    db_session.refresh(sub)
    db_session.refresh(assign)
    db_session.refresh(ru)
    assert cred.is_active is True
    assert ru.is_active is True
    assert sub.ipv4_address == "10.0.0.9"
    assert assign.is_active is True
    db_session.refresh(backup)
    assert backup.restored_at is not None
    assert backup.restored_by == "tester"


def test_capture_missing_subscriber_is_safe(db_session):
    import uuid

    # No such subscriber → best-effort capture returns a row with empty state,
    # never raises.
    backup = capture_connectivity_state(
        db_session, uuid.uuid4(), reason="manual", include_radius=False
    )
    assert backup is not None
    assert backup.credentials == []
