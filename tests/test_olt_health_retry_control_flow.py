"""Fast unit coverage for Celery retry control flow, without device I/O."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from celery.exceptions import MaxRetriesExceededError, Retry
from sqlalchemy.orm import Session

from app.models.network import OLTDevice
from app.models.network_monitoring import NetworkDevice
from app.tasks import olt_health_retry


@pytest.fixture
def retry_context(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MagicMock, OLTDevice, MagicMock]:
    db = MagicMock(spec=Session)
    olt = OLTDevice(id=uuid4(), name="Retry test OLT", mgmt_ip="192.0.2.10")
    device = NetworkDevice(
        id=uuid4(),
        name="Retry test monitoring device",
        mgmt_ip="192.0.2.10",
        ping_enabled=True,
        last_ping_ok=False,
    )
    db.scalar.return_value = olt
    db.scalars.return_value.all.return_value = [device]
    monkeypatch.setattr("app.services.db_session_adapter.SessionLocal", lambda: db)
    ping = MagicMock(return_value=False)
    monkeypatch.setattr(olt_health_retry, "_retry_ping_check", ping)
    return db, olt, ping


def test_retry_signal_reaches_celery_after_observation_commit(
    retry_context: tuple[MagicMock, OLTDevice, MagicMock],
    caplog: pytest.LogCaptureFixture,
) -> None:
    db, olt, _ping = retry_context
    signal = Retry(when=30)
    with patch.object(
        olt_health_retry.retry_single_olt, "retry", side_effect=signal
    ) as retry:
        with pytest.raises(Retry) as raised:
            olt_health_retry.retry_single_olt.run(str(olt.id))

    assert raised.value is signal
    retry.assert_called_once_with()
    db.commit.assert_called_once_with()
    db.rollback.assert_called_once_with()
    db.close.assert_called_once_with()
    assert "Single OLT retry task failed" not in caplog.text


def test_recovered_ping_does_not_schedule_another_attempt(
    retry_context: tuple[MagicMock, OLTDevice, MagicMock],
) -> None:
    db, olt, ping = retry_context
    ping.return_value = True
    with patch.object(olt_health_retry.retry_single_olt, "retry") as retry:
        result = olt_health_retry.retry_single_olt.run(str(olt.id))

    assert result == {"olt_id": str(olt.id), "ping_recovered": True, "error": None}
    retry.assert_not_called()
    db.rollback.assert_not_called()
    db.close.assert_called_once_with()


def test_retry_exhaustion_preserves_unrecovered_outcome(
    retry_context: tuple[MagicMock, OLTDevice, MagicMock],
) -> None:
    db, olt, _ping = retry_context
    with patch.object(
        olt_health_retry.retry_single_olt,
        "retry",
        side_effect=MaxRetriesExceededError(),
    ) as retry:
        result = olt_health_retry.retry_single_olt.run(str(olt.id))

    assert result == {"olt_id": str(olt.id), "ping_recovered": False, "error": None}
    retry.assert_called_once_with()
    db.rollback.assert_not_called()
    assert olt_health_retry.retry_single_olt.max_retries == 2
    assert olt_health_retry.retry_single_olt.default_retry_delay == 30


def test_missing_olt_does_not_retry(
    retry_context: tuple[MagicMock, OLTDevice, MagicMock],
) -> None:
    db, olt, ping = retry_context
    db.scalar.return_value = None
    with patch.object(olt_health_retry.retry_single_olt, "retry") as retry:
        result = olt_health_retry.retry_single_olt.run(str(olt.id))

    assert result["error"] == "OLT not found or inactive"
    assert result["ping_recovered"] is False
    ping.assert_not_called()
    retry.assert_not_called()


def test_unexpected_failure_keeps_existing_error_reporting(
    retry_context: tuple[MagicMock, OLTDevice, MagicMock],
) -> None:
    db, olt, _ping = retry_context
    db.scalar.side_effect = RuntimeError("database unavailable")
    with patch.object(olt_health_retry.retry_single_olt, "retry") as retry:
        result = olt_health_retry.retry_single_olt.run(str(olt.id))

    assert result["ping_recovered"] is False
    assert result["error"] == "database unavailable"
    retry.assert_not_called()
    db.rollback.assert_called_once_with()
    db.close.assert_called_once_with()
