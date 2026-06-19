"""RADIUS populate single-flight wrapper (review task #16).

On Postgres the task takes a pg advisory lock so two overlapping refreshes
can't interleave their radcheck rewrites. On SQLite (tests) the lock is
skipped and populate runs normally — verified here so the wrapper plumbing
doesn't regress.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_refresh_calls_populate_without_lock_on_non_pg():
    """On a non-Postgres bind the advisory lock is skipped and populate runs.

    SessionLocal is patched to a fake non-pg session so the task doesn't try to
    reach the real Postgres (and so the lock path is exercised as a skip)."""
    fake_session = MagicMock()
    fake_session.bind.dialect.name = "sqlite"

    with (
        patch("app.db.SessionLocal", return_value=fake_session),
        patch(
            "app.services.radius_population.populate",
            return_value={"radcheck_upserts": 3},
        ) as mock_pop,
    ):
        from app.tasks.radius_population import refresh_radius_from_subs

        result = refresh_radius_from_subs()

    mock_pop.assert_called_once_with(dry_run=False)
    assert result == {"radcheck_upserts": 3}
    fake_session.close.assert_called_once()


def test_refresh_skips_when_lock_not_acquired():
    """On Postgres, a second concurrent run that can't take the lock skips."""
    fake_session = MagicMock()
    fake_session.bind.dialect.name = "postgresql"
    # pg_try_advisory_lock(...) -> False (someone else holds it)
    fake_session.execute.return_value.scalar.return_value = False

    with (
        patch("app.db.SessionLocal", return_value=fake_session),
        patch("app.services.radius_population.populate") as mock_pop,
    ):
        from app.tasks.radius_population import refresh_radius_from_subs

        result = refresh_radius_from_subs()

    mock_pop.assert_not_called()
    assert result == {"skipped_locked": 1}
    fake_session.close.assert_called_once()
