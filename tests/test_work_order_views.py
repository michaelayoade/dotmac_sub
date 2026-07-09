from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.subscriber import Subscriber
from app.models.work_order_mirror import WorkOrderMirror
from app.services import work_order_views


def _subscriber(db, **overrides) -> Subscriber:
    sub = Subscriber(
        first_name=overrides.pop("first_name", "Adaeze"),
        last_name=overrides.pop("last_name", "Nwosu"),
        email=overrides.pop("email", f"{uuid4().hex}@example.com"),
        phone=overrides.pop("phone", "08035550114"),
        account_number=overrides.pop("account_number", f"DM-{uuid4().hex[:6]}"),
        company_name=overrides.pop("company_name", None),
        **overrides,
    )
    db.add(sub)
    db.flush()
    return sub


def _work_order(db, subscriber: Subscriber, **overrides) -> WorkOrderMirror:
    row = WorkOrderMirror(
        crm_work_order_id=overrides.pop("crm_work_order_id", f"wo-{uuid4().hex[:8]}"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Fibre install"),
        status=overrides.pop("status", "scheduled"),
        work_type=overrides.pop("work_type", "install"),
        priority=overrides.pop("priority", "normal"),
        address=overrides.pop("address", "Plot 14, Jabi District"),
        scheduled_start=overrides.pop(
            "scheduled_start", datetime.now(UTC) + timedelta(hours=2)
        ),
        **overrides,
    )
    db.add(row)
    return row


def test_list_work_orders_filters_and_formats_internal_fields(db_session):
    subscriber = _subscriber(db_session, company_name="Adaeze Home")
    mine = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-match",
        status="dispatched",
        priority="high",
        crm_ticket_id="ticket-1",
        crm_project_id="project-1",
        assigned_to_crm_person_id="person-1",
        assigned_to_name="Ade Tech",
        required_skills=["fiber"],
        tags=["customer-facing"],
        access_notes="Call on arrival",
        metadata_={"source": "crm"},
    )
    other_sub = _subscriber(db_session)
    _work_order(db_session, other_sub, crm_work_order_id="wo-other", status="scheduled")
    db_session.commit()

    out = work_order_views.list_work_orders(
        db_session,
        work_order_views.WorkOrderListFilters(
            status="dispatched",
            q="Adaeze",
            limit=10,
        ),
    )

    assert out["total"] == 1
    assert out["summary"]["open"] == 1
    item = out["work_orders"][0]
    assert item["id"] == "wo-match"
    assert item["account_id"] == str(subscriber.id)
    assert item["account_name"] == "Adaeze Home"
    assert item["crm_ticket_id"] == "ticket-1"
    assert item["crm_project_id"] == "project-1"
    assert item["assigned_to_crm_person_id"] == "person-1"
    assert item["technician_name"] == "Ade Tech"
    assert item["required_skills"] == ["fiber"]
    assert item["tags"] == ["customer-facing"]
    assert item["access_notes"] == "Call on arrival"
    assert item["metadata"] == {"source": "crm"}
    assert mine.id is not None


def test_summary_counts_terminal_and_overdue(db_session):
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        status="in_progress",
        scheduled_start=datetime.now(UTC) - timedelta(hours=1),
    )
    _work_order(db_session, subscriber, status="completed")
    _work_order(db_session, subscriber, status="canceled")
    db_session.commit()

    summary = work_order_views.summary(db_session)

    assert summary["total"] == 3
    assert summary["open"] == 1
    assert summary["terminal"] == 2
    assert summary["overdue"] == 1
    assert summary["by_status"]["in_progress"] == 1


def test_get_work_order_returns_none_for_missing_and_detail_for_match(db_session):
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-detail")
    db_session.commit()

    assert work_order_views.get_work_order(db_session, "missing") is None
    item = work_order_views.get_work_order(db_session, "wo-detail")
    assert item is not None
    assert item["id"] == "wo-detail"
    assert item["account_email"] == subscriber.email


def test_options_are_distinct_sorted_values(db_session):
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, status="scheduled", priority="low")
    _work_order(db_session, subscriber, status="completed", priority="high")
    db_session.commit()

    out = work_order_views.options(db_session)

    assert out["statuses"] == ["completed", "scheduled"]
    assert out["priorities"] == ["high", "low"]
    assert out["work_types"] == ["install"]
