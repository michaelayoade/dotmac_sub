"""Local work-order/field-service mirror service: reconcile, read, webhooks."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.field_location import FieldTechPresence
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
                "assigned_to_person_id": "crm-tech-1",
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
    assert row.assigned_to_crm_person_id == "crm-tech-1"
    assert row.required_skills == ["fiber"]
    assert row.tags == ["customer-facing"]
    assert row.access_notes == "Call on arrival"
    assert db_session.get(WorkOrderSyncState, sub.id) is not None
    profile = (
        db_session.query(TechnicianProfile).filter_by(crm_person_id="crm-tech-1").one()
    )
    assert profile.system_user_id is None
    assert profile.metadata_["name"] == "Ade Tech"
    assert profile.metadata_["phone"] == "+2348000000000"


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
                "assigned_to_person_id": "crm-tech-9",
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
    assert row.assigned_to_crm_person_id == "crm-tech-9"
    assert row.address == "12 Test St"
    assert row.started_at is not None
    assert row.crm_ticket_id == "ticket-1"
    profile = (
        db_session.query(TechnicianProfile).filter_by(crm_person_id="crm-tech-9").one()
    )
    assert profile.person_id is not None
    assert profile.metadata_["source"] == "crm_work_order_sync"


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


def test_read_serves_local_only_when_pull_disabled(monkeypatch, db_session):
    """Phase 2 flip: crm.work_order_pull off -> never contact the CRM (no cold
    fetch, no lazy refresh) and serve whatever the local store holds."""
    monkeypatch.setenv("CRM_WORK_ORDER_PULL_ENABLED", "false")
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    db_session.add(
        WorkOrderMirror(
            subscriber_id=sub.id,
            crm_work_order_id="sub-native1",
            title="Native install",
            status="scheduled",
        )
    )
    db_session.commit()

    # Cold cache (no sync state) would normally fetch synchronously.
    with (
        patch("app.services.work_orders_mirror.reconcile_subscriber") as recon,
        patch("app.services.queue_adapter.enqueue_task") as enq,
    ):
        out = work_orders_mirror.read_for_subscriber(db_session, str(sub.id))

    recon.assert_not_called()
    enq.assert_not_called()
    assert out["total"] == 1
    assert out["work_orders"][0]["id"] == "sub-native1"


def test_upsert_protects_native_field_activity_from_crm_clobber(db_session):
    """Reconcile-clobber protection: a CRM payload must not overwrite status or
    activity timestamps on a row sub's field services own — harmless header
    fields still merge and the native metadata marker survives."""
    started = datetime(2026, 7, 9, 9, 0, tzinfo=UTC)
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    db_session.add(
        WorkOrderMirror(
            subscriber_id=sub.id,
            crm_work_order_id="wo-native",
            title="Repair",
            status="in_progress",
            started_at=started,
            total_active_seconds=600,
            metadata_={
                "native_field_source": "sub",
                "native_field_activity": {"start": {"source": "sub"}},
            },
        )
    )
    db_session.commit()

    with (
        patch(
            "app.services.work_orders_mirror.get_crm_client",
            return_value=MagicMock(
                get_portal_work_orders=MagicMock(
                    return_value={
                        "work_orders": [
                            {
                                "id": "wo-native",
                                "title": "Repair (CRM title)",
                                "status": "scheduled",
                                "address": "12 New St",
                                "started_at": None,
                                "completed_at": "2026-07-09T10:00:00+00:00",
                                "total_active_seconds": 0,
                                "metadata": {"crm_only_key": "x"},
                            }
                        ]
                    }
                )
            ),
        ),
        patch(
            "app.services.work_orders_mirror.resolve_crm_subscriber_id",
            return_value="crm-1",
        ),
    ):
        work_orders_mirror.reconcile_subscriber(db_session, str(sub.id))

    row = (
        db_session.query(WorkOrderMirror).filter_by(crm_work_order_id="wo-native").one()
    )
    # Protected: status + activity timestamps stay sub-owned.
    assert row.status == "in_progress"
    assert row.started_at.replace(tzinfo=UTC) == started
    assert row.completed_at is None
    assert row.total_active_seconds == 600
    # Harmless header fields merge; native metadata survives a CRM metadata blob.
    assert row.title == "Repair (CRM title)"
    assert row.address == "12 New St"
    assert row.metadata_["native_field_source"] == "sub"
    assert row.metadata_["native_field_activity"] == {"start": {"source": "sub"}}
    assert row.metadata_["crm_only_key"] == "x"


def test_webhook_does_not_clobber_or_notify_on_native_row(db_session):
    """A CRM echo for a natively-run work order neither applies status nor
    re-pushes lifecycle notifications (the field transitions own that)."""
    sub = _subscriber(db_session)
    db_session.add(
        WorkOrderMirror(
            subscriber_id=sub.id,
            crm_work_order_id="wo-echo",
            title="Repair",
            status="completed",
            metadata_={"native_field_source": "sub"},
        )
    )
    db_session.commit()

    with patch("app.services.push.send_push") as push:
        out = work_orders_mirror.apply_webhook(
            db_session,
            "work_order.updated",
            {
                "subscriber_id": str(sub.id),
                "work_order_id": "wo-echo",
                "to_status": "in_progress",
            },
        )

    assert out == {
        "status": "ok",
        "event": "work_order.updated",
        "native_precedence": True,
    }
    push.assert_not_called()
    row = db_session.query(WorkOrderMirror).filter_by(crm_work_order_id="wo-echo").one()
    assert row.status == "completed"


def test_upsert_protects_sub_prefixed_rows_without_marker(db_session):
    """Native rows are recognizable by their ``sub-`` public id alone (born via
    dispatch.work_order_headers.create) even without the metadata marker."""
    sub = _subscriber(db_session)
    db_session.add(
        WorkOrderMirror(
            subscriber_id=sub.id,
            crm_work_order_id="sub-abc123",
            title="Native",
            status="in_progress",
        )
    )
    db_session.commit()

    with patch("app.services.push.send_push"):
        work_orders_mirror.apply_webhook(
            db_session,
            "work_order.canceled",
            {
                "subscriber_id": str(sub.id),
                "work_order_id": "sub-abc123",
                "status": "canceled",
            },
        )

    row = (
        db_session.query(WorkOrderMirror)
        .filter_by(crm_work_order_id="sub-abc123")
        .one()
    )
    assert row.status == "in_progress"


def test_technician_location_uses_native_presence_for_owned_work_order(db_session):
    sub = _subscriber(db_session)
    profile = TechnicianProfile(
        person_id=uuid.uuid4(),
        crm_person_id="crm-tech-live",
        title="Field technician",
    )
    row = WorkOrderMirror(
        subscriber_id=sub.id,
        crm_work_order_id="wo-live",
        title="Repair",
        status="in_progress",
        assigned_to_crm_person_id="crm-tech-live",
        estimated_arrival_at=datetime(2026, 7, 9, 12, 0, tzinfo=UTC),
    )
    db_session.add_all([profile, row])
    db_session.flush()
    db_session.add(
        FieldTechPresence(
            technician_id=profile.id,
            person_id=profile.person_id,
            status="busy",
            location_sharing_enabled=True,
            last_latitude=9.0765,
            last_longitude=7.3986,
            last_location_accuracy_m=14.5,
            last_location_at=datetime(2026, 7, 9, 11, 45, tzinfo=UTC),
        )
    )
    db_session.commit()

    out = work_orders_mirror.technician_location(db_session, str(sub.id), "wo-live")

    assert out["available"] is True
    assert out["latitude"] == 9.0765
    assert out["longitude"] == 7.3986
    assert out["accuracy_m"] == 14.5
    assert out["estimated_arrival_at"] == "2026-07-09T12:00:00+00:00"


def test_technician_location_uses_native_dispatch_assignment(db_session):
    sub = _subscriber(db_session)
    profile = TechnicianProfile(
        person_id=uuid.uuid4(),
        title="Field technician",
    )
    row = WorkOrderMirror(
        subscriber_id=sub.id,
        crm_work_order_id="wo-dispatch-live",
        title="Repair",
        status="in_progress",
    )
    db_session.add_all([profile, row])
    db_session.flush()
    db_session.add_all(
        [
            WorkOrderAssignmentQueue(
                work_order_mirror_id=row.id,
                crm_work_order_id=row.crm_work_order_id,
                status="assigned",
                assigned_technician_id=profile.id,
            ),
            FieldTechPresence(
                technician_id=profile.id,
                person_id=profile.person_id,
                status="busy",
                location_sharing_enabled=True,
                last_latitude=9.01,
                last_longitude=7.02,
                last_location_at=datetime(2026, 7, 9, 11, 45, tzinfo=UTC),
            ),
        ]
    )
    db_session.commit()

    out = work_orders_mirror.technician_location(
        db_session, str(sub.id), "wo-dispatch-live"
    )

    assert out["available"] is True
    assert out["latitude"] == 9.01
    assert out["longitude"] == 7.02


def test_technician_location_hidden_when_work_order_not_active(db_session):
    sub = _subscriber(db_session)
    db_session.add(
        WorkOrderMirror(
            subscriber_id=sub.id,
            crm_work_order_id="wo-scheduled",
            title="Install",
            status="scheduled",
        )
    )
    db_session.commit()

    out = work_orders_mirror.technician_location(
        db_session, str(sub.id), "wo-scheduled"
    )

    assert out == {
        "available": False,
        "reason": "not_active",
        "work_order_id": "wo-scheduled",
    }


def test_rate_technician_stores_local_metadata_and_is_idempotent(db_session):
    sub = _subscriber(db_session)
    row = WorkOrderMirror(
        subscriber_id=sub.id,
        crm_work_order_id="wo-rate",
        title="Repair",
        status="completed",
    )
    db_session.add(row)
    db_session.commit()

    first = work_orders_mirror.rate_technician(
        db_session,
        str(sub.id),
        "wo-rate",
        rating=5,
        comment="Great work",
    )
    db_session.refresh(row)
    second = work_orders_mirror.rate_technician(
        db_session,
        str(sub.id),
        "wo-rate",
        rating=2,
        comment="changed",
    )

    assert first == {
        "ok": True,
        "already_rated": False,
        "rating": 5,
        "work_order_id": "wo-rate",
    }
    assert second == {
        "ok": True,
        "already_rated": True,
        "rating": 5,
        "work_order_id": "wo-rate",
    }
    assert row.metadata_["technician_rating"]["comment"] == "Great work"
    assert row.metadata_["technician_rating"]["source"] == "sub_portal"


def test_rate_technician_rejects_incomplete_work_order(db_session):
    sub = _subscriber(db_session)
    db_session.add(
        WorkOrderMirror(
            subscriber_id=sub.id,
            crm_work_order_id="wo-open",
            title="Install",
            status="in_progress",
        )
    )
    db_session.commit()

    with pytest.raises(ValueError, match="work_order_not_completed"):
        work_orders_mirror.rate_technician(db_session, str(sub.id), "wo-open", rating=5)
