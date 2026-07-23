"""ACS sync must abort cleanly and schedule a bounded retry.

A ``SoftTimeLimitExceeded`` is raised asynchronously and can land while psycopg
is mid-statement. Previously the broad ``except Exception`` in the sync path
swallowed it and then reused the poisoned session for the remaining servers,
producing a cascade of "another command is already in progress" errors. These
tests pin the new behaviour: the timeout stops the pass, partial passes retry,
and the defensive rollback never raises.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from celery.exceptions import Retry


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
            patch.object(
                task_mod.sync_all_acs_devices, "retry", side_effect=Retry()
            ) as retry,
        ):
            with pytest.raises(Retry):
                task_mod.sync_all_acs_devices()
        retry.assert_called_once()
        assert retry.call_args.kwargs["countdown"] == 60
        assert isinstance(retry.call_args.kwargs["exc"], SoftTimeLimitExceeded)
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
            patch.object(
                task_mod.sync_all_acs_devices, "retry", side_effect=Retry()
            ) as retry,
        ):
            with pytest.raises(Retry):
                task_mod.sync_all_acs_devices()
        # Must NOT reuse the poisoned session for the 2nd server.
        assert sync.call_count == 1
        assert retry.call_args.kwargs["countdown"] == 60

    def test_partial_server_failure_schedules_retry(self):
        from app.tasks import tr069 as task_mod

        db = _fake_db_with_servers([SimpleNamespace(id="srv-1", name="GenieACS")])
        with (
            patch.object(task_mod, "SessionLocal", return_value=db),
            patch(
                "app.services.tr069.CpeDevices.sync_from_genieacs",
                side_effect=RuntimeError("boom"),
            ),
            patch.object(
                task_mod.sync_all_acs_devices, "retry", side_effect=Retry()
            ) as retry,
        ):
            with pytest.raises(Retry):
                task_mod.sync_all_acs_devices()
        assert retry.call_args.kwargs["countdown"] == 60
        db.close.assert_called_once()


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
