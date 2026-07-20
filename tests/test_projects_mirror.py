"""Local project/installation mirror service: reconcile, read, webhook events."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from app.models.project_mirror import ProjectMirror, ProjectSyncState
from app.models.subscriber import Subscriber
from app.services import projects_mirror


def _subscriber(db, crm_id: uuid.UUID | None = None) -> Subscriber:
    sub = Subscriber(
        first_name="Cust",
        last_name="Omer",
        email=f"c-{uuid.uuid4().hex[:8]}@example.com",
        crm_subscriber_id=crm_id,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _crm_resp():
    return {
        "projects": [
            {
                "id": "p1",
                "name": "Fiber install — 12 Test St",
                "status": "active",
                "project_type": "fiber_optics_installation",
                "progress_pct": 50,
                "current_stage": "Drop Cable Installation",
                "stages": [
                    {"key": "project_plan", "title": "Project Plan", "status": "done"},
                    {
                        "key": "project_survey",
                        "title": "Project Survey",
                        "status": "done",
                    },
                    {
                        "key": "drop_cable_installation",
                        "title": "Drop Cable Installation",
                        "status": "in_progress",
                    },
                ],
                "customer_address": "12 Test St",
                "region": "Abuja",
                "created_at": "2026-06-20T09:00:00+00:00",
            }
        ],
        "total": 1,
    }


# ── reconcile (pull) ─────────────────────────────────────────────────────────


def test_reconcile_upserts_projects_and_sync_state(db_session):
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    client = MagicMock()
    client.get_portal_projects.return_value = _crm_resp()
    with (
        patch("app.services.projects_mirror.get_crm_client", return_value=client),
        patch(
            "app.services.projects_mirror.resolve_crm_subscriber_id",
            return_value="crm-1",
        ),
    ):
        ok = projects_mirror.reconcile_subscriber(db_session, str(sub.id))
    assert ok is True
    row = db_session.query(ProjectMirror).filter_by(crm_project_id="p1").one()
    assert row.status == "active"
    assert row.progress_pct == 50
    assert row.current_stage == "Drop Cable Installation"
    assert len(row.stages) == 3
    assert db_session.get(ProjectSyncState, sub.id) is not None


def test_reconcile_noops_when_not_linked(db_session):
    sub = _subscriber(db_session, crm_id=None)
    with patch(
        "app.services.projects_mirror.resolve_crm_subscriber_id", return_value=None
    ):
        assert projects_mirror.reconcile_subscriber(db_session, str(sub.id)) is False


# ── read (lazy refresh) ──────────────────────────────────────────────────────


def test_read_builds_payload_and_counts_active(db_session):
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    client = MagicMock()
    client.get_portal_projects.return_value = _crm_resp()
    with (
        patch("app.services.projects_mirror.get_crm_client", return_value=client),
        patch(
            "app.services.projects_mirror.resolve_crm_subscriber_id",
            return_value="crm-1",
        ),
    ):
        out = projects_mirror.read_for_subscriber(db_session, str(sub.id))
    assert out["total"] == 1
    assert out["active"] == 1
    p = out["projects"][0]
    assert p["id"] == "p1"
    assert p["progress_pct"] == 50
    assert p["stages"][0]["title"] == "Project Plan"


def test_read_serves_mirror_when_crm_unreachable(db_session):
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    from app.services.crm_client import CRMClientError

    with patch(
        "app.services.projects_mirror.reconcile_subscriber",
        side_effect=CRMClientError("down"),
    ):
        out = projects_mirror.read_for_subscriber(db_session, str(sub.id))
    assert out["total"] == 0


# ── webhook application ───────────────────────────────────────────────────────


def test_webhook_project_created_upserts_via_subscriber_id(db_session):
    sub = _subscriber(db_session)
    out = projects_mirror.apply_webhook(
        db_session,
        "project.created",
        {
            "subscriber_id": str(sub.id),
            "project_id": "p9",
            "name": "New install",
            "status": "open",
        },
    )
    assert out["status"] == "ok"
    row = db_session.query(ProjectMirror).filter_by(crm_project_id="p9").one()
    assert row.status == "open"
    assert row.subscriber_id == sub.id


def test_webhook_project_completed_sets_status(db_session):
    crm_id = uuid.uuid4()
    sub = _subscriber(db_session, crm_id=crm_id)
    projects_mirror.apply_webhook(
        db_session,
        "project.created",
        {"subscriber_id": str(sub.id), "project_id": "p9", "status": "active"},
    )
    out = projects_mirror.apply_webhook(
        db_session,
        "project.completed",
        {
            "crm_subscriber_id": str(crm_id),
            "project_id": "p9",
            "to_status": "completed",
        },
    )
    assert out["status"] == "ok"
    row = db_session.query(ProjectMirror).filter_by(crm_project_id="p9").one()
    assert row.status == "completed"
    assert row.completed_at is not None


def test_webhook_project_completed_pushes(db_session):
    sub = _subscriber(db_session)
    with patch("app.services.push.send_push") as push:
        out = projects_mirror.apply_webhook(
            db_session,
            "project.completed",
            {"subscriber_id": str(sub.id), "project_id": "p9", "status": "completed"},
        )
    assert out["status"] == "ok"
    push.assert_called_once()


def test_webhook_project_updated_does_not_push(db_session):
    sub = _subscriber(db_session)
    with patch("app.services.push.send_push") as push:
        projects_mirror.apply_webhook(
            db_session,
            "project.updated",
            {"subscriber_id": str(sub.id), "project_id": "p9", "status": "active"},
        )
    push.assert_not_called()


def test_webhook_task_event_forces_refresh(db_session):
    sub = _subscriber(db_session)
    db_session.add(
        ProjectMirror(
            crm_project_id="p9", subscriber_id=sub.id, name="x", status="active"
        )
    )
    db_session.add(ProjectSyncState(subscriber_id=sub.id))
    db_session.commit()
    out = projects_mirror.apply_webhook(
        db_session,
        "project_task.completed",
        {"project_id": "p9", "title": "Drop cable"},
    )
    assert out["status"] == "ok"
    sync = db_session.get(ProjectSyncState, sub.id)
    assert sync.synced_at.year == 1970  # forced stale → next read refreshes


def test_webhook_unmapped_ignored(db_session):
    out = projects_mirror.apply_webhook(
        db_session,
        "project.created",
        {"subscriber_id": str(uuid.uuid4()), "project_id": "pX"},
    )
    assert out["reason"] == "unmapped_subscriber"


def test_webhook_task_event_unmirrored_ignored(db_session):
    out = projects_mirror.apply_webhook(
        db_session, "project_task.completed", {"project_id": "nope"}
    )
    assert out["reason"] == "project_not_mirrored"
