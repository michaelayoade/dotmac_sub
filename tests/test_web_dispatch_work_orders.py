from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.schemas.dispatch import TechnicianProfileCreate
from app.services import dispatch
from app.services import web_dispatch_work_orders as web_dispatch


def _subscriber(db_session, *, first_name: str = "Adaeze") -> Subscriber:
    sub = Subscriber(
        first_name=first_name,
        last_name="Nwosu",
        email=f"dispatch-{uuid4().hex[:8]}@example.com",
        account_number=f"DM{uuid4().hex[:6].upper()}",
    )
    db_session.add(sub)
    db_session.commit()
    return sub


def _technician(db_session):
    user = SystemUser(
        first_name="Ade",
        last_name="Tech",
        email=f"tech-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.commit()
    return dispatch.technician_profiles.create(
        db_session, TechnicianProfileCreate(system_user_id=user.id, region="Jabi")
    )


def test_list_page_counts_filters_and_options(db_session):
    sub = _subscriber(db_session)
    _technician(db_session)
    web_dispatch.create_from_form(
        db_session,
        {
            "public_id": "sub-web-wo-1",
            "subscriber_id": str(sub.id),
            "title": "Fibre install",
            "status": "scheduled",
            "priority": "high",
            "work_type": "install",
            "address": "Plot 14, Jabi",
            "required_skills": "fiber, splicing",
            "tags": "native, install",
        },
    )

    page = web_dispatch.list_page(
        db_session, status="scheduled", q="Jabi", page=1, per_page=25
    )

    assert page["total"] == 1
    assert page["counts"]["scheduled"] >= 1
    assert page["items"][0]["work_order"].crm_work_order_id == "sub-web-wo-1"
    assert page["items"][0]["subscriber_label"]
    assert page["status_filter"] == "scheduled"
    assert page["subscriber_options"]
    assert page["technician_options"]


def test_update_and_queue_from_form(db_session):
    sub = _subscriber(db_session)
    tech = _technician(db_session)
    web_dispatch.create_from_form(
        db_session,
        {
            "public_id": "sub-web-wo-2",
            "subscriber_id": str(sub.id),
            "title": "Router swap",
            "status": "scheduled",
        },
    )

    updated = web_dispatch.update_from_form(
        db_session,
        "sub-web-wo-2",
        {
            "title": "Router swap & test",
            "status": "dispatched",
            "priority": "normal",
            "work_type": "repair",
            "assigned_to_name": "Ade Tech",
            "scheduled_start": "2026-07-09T09:00",
            "scheduled_end": "2026-07-09T10:00",
            "estimated_duration_minutes": "60",
        },
    )
    queued = web_dispatch.queue_assignment_from_form(
        db_session,
        "sub-web-wo-2",
        {
            "assigned_technician_id": str(tech.id),
            "status": "assigned",
            "reason": "Morning route",
        },
    )

    assert updated.status == "dispatched"
    assert updated.assigned_to_name == "Ade Tech"
    assert updated.scheduled_start is not None
    assert queued.crm_work_order_id == "sub-web-wo-2"
    assert queued.status == "assigned"


def test_queue_assignment_requires_technician(db_session):
    sub = _subscriber(db_session)
    web_dispatch.create_from_form(
        db_session,
        {
            "public_id": "sub-web-wo-3",
            "subscriber_id": str(sub.id),
            "title": "Install",
            "status": "scheduled",
        },
    )

    with pytest.raises(HTTPException) as exc:
        web_dispatch.queue_assignment_from_form(
            db_session,
            "sub-web-wo-3",
            {"assigned_technician_id": "", "status": "assigned"},
        )
    assert exc.value.status_code == 422
