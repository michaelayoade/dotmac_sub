"""Dead-man switch for the native infrastructure poller (heartbeat + alerts)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.network_monitoring import NetworkDevice
from app.services import admin_alerts, infrastructure_polling


class _FakeCache:
    def __init__(self):
        self.store: dict[str, object] = {}

    def get_json(self, key):
        return self.store.get(key)

    def set_json(self, key, value, ttl_seconds):
        self.store[key] = value
        return True


def _wire_cache(monkeypatch) -> _FakeCache:
    cache = _FakeCache()
    monkeypatch.setattr("app.services.app_cache.get_json", cache.get_json)
    monkeypatch.setattr("app.services.app_cache.set_json", cache.set_json)
    return cache


def _pingable(db_session, name: str, ip: str, *, pinged_at=None):
    device = NetworkDevice(
        name=name,
        mgmt_ip=ip,
        is_active=True,
        ping_enabled=True,
        last_ping_at=pinged_at,
    )
    db_session.add(device)
    db_session.flush()
    return device


def test_success_stamps_heartbeat_and_resets_streak(db_session, monkeypatch):
    cache = _wire_cache(monkeypatch)
    infrastructure_polling.record_poll_skip()
    infrastructure_polling.record_poll_skip()
    assert cache.store[infrastructure_polling.SKIP_STREAK_KEY] == 2

    infrastructure_polling.record_poll_success(
        {"checked": 300, "interface_write_failed": 0, "skipped": "no"}
    )

    heartbeat = cache.store[infrastructure_polling.HEARTBEAT_KEY]
    assert heartbeat["result"] == {"checked": 300, "interface_write_failed": 0}
    assert cache.store[infrastructure_polling.SKIP_STREAK_KEY] == 0


def test_snapshot_reads_heartbeat_and_db(db_session, monkeypatch):
    _wire_cache(monkeypatch)
    now = datetime.now(UTC)
    _pingable(db_session, "wd-1", "10.81.0.1", pinged_at=now - timedelta(seconds=30))
    infrastructure_polling.record_poll_success(
        {"interface_write_failed": 2}, now=now - timedelta(seconds=120)
    )

    snapshot = infrastructure_polling.poll_health_snapshot(db_session, now=now)

    assert 115 <= snapshot["last_success_age_seconds"] <= 125
    assert snapshot["interface_write_failed"] == 2
    assert 25 <= snapshot["newest_ping_age_seconds"] <= 35
    assert snapshot["pingable_devices"] == 1
    assert snapshot["skip_streak"] == 0


def _findings_for(db_session, monkeypatch, snapshot: dict):
    defaults = {
        "last_success_age_seconds": 60.0,
        "skip_streak": 0,
        "interface_write_failed": 0,
        "newest_ping_age_seconds": 60.0,
        "pingable_devices": 500,
        "poll_interval_seconds": 60,
    }
    defaults.update(snapshot)
    monkeypatch.setattr(
        "app.services.infrastructure_polling.poll_health_snapshot",
        lambda db, now=None: defaults,
    )
    return admin_alerts._poll_health_findings(db_session)


def test_healthy_snapshot_raises_no_findings(db_session, monkeypatch):
    assert _findings_for(db_session, monkeypatch, {}) == []


def test_stalled_run_raises_critical(db_session, monkeypatch):
    findings = _findings_for(
        db_session, monkeypatch, {"last_success_age_seconds": 400.0}
    )
    assert [f.fingerprint for f in findings] == ["infrastructure:poll:stalled"]
    assert findings[0].severity.name == "critical"


def test_never_ran_raises_stalled(db_session, monkeypatch):
    findings = _findings_for(
        db_session, monkeypatch, {"last_success_age_seconds": None}
    )
    assert "infrastructure:poll:stalled" in [f.fingerprint for f in findings]


def test_skip_streak_raises_lock_stuck(db_session, monkeypatch):
    findings = _findings_for(db_session, monkeypatch, {"skip_streak": 5})
    assert [f.fingerprint for f in findings] == ["infrastructure:poll:lock-stuck"]


def test_write_failures_raise_warning(db_session, monkeypatch):
    findings = _findings_for(db_session, monkeypatch, {"interface_write_failed": 12})
    assert [f.fingerprint for f in findings] == ["infrastructure:poll:vm-write-failed"]
    assert findings[0].severity.name == "warning"


def test_stale_ping_rows_raise_critical(db_session, monkeypatch):
    findings = _findings_for(
        db_session, monkeypatch, {"newest_ping_age_seconds": 3600.0}
    )
    assert [f.fingerprint for f in findings] == ["infrastructure:poll:ping-stale"]


def test_no_pingable_devices_means_no_ping_finding(db_session, monkeypatch):
    findings = _findings_for(
        db_session,
        monkeypatch,
        {"newest_ping_age_seconds": None, "pingable_devices": 0},
    )
    assert findings == []
