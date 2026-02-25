from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.models.audit import AuditEvent
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.scheduler import ScheduledTask
from app.services import web_system_export_tool as export_service


def test_module_fields_returns_columns():
    fields = export_service.module_fields("subscribers")
    assert "id" in fields
    assert "email" in fields
    assert "status" in fields


def test_export_csv_filters_by_status_and_date(db_session):
    older = Subscriber(
        first_name="Old",
        last_name="Active",
        email="export-old@example.com",
        status=SubscriberStatus.active,
        created_at=datetime.now(UTC) - timedelta(days=4),
    )
    newer = Subscriber(
        first_name="New",
        last_name="Suspended",
        email="export-new@example.com",
        status=SubscriberStatus.suspended,
        created_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(older)
    db_session.add(newer)
    db_session.commit()

    date_from = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()
    csv_text, row_count = export_service.export_csv(
        db_session,
        module="subscribers",
        selected_fields=["email", "status"],
        delimiter=",",
        date_from=date_from,
        date_to=None,
        status="suspended",
        include_headers=True,
    )

    assert row_count == 1
    assert "email,status" in csv_text
    assert "export-new@example.com,suspended" in csv_text
    assert "export-old@example.com" not in csv_text


def test_export_csv_supports_semicolon_without_headers(db_session):
    subscriber = Subscriber(
        first_name="Semi",
        last_name="Colon",
        email="export-semi@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()

    csv_text, row_count = export_service.export_csv(
        db_session,
        module="subscribers",
        selected_fields=["email"],
        delimiter=";",
        include_headers=False,
    )

    assert row_count >= 1
    assert "email" not in csv_text.splitlines()[0]
    assert "export-semi@example.com" in csv_text


def test_export_csv_rejects_invalid_module(db_session):
    with pytest.raises(ValueError):
        export_service.export_csv(
            db_session,
            module="invalid-module",
            selected_fields=["id"],
            delimiter=",",
        )


def test_export_content_json(db_session):
    subscriber = Subscriber(
        first_name="Json",
        last_name="Export",
        email="export-json@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()

    payload, media_type, extension, row_count = export_service.export_content(
        db_session,
        module="subscribers",
        selected_fields=["email"],
        delimiter=",",
        export_format="json",
    )
    data = json.loads(payload.decode("utf-8"))
    assert media_type == "application/json"
    assert extension == "json"
    assert row_count >= 1
    assert any(item.get("email") == "export-json@example.com" for item in data)


def test_export_content_xlsx(db_session):
    subscriber = Subscriber(
        first_name="Xlsx",
        last_name="Export",
        email="export-xlsx@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()

    payload, media_type, extension, row_count = export_service.export_content(
        db_session,
        module="subscribers",
        selected_fields=["email"],
        delimiter=",",
        export_format="xlsx",
    )
    assert payload.startswith(b"PK")
    assert media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert extension == "xlsx"
    assert row_count >= 1


def test_export_content_pdf(db_session):
    subscriber = Subscriber(
        first_name="Pdf",
        last_name="Export",
        email="export-pdf@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()

    payload, media_type, extension, row_count = export_service.export_content(
        db_session,
        module="subscribers",
        selected_fields=["email"],
        delimiter=",",
        export_format="pdf",
    )
    assert payload.startswith(b"%PDF")
    assert media_type == "application/pdf"
    assert extension == "pdf"
    assert row_count >= 1


def test_create_export_schedule(db_session):
    schedule = export_service.create_export_schedule(
        db_session,
        name="Weekly Subscribers",
        module="subscribers",
        selected_fields=["email", "status"],
        delimiter=",",
        export_format="csv",
        date_from=None,
        date_to=None,
        status=None,
        include_headers=True,
        recipient_email="ops@example.com",
        frequency="weekly",
    )
    assert schedule.task_name == export_service.EXPORT_SCHEDULE_TASK_NAME
    assert schedule.interval_seconds == 86400 * 7
    assert isinstance(schedule.kwargs_json, dict)
    assert schedule.kwargs_json.get("recipient_email") == "ops@example.com"
    assert schedule.kwargs_json.get("module") == "subscribers"


def test_export_templates_create_list_get_delete(db_session):
    created = export_service.create_export_template(
        db_session,
        name="My Subscriber Template",
        module="subscribers",
        selected_fields=["email", "status", "missing_field"],
        delimiter=";",
        export_format="csv",
        date_from="2026-01-01",
        date_to="2026-01-31",
        status="active",
        include_headers=True,
    )
    assert created["name"] == "My Subscriber Template"
    template_id = str(created["id"])

    listed = export_service.list_export_templates(db_session)
    assert any(str(item["id"]) == template_id for item in listed)

    loaded = export_service.get_export_template(db_session, template_id)
    assert loaded is not None
    config = loaded["config"]
    assert config["module"] == "subscribers"
    assert config["delimiter"] == ";"
    assert config["selected_fields"] == ["email", "status"]
    assert config["status"] == "active"

    export_service.delete_export_template(db_session, template_id=template_id)
    assert export_service.get_export_template(db_session, template_id) is None


def test_list_export_schedules_filters_non_export_tasks(db_session):
    export_schedule = export_service.create_export_schedule(
        db_session,
        name="Daily Subscribers",
        module="subscribers",
        selected_fields=["email"],
        delimiter=",",
        export_format="csv",
        date_from=None,
        date_to=None,
        status=None,
        include_headers=True,
        recipient_email="ops@example.com",
        frequency="daily",
    )
    db_session.add(
        ScheduledTask(
            name="Other",
            task_name="app.tasks.other.run",
            interval_seconds=60,
            enabled=True,
        )
    )
    db_session.commit()

    schedules = export_service.list_export_schedules(db_session)
    ids = {str(item.id) for item in schedules}
    assert str(export_schedule.id) in ids
    assert all(item.task_name == export_service.EXPORT_SCHEDULE_TASK_NAME for item in schedules)


def test_execute_scheduled_export_updates_last_run_and_sends_email(db_session, monkeypatch):
    subscriber = Subscriber(
        first_name="Scheduled",
        last_name="Export",
        email="scheduled-export@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()

    schedule = export_service.create_export_schedule(
        db_session,
        name="Hourly Subscribers",
        module="subscribers",
        selected_fields=["email"],
        delimiter=",",
        export_format="csv",
        date_from=None,
        date_to=None,
        status=None,
        include_headers=True,
        recipient_email="ops@example.com",
        frequency="hourly",
    )

    calls: list[dict[str, object]] = []

    def _fake_send_export_email(db, **kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return True

    monkeypatch.setattr(export_service, "_send_export_email", _fake_send_export_email)
    result = export_service.execute_scheduled_export(
        db_session,
        schedule_task_id=str(schedule.id),
        module="subscribers",
        selected_fields=["email"],
        delimiter=",",
        export_format="csv",
        date_from=None,
        date_to=None,
        status=None,
        include_headers=True,
        recipient_email="ops@example.com",
    )

    db_session.refresh(schedule)
    assert result["sent"] is True
    assert result["rows"] >= 1
    assert schedule.last_run_at is not None
    assert calls
    events = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "export_scheduled_run")
        .all()
    )
    assert events


def test_count_rows_with_status_filter(db_session):
    active = Subscriber(
        first_name="Count",
        last_name="Active",
        email="count-active@example.com",
        status=SubscriberStatus.active,
    )
    suspended = Subscriber(
        first_name="Count",
        last_name="Suspended",
        email="count-suspended@example.com",
        status=SubscriberStatus.suspended,
    )
    db_session.add(active)
    db_session.add(suspended)
    db_session.commit()

    total = export_service.count_rows(db_session, module="subscribers")
    suspended_only = export_service.count_rows(
        db_session,
        module="subscribers",
        status="suspended",
    )
    assert total >= 2
    assert suspended_only >= 1


def test_process_export_job_writes_file_and_marks_completed(db_session, monkeypatch):
    subscriber = Subscriber(
        first_name="Big",
        last_name="Export",
        email="big-export@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()

    job = export_service.create_export_job(
        db_session,
        module="subscribers",
        selected_fields=["email"],
        delimiter=",",
        export_format="csv",
        date_from=None,
        date_to=None,
        status=None,
        include_headers=True,
        recipient_email="ops@example.com",
        requested_by_email="ops@example.com",
        row_count=12001,
    )
    monkeypatch.setattr(export_service.email_service, "send_email", lambda **kwargs: True)
    result = export_service.process_export_job(db_session, job_id=str(job["id"]))

    assert result["status"] == "completed"
    file_path = Path(str(result["file_path"]))
    assert file_path.exists()
    assert file_path.read_bytes()
    events = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "export_job_completed")
        .all()
    )
    assert events
