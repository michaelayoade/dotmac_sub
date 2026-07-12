"""RADIUS health monitor: enforcement drift, metrics push, alert findings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine, text

from app.celery_app import celery_app
from app.models.radius_active_session import RadiusActiveSession
from app.services import admin_alerts, radius_health
from tests.test_customer_plan_change_prepaid import _make_offer, _make_subscription

TASK_NAME = "app.tasks.radius_health.run_radius_health_check"


def test_radacct_schema_rejects_undersized_nasportid():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE radacct (nasportid VARCHAR(32))"))
        assert radius_health._radacct_schema_signals(conn) == {
            "radacct_schema_ok": 0,
            "radacct_nasportid_capacity": 32,
        }


def test_radacct_schema_accepts_full_radius_attribute():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE radacct (nasportid VARCHAR(253))"))
        assert radius_health._radacct_schema_signals(conn) == {
            "radacct_schema_ok": 1,
            "radacct_nasportid_capacity": 253,
        }


def test_task_registered_routed_and_exported():
    import app.tasks as tasks

    assert TASK_NAME in celery_app.tasks
    assert celery_app.conf.task_routes[TASK_NAME] == {"queue": "ingestion"}
    assert "run_radius_health_check" in tasks.__all__


def _subscription(db_session, subscriber, *, login: str, status, name: str):
    offer = _make_offer(
        db_session, name=name, amount=Decimal("100.00"), plan_family="unlimited"
    )
    subscription = _make_subscription(
        db_session,
        subscriber,
        offer,
        next_billing_at=datetime.now(UTC) + timedelta(days=3),
        start_at=datetime.now(UTC) - timedelta(days=27),
    )
    subscription.login = login
    subscription.status = status
    db_session.commit()
    return subscription


def _session_row(db_session, *, username: str, subscription=None):
    row = RadiusActiveSession(
        username=username,
        acct_session_id=f"sess-{username}",
        subscription_id=subscription.id if subscription else None,
        subscriber_id=subscription.subscriber_id if subscription else None,
        session_start=datetime.now(UTC) - timedelta(hours=1),
        last_update=datetime.now(UTC),
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_enforcement_signals_counts_drift(db_session, subscriber, monkeypatch):
    from app.models.catalog import SubscriptionStatus
    from app.models.subscriber import Subscriber, SubscriberStatus

    online = _subscription(
        db_session,
        subscriber,
        login="online-1",
        status=SubscriptionStatus.active,
        name="RH Online",
    )
    _session_row(db_session, username="online-1", subscription=online)

    suspended = _subscription(
        db_session,
        subscriber,
        login="susp-1",
        status=SubscriptionStatus.suspended,
        name="RH Suspended",
    )
    _session_row(db_session, username="susp-1", subscription=suspended)

    disabled = Subscriber(
        first_name="Radius",
        last_name="Disabled",
        email="radius-disabled@example.com",
        status=SubscriberStatus.disabled,
        is_active=False,
    )
    db_session.add(disabled)
    db_session.commit()
    disabled_stale_active = _subscription(
        db_session,
        disabled,
        login="disabled-active-1",
        status=SubscriptionStatus.active,
        name="RH Disabled Active",
    )
    _session_row(
        db_session, username="disabled-active-1", subscription=disabled_stale_active
    )

    # active, has login, no session -> paid_active_without_session
    _subscription(
        db_session,
        subscriber,
        login="offline-1",
        status=SubscriptionStatus.active,
        name="RH Offline",
    )

    signals = radius_health._enforcement_signals(db_session)

    assert signals["active_sessions"] == 3
    assert signals["suspended_with_session"] == 2
    assert signals["paid_active_without_session"] == 1


def test_push_radius_metrics_writes_gauges(monkeypatch):
    written = {}

    class _Writer:
        def write_prometheus_lines(self, lines, **kwargs):
            written["lines"] = lines
            written["kwargs"] = kwargs
            return SimpleNamespace(success=True, written=len(lines))

    monkeypatch.setattr(radius_health, "_writer", lambda: _Writer())

    result = radius_health.push_radius_metrics(
        {
            "open_sessions": 855,
            "stale_open_sessions": 3,
            "acct_freshness_seconds": 42.0,
            "active_sessions": 850,
            "suspended_with_session": 2,
            "paid_active_without_session": 60,
            "radacct_read_ok": 1,
            "radacct_schema_ok": 1,
            "radacct_nasportid_capacity": 253,
        }
    )

    assert result == {"radius_metric_lines": 9, "radius_metric_write_failed": 0}
    names = {line.split("{")[0].split(" ")[0] for line in written["lines"]}
    assert "radius_acct_freshness_seconds" in names
    assert "radius_suspended_with_active_session" in names
    assert "radius_radacct_schema_ok" in names
    assert written["kwargs"]["operation"] == "radius_health"


class _FakeCache:
    def __init__(self):
        self.store: dict[str, object] = {}

    def get_json(self, key):
        return self.store.get(key)

    def set_json(self, key, value, ttl_seconds):
        self.store[key] = value
        return True


def _wire_heartbeat(monkeypatch, result: dict | None):
    cache = _FakeCache()
    monkeypatch.setattr("app.services.app_cache.get_json", cache.get_json)
    monkeypatch.setattr("app.services.app_cache.set_json", cache.set_json)
    if result is not None:
        from app.services.task_heartbeat import record_success

        record_success(radius_health.HEARTBEAT_TASK, result)
    return cache


_HEALTHY = {
    "radacct_read_ok": 1,
    "radacct_schema_ok": 1,
    "radacct_nasportid_capacity": 253,
    "open_sessions": 855,
    "stale_open_sessions": 0,
    "acct_freshness_seconds": 30.0,
    "active_sessions": 850,
    "suspended_with_session": 0,
    "paid_active_without_session": 60,
}


def test_healthy_run_raises_no_findings(db_session, monkeypatch):
    _wire_heartbeat(monkeypatch, _HEALTHY)
    assert admin_alerts._radius_health_findings(db_session) == []


def test_never_ran_raises_nothing(db_session, monkeypatch):
    _wire_heartbeat(monkeypatch, None)
    assert admin_alerts._radius_health_findings(db_session) == []


def test_unreadable_radacct_is_critical(db_session, monkeypatch):
    _wire_heartbeat(monkeypatch, {**_HEALTHY, "radacct_read_ok": 0})
    findings = admin_alerts._radius_health_findings(db_session)
    assert [f.fingerprint for f in findings] == [
        "infrastructure:radius:radacct-unreachable"
    ]
    assert findings[0].severity.name == "critical"


def test_undersized_radacct_schema_is_critical(db_session, monkeypatch):
    _wire_heartbeat(
        monkeypatch,
        {
            **_HEALTHY,
            "radacct_schema_ok": 0,
            "radacct_nasportid_capacity": 32,
        },
    )
    findings = admin_alerts._radius_health_findings(db_session)
    assert [f.fingerprint for f in findings] == [
        "infrastructure:radius:radacct-schema-incompatible"
    ]
    assert findings[0].severity.name == "critical"


def test_stale_accounting_is_critical(db_session, monkeypatch):
    _wire_heartbeat(monkeypatch, {**_HEALTHY, "acct_freshness_seconds": 4000.0})
    findings = admin_alerts._radius_health_findings(db_session)
    assert [f.fingerprint for f in findings] == ["infrastructure:radius:acct-stale"]


def test_no_open_sessions_means_no_freshness_alert(db_session, monkeypatch):
    # An empty radacct (e.g. maintenance window) has no updates to be fresh.
    _wire_heartbeat(
        monkeypatch,
        {**_HEALTHY, "open_sessions": 0, "acct_freshness_seconds": None},
    )
    assert admin_alerts._radius_health_findings(db_session) == []


def test_enforcement_drift_is_warning(db_session, monkeypatch):
    _wire_heartbeat(monkeypatch, {**_HEALTHY, "suspended_with_session": 3})
    findings = admin_alerts._radius_health_findings(db_session)
    assert [f.fingerprint for f in findings] == [
        "infrastructure:radius:enforcement-drift"
    ]
    assert findings[0].severity.name == "warning"
