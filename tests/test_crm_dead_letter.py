"""CRM push dead-letter: recording on terminal failure, stamping, re-drive."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from app.models.crm_sync_failure import CrmSyncFailure, CrmSyncFailureStatus
from app.services import crm_sync_failures
from app.tasks.crm_sync import CrmPushError, push_subscriber_change
from app.web.admin import integrations as integrations_admin


def _failure(db, *, status=CrmSyncFailureStatus.unresolved):
    row = CrmSyncFailure(
        entity="subscriber",
        external_id=str(uuid.uuid4()),
        external_system="dotmac",
        payload={"balance": "100.00"},
        error="boom",
        attempts=9,
        status=status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class TestRecordDeadLetter:
    def test_push_failure_raises_for_retry(self):
        with patch(
            "app.services.crm_webhook.push_subscriber_change",
            return_value=None,
        ):
            with pytest.raises(CrmPushError):
                push_subscriber_change.run("sub-1", {"status": "active"}, "selfcare")

    def test_on_failure_records_row(self, db_session):
        from app.tasks import crm_sync

        with patch.object(crm_sync, "task_session", create=True):
            pass  # ensure import path resolves
        # Call the recorder directly with a task_session bound to the test db.
        from contextlib import contextmanager

        @contextmanager
        def _fake_session():
            yield db_session

        with patch("app.db.task_session", _fake_session):
            crm_sync._record_dead_letter(
                external_id="abc",
                external_system="splynx",
                payload={"x": 1},
                error="CRM unreachable",
                attempts=9,
            )
        rows = db_session.query(CrmSyncFailure).all()
        assert len(rows) == 1
        assert rows[0].external_id == "abc"
        assert rows[0].external_system == "splynx"
        assert rows[0].status == CrmSyncFailureStatus.unresolved
        assert rows[0].attempts == 9

    def test_on_failure_redacts_nin_from_payload(self, db_session):
        from app.tasks import crm_sync

        from contextlib import contextmanager

        @contextmanager
        def _fake_session():
            yield db_session

        with patch("app.db.task_session", _fake_session):
            crm_sync._record_dead_letter(
                external_id="abc",
                external_system="selfcare",
                payload={"nin": "12345678901", "status": "active"},
                error="CRM unreachable",
                attempts=9,
            )
        row = db_session.query(CrmSyncFailure).one()
        assert row.payload["nin"] == "<redacted>"


class TestVisibility:
    def test_unresolved_count_and_list(self, db_session):
        _failure(db_session)
        _failure(db_session)
        _failure(db_session, status=CrmSyncFailureStatus.resolved)
        assert crm_sync_failures.unresolved_count(db_session) == 2
        assert len(crm_sync_failures.list_failures(db_session)) == 2
        assert (
            len(crm_sync_failures.list_failures(db_session, unresolved_only=False)) == 3
        )

    def test_integrations_template_renders_dead_letter_controls(self):
        template = Path(
            "templates/admin/integrations/connectors/index.html"
        ).read_text()

        assert "CRM Dead Letters" in template
        assert "/admin/integrations/crm-dead-letters/redrive" in template
        assert "Re-drive All" in template
        assert 'name="failure_id"' in template


class TestRedrive:
    def test_redrive_one_resolves_and_reenqueues(self, db_session):
        row = _failure(db_session)
        with patch("app.tasks.crm_sync.push_subscriber_change") as task:
            ok = crm_sync_failures.redrive(db_session, str(row.id))
        assert ok is True
        task.delay.assert_called_once()
        db_session.refresh(row)
        assert row.status == CrmSyncFailureStatus.resolved
        assert row.resolved_at is not None

    def test_redrive_skips_already_resolved(self, db_session):
        row = _failure(db_session, status=CrmSyncFailureStatus.resolved)
        with patch("app.tasks.crm_sync.push_subscriber_change") as task:
            ok = crm_sync_failures.redrive(db_session, str(row.id))
        assert ok is False
        task.delay.assert_not_called()

    def test_redrive_all_sweeps_unresolved(self, db_session):
        _failure(db_session)
        _failure(db_session)
        with patch("app.tasks.crm_sync.push_subscriber_change") as task:
            result = crm_sync_failures.redrive_all(db_session)
        assert result["redriven"] == 2
        assert task.delay.call_count == 2
        assert crm_sync_failures.unresolved_count(db_session) == 0

    def test_redrive_skips_rows_without_payload(self, db_session):
        row = _failure(db_session)
        row.payload = None
        db_session.commit()
        with patch("app.tasks.crm_sync.push_subscriber_change") as task:
            result = crm_sync_failures.redrive_all(db_session)
        assert result["redriven"] == 0
        task.delay.assert_not_called()
        # Still unresolved — surfaced for manual handling, not silently lost.
        assert crm_sync_failures.unresolved_count(db_session) == 1

    def test_admin_redrive_redirect_reports_count(self, db_session):
        _failure(db_session)
        _failure(db_session)
        with patch("app.tasks.crm_sync.push_subscriber_change"):
            response = integrations_admin.crm_dead_letters_redrive(
                failure_id="", db=db_session
            )

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/integrations/?crm_redrive=2"

    def test_admin_redrive_redirect_reports_missing_row(self, db_session):
        response = integrations_admin.crm_dead_letters_redrive(
            failure_id=str(uuid.uuid4()), db=db_session
        )

        assert response.status_code == 303
        assert (
            response.headers["location"] == "/admin/integrations/?crm_redrive=not_found"
        )
