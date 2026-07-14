"""RADIUS populate single-flight wrapper (review task #16).

On Postgres the tasks take a pg advisory lock so two overlapping refreshes
can't interleave their radcheck rewrites. The lock MUST go through
``postgres_session_advisory_lock`` (pinned connection): session-level
advisory locks belong to one Postgres backend, and a pooled Session that
commits after acquiring can unlock on a *different* backend — the unlock
silently returns false and the lock strands, skipping every later run as
"previous run still in progress" (the db_session_adapter bug that bit the
infrastructure poller in prod).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _held_lock(acquired: bool) -> MagicMock:
    lock = MagicMock()
    lock.return_value.__enter__.return_value = acquired
    return lock


def test_refresh_runs_populate_under_pinned_lock():
    mock_lock = _held_lock(True)
    with (
        patch("app.tasks.radius_population.postgres_session_advisory_lock", mock_lock),
        patch(
            "app.services.radius_population.populate",
            return_value={"radcheck_upserts": 3},
        ) as mock_pop,
    ):
        from app.tasks.radius_population import (
            _POPULATE_LOCK_KEY,
            refresh_radius_from_subs,
        )

        result = refresh_radius_from_subs()

    mock_lock.assert_called_once_with(_POPULATE_LOCK_KEY)
    mock_pop.assert_called_once_with(dry_run=False)
    assert result == {"radcheck_upserts": 3}


def test_refresh_skips_when_lock_not_acquired():
    """A second concurrent run that can't take the lock skips populate."""
    with (
        patch(
            "app.tasks.radius_population.postgres_session_advisory_lock",
            _held_lock(False),
        ),
        patch("app.services.radius_population.populate") as mock_pop,
    ):
        from app.tasks.radius_population import refresh_radius_from_subs

        result = refresh_radius_from_subs()

    mock_pop.assert_not_called()
    assert result == {"skipped_locked": 1}


def test_device_login_sync_uses_pinned_lock():
    mock_lock = _held_lock(True)
    with (
        patch("app.tasks.radius_population.postgres_session_advisory_lock", mock_lock),
        patch(
            "app.services.radius_population.populate_device_login",
            return_value={"admin_upserts": 1},
        ) as mock_pop,
        patch("app.services.radius_population.record_device_login_sync_status"),
        patch("app.db.SessionLocal", return_value=MagicMock()),
    ):
        from app.tasks.radius_population import (
            _DEVICE_LOGIN_LOCK_KEY,
            sync_device_login,
        )

        result = sync_device_login()

    mock_lock.assert_called_once_with(_DEVICE_LOGIN_LOCK_KEY)
    mock_pop.assert_called_once()
    assert result == {"admin_upserts": 1}


def test_device_login_sync_skips_when_lock_not_acquired():
    with (
        patch(
            "app.tasks.radius_population.postgres_session_advisory_lock",
            _held_lock(False),
        ),
        patch("app.services.radius_population.populate_device_login") as mock_pop,
    ):
        from app.tasks.radius_population import sync_device_login

        result = sync_device_login()

    mock_pop.assert_not_called()
    assert result == {"skipped_locked": 1}


def test_lock_helper_is_the_pinned_connection_implementation():
    """Guard: the task module's lock helper must be the shared pinned-connection
    one, not a local reimplementation on a pooled session."""
    from app.tasks import _postgres_lock, radius_population

    assert (
        radius_population.postgres_session_advisory_lock
        is _postgres_lock.postgres_session_advisory_lock
    )
