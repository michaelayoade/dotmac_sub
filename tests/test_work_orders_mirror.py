"""Local work-order/field-service mirror service: reconcile, read, webhooks."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from app.models.subscriber import Subscriber
from app.models.work_order_mirror import WorkOrderMirror, WorkOrderSyncState
from app.services import work_orders_mirror


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
        "work_orders": [
            {
                "id": "wo1",
                "title": "Fault repair — no signal",
                "status": "dispatched",
                "work_type": "repair",
                "priority": "high",
                "technician_name": "Ade Tech",
                "technician_phone": "+2348000000000",
                "address": "12 Test St",
                "scheduled_start": "2026-06-30T09:00:00+00:00",
                "estimated_arrival_at": "2026-06-30T09:30:00+00:00",
                "estimated_duration_minutes": 60,
                "started_at": "2026-06-30T09:32:00+00:00",
                "total_active_seconds": 120,
                "ticket_id": "ticket-1",
                "project_id": "project-1",
                "required_skills": ["fiber"],
                "tags": ["customer-facing"],
                "access_notes": "Call on arrival",
                "created_at": "2026-06-29T10:00:00+00:00",
            }
        ],
        "total": 1,
    }


def test_reconcile_upserts_and_marks_synced(db_session):
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    client = MagicMock()
    client.get_portal_work_orders.return_value = _crm_resp()
    with (
        patch("app.services.work_orders_mirror.get_crm_client", return_value=client),
        patch(
            "app.services.work_orders_mirror.resolve_crm_subscriber_id",
            return_value="crm-1",
        ),
    ):
        ok = work_orders_mirror.reconcile_subscriber(db_session, str(sub.id))
    assert ok is True
    row = db_session.query(WorkOrderMirror).filter_by(crm_work_order_id="wo1").one()
    assert row.status == "dispatched"
    assert row.technician_name == "Ade Tech"
    assert row.estimated_duration_minutes == 60
    assert row.started_at is not None
    assert row.total_active_seconds == 120
    assert row.crm_ticket_id == "ticket-1"
    assert row.crm_project_id == "project-1"
    assert row.required_skills == ["fiber"]
    assert row.tags == ["customer-facing"]
    assert row.access_notes == "Call on arrival"
    assert db_session.get(WorkOrderSyncState, sub.id) is not None


def test_read_counts_upcoming_and_excludes_terminal(db_session):
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    client = MagicMock()
    client.get_portal_work_orders.return_value = _crm_resp()
    with (
        patch("app.services.work_orders_mirror.get_crm_client", return_value=client),
        patch(
            "app.services.work_orders_mirror.resolve_crm_subscriber_id",
            return_value="crm-1",
        ),
    ):
        out = work_orders_mirror.read_for_subscriber(db_session, str(sub.id))
    assert out["total"] == 1
    assert out["upcoming"] == 1
    assert out["work_orders"][0]["technician_name"] == "Ade Tech"
    assert out["work_orders"][0]["started_at"] is not None
    assert out["work_orders"][0]["total_active_seconds"] == 120


def test_read_serves_mirror_when_crm_unreachable(db_session):
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    from app.services.crm_client import CRMClientError

    with patch(
        "app.services.work_orders_mirror.reconcile_subscriber",
        side_effect=CRMClientError("down"),
    ):
        out = work_orders_mirror.read_for_subscriber(db_session, str(sub.id))
    assert out["total"] == 0


def test_webhook_dispatched_upserts_and_pushes(db_session):
    sub = _subscriber(db_session)
    with patch("app.services.push.send_push") as push:
        out = work_orders_mirror.apply_webhook(
            db_session,
            "work_order.dispatched",
            {
                "subscriber_id": str(sub.id),
                "work_order_id": "wo9",
                "title": "Install",
                "status": "dispatched",
                "technician_name": "Ade Tech",
                "address": "12 Test St",
                "started_at": "2026-06-30T09:32:00+00:00",
                "ticket_id": "ticket-1",
            },
        )
    assert out["status"] == "ok"
    push.assert_called_once()
    row = db_session.query(WorkOrderMirror).filter_by(crm_work_order_id="wo9").one()
    assert row.status == "dispatched"
    assert row.technician_name == "Ade Tech"
    assert row.address == "12 Test St"
    assert row.started_at is not None
    assert row.crm_ticket_id == "ticket-1"


def test_webhook_completed_sets_completed_at(db_session):
    crm_id = uuid.uuid4()
    sub = _subscriber(db_session, crm_id=crm_id)
    work_orders_mirror.apply_webhook(
        db_session,
        "work_order.created",
        {"subscriber_id": str(sub.id), "work_order_id": "wo9", "status": "scheduled"},
    )
    with patch("app.services.push.send_push"):
        out = work_orders_mirror.apply_webhook(
            db_session,
            "work_order.completed",
            {
                "crm_subscriber_id": str(crm_id),
                "work_order_id": "wo9",
                "to_status": "completed",
            },
        )
    assert out["status"] == "ok"
    row = db_session.query(WorkOrderMirror).filter_by(crm_work_order_id="wo9").one()
    assert row.status == "completed"
    assert row.completed_at is not None


def test_webhook_started_pushes_track_deeplink(db_session):
    # The dispatched → in_progress transition ("tech started") pushes an
    # on-the-way notice deep-linked to the live map.
    sub = _subscriber(db_session)
    work_orders_mirror.apply_webhook(
        db_session,
        "work_order.created",
        {"subscriber_id": str(sub.id), "work_order_id": "wo9", "status": "dispatched"},
    )
    with patch("app.services.push.send_push") as push:
        out = work_orders_mirror.apply_webhook(
            db_session,
            "work_order.updated",
            {
                "subscriber_id": str(sub.id),
                "work_order_id": "wo9",
                "to_status": "in_progress",
            },
        )
    assert out["status"] == "ok"
    push.assert_called_once()
    kwargs = push.call_args.kwargs
    assert kwargs["title"] == "Your technician is on the way"
    assert kwargs["data"]["route"] == "/track/wo9"


def test_webhook_no_repush_while_in_progress(db_session):
    sub = _subscriber(db_session)
    with patch("app.services.push.send_push") as push:
        work_orders_mirror.apply_webhook(
            db_session,
            "work_order.updated",
            {
                "subscriber_id": str(sub.id),
                "work_order_id": "wo9",
                "to_status": "in_progress",
            },
        )
        assert push.call_count == 1
        # A second update while already in_progress must not re-push.
        work_orders_mirror.apply_webhook(
            db_session,
            "work_order.updated",
            {
                "subscriber_id": str(sub.id),
                "work_order_id": "wo9",
                "to_status": "in_progress",
                "technician_name": "Ade",
            },
        )
        assert push.call_count == 1


def test_webhook_unmapped_ignored(db_session):
    out = work_orders_mirror.apply_webhook(
        db_session,
        "work_order.created",
        {"subscriber_id": str(uuid.uuid4()), "work_order_id": "woX"},
    )
    assert out["reason"] == "unmapped_subscriber"


def test_webhook_unknown_event_ignored(db_session):
    sub = _subscriber(db_session)
    out = work_orders_mirror.apply_webhook(
        db_session,
        "work_order.archived",
        {"subscriber_id": str(sub.id), "work_order_id": "wo9"},
    )
    assert out["status"] == "ignored"


def test_stale_read_serves_stale_and_refreshes_async(db_session):
    """A warm-but-stale read serves the mirror immediately and refreshes in the
    background instead of blocking on the CRM (P3-sub)."""
    from datetime import UTC, datetime, timedelta

    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    db_session.add(
        WorkOrderMirror(
            subscriber_id=sub.id,
            crm_work_order_id="wo-stale",
            title="Old",
            status="scheduled",
        )
    )
    db_session.add(
        WorkOrderSyncState(
            subscriber_id=sub.id, synced_at=datetime.now(UTC) - timedelta(hours=1)
        )
    )
    db_session.commit()

    enqueued: list = []
    with (
        patch("app.services.work_orders_mirror.reconcile_subscriber") as recon,
        patch(
            "app.services.queue_adapter.enqueue_task",
            side_effect=lambda *a, **k: enqueued.append((a, k)),
        ),
    ):
        out = work_orders_mirror.read_for_subscriber(db_session, str(sub.id))

    recon.assert_not_called()  # did NOT block on the CRM
    assert len(enqueued) == 1  # enqueued a background refresh
    assert out["total"] == 1  # served the stale row immediately
    assert out["work_orders"][0]["id"] == "wo-stale"
    # synced_at optimistically bumped so concurrent reads don't re-enqueue (debounce)
    st = db_session.get(WorkOrderSyncState, sub.id)
    assert (datetime.now(UTC) - st.synced_at.replace(tzinfo=UTC)).total_seconds() < 60


def test_cold_read_stays_synchronous_and_does_not_enqueue(db_session):
    """A cold read (no local copy) fetches synchronously so the first load is
    populated — it must not return empty + defer."""
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    client = MagicMock()
    client.get_portal_work_orders.return_value = _crm_resp()
    with (
        patch("app.services.work_orders_mirror.get_crm_client", return_value=client),
        patch(
            "app.services.work_orders_mirror.resolve_crm_subscriber_id",
            return_value="crm-1",
        ),
        patch("app.services.queue_adapter.enqueue_task") as enq,
    ):
        out = work_orders_mirror.read_for_subscriber(db_session, str(sub.id))

    enq.assert_not_called()  # cold path fetched synchronously, no deferral
    assert out["total"] == 1  # populated on first load
