from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.catalog import (
    NasDevice,
    NasDeviceStatus,
    Subscription,
    SubscriptionStatus,
)
from app.models.usage import AccountingStatus, RadiusAccountingSession
from app.services import nas_access_path_evidence, nas_lifecycle
from app.services.radius import RadiusNasLifecycleState


def _mock_radius_states(monkeypatch):
    monkeypatch.setattr(
        nas_lifecycle,
        "radius_nas_lifecycle_states",
        lambda _db, devices: {
            device.id: RadiusNasLifecycleState(
                client_ip=device.nas_ip,
                internal_active_clients=1 if device.is_active else 0,
                external_present=bool(device.is_active and device.shared_secret),
            )
            for device in devices
        },
    )


def _subscription(db, subscriber, offer, nas):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        provisioning_nas_device_id=nas.id,
        status=SubscriptionStatus.active,
    )
    db.add(subscription)
    db.flush()
    return subscription


def _accounting(db, subscription, nas, *, session_id, seen_at):
    db.add(
        RadiusAccountingSession(
            subscription_id=subscription.id,
            nas_device_id=nas.id,
            session_id=session_id,
            status_type=AccountingStatus.stop,
            session_start=seen_at - timedelta(minutes=30),
            session_end=seen_at,
            last_update_at=seen_at,
        )
    )


def test_report_recommends_review_relink_only_when_every_subscription_is_exact(
    db_session, monkeypatch, subscriber, catalog_offer
):
    _mock_radius_states(monkeypatch)
    source = NasDevice(
        name="Retired source",
        nas_ip="10.70.0.1",
        shared_secret="plain:old",
        is_active=False,
        status=NasDeviceStatus.decommissioned,
    )
    target = NasDevice(
        name="Observed target",
        nas_ip="10.70.0.2",
        shared_secret="plain:new",
        is_active=True,
        status=NasDeviceStatus.active,
    )
    db_session.add_all([source, target])
    db_session.flush()
    first = _subscription(db_session, subscriber, catalog_offer, source)
    second = _subscription(db_session, subscriber, catalog_offer, source)
    now = datetime.now(UTC)
    _accounting(db_session, first, target, session_id="first-target", seen_at=now)
    _accounting(
        db_session,
        second,
        target,
        session_id="second-target",
        seen_at=now - timedelta(hours=1),
    )
    db_session.commit()

    report = nas_access_path_evidence.build_nas_access_path_evidence_report(
        db_session,
        now=now,
    )

    item = report.evidence[0]
    assert report.accounting_source_fresh is True
    assert report.as_dict()["status"] == "read_only"
    assert item.recommendation == (
        nas_access_path_evidence.NasEvidenceRecommendation.review_relink
    )
    assert item.exact_active_alternate_subscriptions == 2
    assert item.subscriptions_without_history == 0
    assert item.candidates[0].nas_device_id == str(target.id)
    serialized = str(report.as_dict(include_details=True))
    assert str(subscriber.id) not in serialized
    assert str(first.id) not in serialized


def test_current_historical_path_recommends_review_reactivation(
    db_session, monkeypatch, subscriber, catalog_offer
):
    _mock_radius_states(monkeypatch)
    source = NasDevice(
        name="Inactive but recently used",
        nas_ip="10.70.0.3",
        shared_secret="plain:secret",
        is_active=False,
        status=NasDeviceStatus.active,
    )
    db_session.add(source)
    db_session.flush()
    subscription = _subscription(db_session, subscriber, catalog_offer, source)
    now = datetime.now(UTC)
    _accounting(db_session, subscription, source, session_id="source-use", seen_at=now)
    db_session.commit()

    report = nas_access_path_evidence.build_nas_access_path_evidence_report(
        db_session,
        now=now,
    )

    assert report.evidence[0].recommendation == (
        nas_access_path_evidence.NasEvidenceRecommendation.review_reactivate
    )
    assert report.evidence[0].current_nas_subscriptions == 1


def test_active_nas_history_recommends_radius_identity_repair(
    db_session, monkeypatch, subscriber, catalog_offer
):
    _mock_radius_states(monkeypatch)
    source = NasDevice(
        name="Active missing identity",
        nas_ip="10.70.0.4",
        shared_secret=None,
        is_active=True,
        status=NasDeviceStatus.active,
    )
    db_session.add(source)
    db_session.flush()
    subscription = _subscription(db_session, subscriber, catalog_offer, source)
    now = datetime.now(UTC)
    _accounting(
        db_session, subscription, source, session_id="radius-proof", seen_at=now
    )
    db_session.commit()

    report = nas_access_path_evidence.build_nas_access_path_evidence_report(
        db_session,
        now=now,
    )

    assert report.evidence[0].recommendation == (
        nas_access_path_evidence.NasEvidenceRecommendation.repair_radius_identity
    )


def test_report_rejects_unbounded_history_window(db_session):
    for days in (0, 3651):
        try:
            nas_access_path_evidence.build_nas_access_path_evidence_report(
                db_session,
                window_days=days,
            )
        except ValueError as exc:
            assert "window_days" in str(exc)
        else:
            raise AssertionError("invalid history window was accepted")


def test_report_marks_stale_accounting_source(
    db_session, monkeypatch, subscriber, catalog_offer
):
    _mock_radius_states(monkeypatch)
    source = NasDevice(
        name="Stale accounting source",
        nas_ip="10.70.0.5",
        shared_secret="plain:secret",
        is_active=False,
        status=NasDeviceStatus.active,
    )
    db_session.add(source)
    db_session.flush()
    subscription = _subscription(db_session, subscriber, catalog_offer, source)
    now = datetime.now(UTC)
    _accounting(
        db_session,
        subscription,
        source,
        session_id="stale-source",
        seen_at=now - timedelta(days=2),
    )
    db_session.commit()

    report = nas_access_path_evidence.build_nas_access_path_evidence_report(
        db_session,
        now=now,
    )

    assert report.accounting_source_fresh is False
    assert report.as_dict()["status"] == "source_stale"
