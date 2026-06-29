"""ACS sync must abort cleanly on a Celery soft time limit.

A ``SoftTimeLimitExceeded`` is raised asynchronously and can land while psycopg
is mid-statement. Previously the broad ``except Exception`` in the sync path
swallowed it and then reused the poisoned session for the remaining servers,
producing a cascade of "another command is already in progress" errors. These
tests pin the new behaviour: the timeout propagates (task stops), generic
errors are still swallowed per-server, and the defensive rollback never raises.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from billiard.exceptions import SoftTimeLimitExceeded


def _fake_db_with_servers(servers):
    db = MagicMock()
    db.scalars.return_value.all.return_value = servers
    return db


class TestSyncAllAcsDevicesSoftTimeout:
    def test_soft_timeout_propagates_and_closes_session(self):
        from app.tasks import tr069 as task_mod

        db = _fake_db_with_servers([SimpleNamespace(id="srv-1", name="GenieACS")])
        with (
            patch.object(task_mod, "SessionLocal", return_value=db),
            patch(
                "app.services.tr069.CpeDevices.sync_from_genieacs",
                side_effect=SoftTimeLimitExceeded(),
            ),
        ):
            with pytest.raises(SoftTimeLimitExceeded):
                task_mod.sync_all_acs_devices()
        db.close.assert_called_once()  # finally still runs → connection reset

    def test_soft_timeout_stops_before_second_server(self):
        from app.tasks import tr069 as task_mod

        db = _fake_db_with_servers(
            [
                SimpleNamespace(id="srv-1", name="A"),
                SimpleNamespace(id="srv-2", name="B"),
            ]
        )
        with (
            patch.object(task_mod, "SessionLocal", return_value=db),
            patch(
                "app.services.tr069.CpeDevices.sync_from_genieacs",
                side_effect=SoftTimeLimitExceeded(),
            ) as sync,
        ):
            with pytest.raises(SoftTimeLimitExceeded):
                task_mod.sync_all_acs_devices()
        # Must NOT reuse the poisoned session for the 2nd server.
        assert sync.call_count == 1

    def test_generic_error_is_still_swallowed_per_server(self):
        from app.tasks import tr069 as task_mod

        db = _fake_db_with_servers([SimpleNamespace(id="srv-1", name="GenieACS")])
        with (
            patch.object(task_mod, "SessionLocal", return_value=db),
            patch(
                "app.services.tr069.CpeDevices.sync_from_genieacs",
                side_effect=RuntimeError("boom"),
            ),
        ):
            result = task_mod.sync_all_acs_devices()
        assert result["errors"] == 1
        assert result["servers_synced"] == 0


class TestSafeRollback:
    def test_swallows_rollback_failure(self):
        from app.services.tr069 import _safe_rollback

        db = MagicMock()
        db.rollback.side_effect = Exception("another command is already in progress")
        _safe_rollback(db)  # must not raise
        db.rollback.assert_called_once()

    def test_normal_rollback_passes_through(self):
        from app.services.tr069 import _safe_rollback

        db = MagicMock()
        _safe_rollback(db)
        db.rollback.assert_called_once()
