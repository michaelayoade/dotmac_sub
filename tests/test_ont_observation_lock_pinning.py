"""Isolated lock-lifecycle tests; no live PostgreSQL or device I/O."""

from __future__ import annotations

from collections.abc import Generator, Iterable
from contextlib import contextmanager
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy.orm import Session

from app.models.network import OntSignalObservation, OntUnit
from app.tasks import _postgres_lock, ont_signal_observations


def _install_lock(
    monkeypatch: pytest.MonkeyPatch, *, acquired: bool, events: list[str]
) -> None:
    @contextmanager
    def lock(lock_key: int) -> Generator[bool, None, None]:
        assert lock_key == ont_signal_observations._OBS_LOCK_KEY
        events.append("lock")
        try:
            yield acquired
        finally:
            events.append("unlock")

    monkeypatch.setattr(ont_signal_observations, "postgres_session_advisory_lock", lock)


def test_snapshot_uses_shared_pinned_lock_implementation() -> None:
    assert (
        ont_signal_observations.postgres_session_advisory_lock
        is _postgres_lock.postgres_session_advisory_lock
    )


def test_contended_snapshot_does_not_open_work_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_lock(monkeypatch, acquired=False, events=events)
    create_session = MagicMock()
    monkeypatch.setattr(
        ont_signal_observations.db_session_adapter, "create_session", create_session
    )

    assert ont_signal_observations.record_ont_observations.run() == {
        "skipped_due_to_lock": 1
    }
    create_session.assert_not_called()
    assert events == ["lock", "unlock"]


def test_snapshot_commit_and_session_close_precede_unlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_lock(monkeypatch, acquired=True, events=events)
    db = MagicMock(spec=Session)
    row = OntUnit(
        id=uuid4(),
        serial_number="observation-test",
        olt_device_id=uuid4(),
        pon_port_id=uuid4(),
        olt_status="online",
        onu_rx_signal_dbm=-21.5,
    )
    db.execute.return_value.all.return_value = [row]
    saved: list[OntSignalObservation] = []

    def capture(rows: Iterable[OntSignalObservation]) -> None:
        saved.extend(rows)

    db.add_all.side_effect = capture
    db.commit.side_effect = lambda: events.append("commit")
    db.close.side_effect = lambda: events.append("close")
    monkeypatch.setattr(
        ont_signal_observations.db_session_adapter, "create_session", lambda: db
    )

    assert ont_signal_observations.record_ont_observations.run() == {"recorded": 1}
    assert events == ["lock", "commit", "close", "unlock"]
    assert len(saved) == 1
    assert saved[0].ont_unit_id == row.id
    assert saved[0].olt_device_id == row.olt_device_id
    assert saved[0].pon_port_id == row.pon_port_id
    assert saved[0].olt_status == row.olt_status
    assert saved[0].rx_signal_dbm == row.onu_rx_signal_dbm
    # Only the snapshot SELECT uses the work session, never lock/unlock SQL.
    assert db.execute.call_count == 1
    db.rollback.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (RuntimeError("snapshot unavailable"), "snapshot unavailable"),
        (SoftTimeLimitExceeded(), "ont_signal_observations_timed_out"),
    ],
)
def test_failed_snapshot_rolls_back_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, error: str
) -> None:
    events: list[str] = []
    _install_lock(monkeypatch, acquired=True, events=events)
    db = MagicMock(spec=Session)
    db.execute.side_effect = failure
    db.rollback.side_effect = lambda: events.append("rollback")
    db.close.side_effect = lambda: events.append("close")
    monkeypatch.setattr(
        ont_signal_observations.db_session_adapter, "create_session", lambda: db
    )

    assert ont_signal_observations.record_ont_observations.run() == {"error": error}
    assert events == ["lock", "rollback", "close", "unlock"]
    db.commit.assert_not_called()
    db.add_all.assert_not_called()


@pytest.mark.parametrize("unlocked", [True, False])
def test_shared_helper_pins_backend_and_discards_unreleased_connection(
    monkeypatch: pytest.MonkeyPatch, unlocked: bool
) -> None:
    engine = MagicMock()
    conn = engine.connect.return_value
    conn.dialect.name = "postgresql"
    db = MagicMock(spec=Session)
    acquired_result = MagicMock()
    acquired_result.scalar.return_value = True
    unlock_result = MagicMock()
    unlock_result.scalar.return_value = unlocked
    db.execute.side_effect = [MagicMock(), acquired_result, unlock_result]
    session_factory = MagicMock(return_value=db)
    monkeypatch.setattr(_postgres_lock, "Session", session_factory)
    monkeypatch.setattr(_postgres_lock, "SessionLocal", MagicMock(kw={"bind": engine}))

    with _postgres_lock.postgres_session_advisory_lock(
        ont_signal_observations._OBS_LOCK_KEY
    ) as acquired:
        assert acquired is True
        session_factory.assert_called_once_with(bind=conn, autoflush=False)
        db.commit.assert_called_once_with()

    if unlocked:
        conn.invalidate.assert_not_called()
    else:
        conn.invalidate.assert_called_once_with()
    db.close.assert_called_once_with()
    conn.close.assert_called_once_with()
