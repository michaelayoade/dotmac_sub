"""Service extensions: outage validity compensation."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.catalog import NasVendor, SubscriptionStatus
from app.models.service_extension import (
    ServiceExtensionEntry,
    ServiceExtensionScope,
    ServiceExtensionStatus,
)
from app.models.subscriber import Subscriber
from app.schemas.catalog import NasDeviceCreate, SubscriptionCreate
from app.services import catalog as catalog_service
from app.services import nas as nas_service
from app.services import service_extensions as svc

_WIN_START = datetime(2026, 6, 10, 8, 0, tzinfo=UTC)
_WIN_END = datetime(2026, 6, 10, 20, 0, tzinfo=UTC)


def _naive(dt):
    return dt.replace(tzinfo=None) if dt and dt.tzinfo else dt


def _another_subscriber(db_session):
    sub = Subscriber(
        first_name="Out", last_name="Age", email=f"ext-{uuid4().hex[:8]}@example.com"
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def _sub(db_session, subscriber, catalog_offer, *, nas_id=None, next_billing_at=None):
    return catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.active,
            provisioning_nas_device_id=nas_id,
            next_billing_at=next_billing_at or datetime(2026, 7, 1, tzinfo=UTC),
        ),
    )


def test_create_requires_valid_window_and_days(db_session, subscriber, catalog_offer):
    _sub(db_session, subscriber, catalog_offer)
    with pytest.raises(HTTPException) as exc:
        svc.create_extension(
            db_session,
            reason="x",
            window_start=_WIN_END,
            window_end=_WIN_START,  # end before start
            days=2,
            scope_type=ServiceExtensionScope.network,
        )
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException):
        svc.create_extension(
            db_session,
            reason="x",
            window_start=_WIN_START,
            window_end=_WIN_END,
            days=99,  # over MAX
            scope_type=ServiceExtensionScope.network,
        )


def test_apply_network_scope_extends_all_active(db_session, subscriber, catalog_offer):
    s1 = _sub(
        db_session,
        subscriber,
        catalog_offer,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    s2 = _sub(
        db_session,
        _another_subscriber(db_session),
        catalog_offer,
        next_billing_at=datetime(2026, 7, 15, tzinfo=UTC),
    )

    ext = svc.create_extension(
        db_session,
        reason="Backbone outage",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=2,
        scope_type=ServiceExtensionScope.network,
        created_by="admin-1",
    )
    applied = svc.apply_extension(db_session, str(ext.id), actor_id="admin-1")

    assert applied.status == ServiceExtensionStatus.applied
    assert applied.affected_count == 2
    db_session.refresh(s1)
    db_session.refresh(s2)
    assert _naive(s1.next_billing_at) == datetime(2026, 7, 3)
    assert _naive(s2.next_billing_at) == datetime(2026, 7, 17)

    entries = (
        db_session.query(ServiceExtensionEntry)
        .filter(ServiceExtensionEntry.extension_id == ext.id)
        .all()
    )
    assert len(entries) == 2


def test_apply_is_idempotent(db_session, subscriber, catalog_offer):
    _sub(db_session, subscriber, catalog_offer)
    ext = svc.create_extension(
        db_session,
        reason="outage",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=1,
        scope_type=ServiceExtensionScope.network,
    )
    svc.apply_extension(db_session, str(ext.id))
    with pytest.raises(HTTPException) as exc:
        svc.apply_extension(db_session, str(ext.id))
    assert exc.value.status_code == 409


def test_nas_scope_only_extends_matching(db_session, subscriber, catalog_offer):
    nas = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(
            name="NAS-A",
            vendor=NasVendor.mikrotik,
            ip_address="10.0.0.1",
            management_ip="10.0.0.1",
        ),
    )
    on_nas = _sub(
        db_session,
        subscriber,
        catalog_offer,
        nas_id=nas.id,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    off_nas = _sub(
        db_session,
        _another_subscriber(db_session),
        catalog_offer,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    ext = svc.create_extension(
        db_session,
        reason="NAS down",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=3,
        scope_type=ServiceExtensionScope.nas_device,
        scope_id=str(nas.id),
    )
    applied = svc.apply_extension(db_session, str(ext.id))

    assert applied.affected_count == 1
    db_session.refresh(on_nas)
    db_session.refresh(off_nas)
    assert _naive(on_nas.next_billing_at) == datetime(2026, 7, 4)
    assert _naive(off_nas.next_billing_at) == datetime(2026, 7, 1)


def test_skips_subscription_without_billing_date(db_session, subscriber, catalog_offer):
    no_date = _sub(db_session, subscriber, catalog_offer)
    no_date.next_billing_at = None
    db_session.commit()

    ext = svc.create_extension(
        db_session,
        reason="outage",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=2,
        scope_type=ServiceExtensionScope.network,
    )
    applied = svc.apply_extension(db_session, str(ext.id))
    assert applied.affected_count == 0
    assert applied.skipped_count == 1


def test_cancel_pending_extension(db_session, subscriber, catalog_offer):
    _sub(db_session, subscriber, catalog_offer)
    ext = svc.create_extension(
        db_session,
        reason="outage",
        window_start=_WIN_START,
        window_end=_WIN_END,
        days=1,
        scope_type=ServiceExtensionScope.network,
    )
    svc.cancel_extension(db_session, str(ext.id), actor_id="admin-1")
    db_session.refresh(ext)
    assert ext.status == ServiceExtensionStatus.canceled
    with pytest.raises(HTTPException):
        svc.apply_extension(db_session, str(ext.id))


def test_subscribers_scope_requires_ids(db_session, subscriber, catalog_offer):
    _sub(db_session, subscriber, catalog_offer)
    with pytest.raises(HTTPException) as exc:
        svc.create_extension(
            db_session,
            reason="outage",
            window_start=_WIN_START,
            window_end=_WIN_END,
            days=1,
            scope_type=ServiceExtensionScope.subscribers,
            subscriber_ids=[],
        )
    assert exc.value.status_code == 400
