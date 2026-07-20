"""Real observability on the integrations surfaces (audit P-C).

Connector health must come from real signals (integration runs / webhook
deliveries), API-key ``last_used_at`` must be stamped (throttled) on auth,
and the activity log must show connector names and real run durations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.auth import ApiKey
from app.models.connector import ConnectorConfig
from app.models.integration import (
    IntegrationJob,
    IntegrationRun,
    IntegrationRunStatus,
    IntegrationTarget,
)
from app.services import web_integrations
from app.services.auth import hash_api_key
from app.services.auth_dependencies import require_user_auth


def _make_connector(db, name: str | None = None) -> ConnectorConfig:
    connector = ConnectorConfig(name=name or f"conn-{uuid4().hex[:10]}")
    db.add(connector)
    db.commit()
    return connector


def _make_run(
    db,
    connector: ConnectorConfig,
    *,
    status: IntegrationRunStatus,
    started_at: datetime,
    finished_at: datetime | None = None,
    job_name: str | None = None,
) -> IntegrationRun:
    target = IntegrationTarget(
        name=f"target-{uuid4().hex[:10]}", connector_config_id=connector.id
    )
    db.add(target)
    db.flush()
    job = IntegrationJob(
        target_id=target.id, name=job_name or f"job-{uuid4().hex[:10]}"
    )
    db.add(job)
    db.flush()
    run = IntegrationRun(
        job_id=job.id,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
    )
    db.add(run)
    db.commit()
    return run


def _health_for(data: dict, connector: ConnectorConfig) -> str:
    for row in data["integrations"]:
        if row["connector"].id == connector.id:
            return row["health"]
    raise AssertionError("connector not in installed integrations data")


def test_connector_health_unknown_when_no_signals(db_session):
    connector = _make_connector(db_session)

    data = web_integrations.build_installed_integrations_data(db_session)

    assert _health_for(data, connector) == "unknown"
    assert data["stats"]["healthy"] == 0


def test_connector_health_healthy_when_last_run_succeeded(db_session):
    connector = _make_connector(db_session)
    now = datetime.now(UTC)
    _make_run(
        db_session,
        connector,
        status=IntegrationRunStatus.failed,
        started_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2),
    )
    _make_run(
        db_session,
        connector,
        status=IntegrationRunStatus.success,
        started_at=now - timedelta(hours=1),
        finished_at=now - timedelta(hours=1),
    )

    data = web_integrations.build_installed_integrations_data(db_session)

    assert _health_for(data, connector) == "healthy"
    assert data["stats"]["healthy"] == 1


def test_connector_health_degraded_when_last_run_failed(db_session):
    connector = _make_connector(db_session)
    now = datetime.now(UTC)
    _make_run(
        db_session,
        connector,
        status=IntegrationRunStatus.success,
        started_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2),
    )
    _make_run(
        db_session,
        connector,
        status=IntegrationRunStatus.failed,
        started_at=now - timedelta(hours=1),
        finished_at=now - timedelta(hours=1),
    )

    data = web_integrations.build_installed_integrations_data(db_session)

    assert _health_for(data, connector) == "degraded"


def test_connector_health_ignores_in_flight_runs(db_session):
    connector = _make_connector(db_session)
    _make_run(
        db_session,
        connector,
        status=IntegrationRunStatus.running,
        started_at=datetime.now(UTC),
    )

    data = web_integrations.build_installed_integrations_data(db_session)

    assert _health_for(data, connector) == "unknown"


def test_activity_log_resolves_connector_name_and_run_duration(db_session):
    connector = _make_connector(db_session, name=f"Billing sync {uuid4().hex[:6]}")
    started = datetime.now(UTC) - timedelta(minutes=10)
    _make_run(
        db_session,
        connector,
        status=IntegrationRunStatus.success,
        started_at=started,
        finished_at=started + timedelta(seconds=2, milliseconds=500),
        job_name="pull-tickets",
    )

    data = web_integrations.build_installed_integrations_data(db_session)

    entries = [
        item
        for item in data["activity_log"]
        if item["connector_id"] == str(connector.id)
    ]
    assert entries, "run should appear in the activity log"
    entry = entries[0]
    assert entry["connector_name"] == connector.name
    assert entry["event_type"] == "job: pull-tickets"
    assert entry["status"] == "success"
    assert entry["response_time_ms"] == 2500


def test_api_key_auth_stamps_last_used_at(db_session):
    raw = f"key-{uuid4().hex}"
    key = ApiKey(
        label="obs",
        key_hash=hash_api_key(raw),
        scopes=["audit:read"],
        is_active=True,
    )
    db_session.add(key)
    db_session.commit()
    assert key.last_used_at is None

    auth = require_user_auth(authorization=None, x_api_key=raw, db=db_session)

    assert auth["principal_type"] == "api_key"
    db_session.refresh(key)
    assert key.last_used_at is not None


def test_api_key_last_used_at_write_is_throttled(db_session):
    raw = f"key-{uuid4().hex}"
    recent = datetime.now(UTC) - timedelta(minutes=2)
    key = ApiKey(
        label="obs",
        key_hash=hash_api_key(raw),
        scopes=["audit:read"],
        is_active=True,
        last_used_at=recent,
    )
    db_session.add(key)
    db_session.commit()

    require_user_auth(authorization=None, x_api_key=raw, db=db_session)

    db_session.refresh(key)
    stamped = key.last_used_at
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    assert stamped == recent, "recent stamp must not be rewritten on every request"


def test_api_key_last_used_at_refreshes_after_window(db_session):
    raw = f"key-{uuid4().hex}"
    stale = datetime.now(UTC) - timedelta(minutes=30)
    key = ApiKey(
        label="obs",
        key_hash=hash_api_key(raw),
        scopes=["audit:read"],
        is_active=True,
        last_used_at=stale,
    )
    db_session.add(key)
    db_session.commit()

    require_user_auth(authorization=None, x_api_key=raw, db=db_session)

    db_session.refresh(key)
    stamped = key.last_used_at
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    assert stamped > stale + timedelta(minutes=20)
