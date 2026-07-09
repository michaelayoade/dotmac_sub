from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.network import FdhCabinet, FiberAccessPoint, FiberSpliceClosure
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order_mirror import WorkOrderMirror
from app.services.field.jobs import field_jobs


def _auth(user: SystemUser) -> dict:
    return {
        "principal_id": str(user.id),
        "person_id": str(user.id),
        "subscriber_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }


def _user(db_session, name: str = "Ade") -> SystemUser:
    user = SystemUser(
        first_name=name,
        last_name="Tech",
        display_name=f"{name} Tech",
        email=f"{name.lower()}-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _profile(db_session, user: SystemUser, **overrides) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=overrides.pop("person_id", user.id),
        system_user_id=overrides.pop("system_user_id", user.id),
        crm_person_id=overrides.pop("crm_person_id", f"crm-person-{uuid4().hex[:8]}"),
        title=overrides.pop("title", "Installer"),
        region=overrides.pop("region", "Jabi"),
        **overrides,
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    sub = Subscriber(
        first_name="Adaeze",
        last_name="Nwosu",
        email=f"adaeze-{uuid4().hex[:8]}@example.com",
        phone="08035550114",
        account_number=f"DM-{uuid4().hex[:6]}",
    )
    db_session.add(sub)
    db_session.flush()
    return sub


def _work_order(db_session, subscriber: Subscriber, **overrides) -> WorkOrderMirror:
    row = WorkOrderMirror(
        crm_work_order_id=overrides.pop("crm_work_order_id", f"wo-{uuid4().hex[:8]}"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Fibre install"),
        description=overrides.pop("description", "4-drop"),
        status=overrides.pop("status", "dispatched"),
        work_type=overrides.pop("work_type", "install"),
        priority=overrides.pop("priority", "high"),
        address=overrides.pop("address", "Plot 14, Jabi District"),
        scheduled_start=overrides.pop(
            "scheduled_start", datetime.now(UTC) - timedelta(hours=1)
        ),
        tags=overrides.pop("tags", ["customer-facing"]),
        **overrides,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_field_jobs_scope_by_crm_person_and_assignment_queue(db_session):
    user = _user(db_session)
    profile = _profile(db_session, user, crm_person_id="crm-tech-1")
    other_user = _user(db_session, "Other")
    _profile(db_session, other_user, crm_person_id="crm-tech-2")
    subscriber = _subscriber(db_session)
    assigned_by_crm = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-crm-assigned",
        assigned_to_crm_person_id="crm-tech-1",
    )
    assigned_by_queue = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-queue-assigned",
        assigned_to_crm_person_id="crm-tech-2",
    )
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-hidden",
        assigned_to_crm_person_id="crm-tech-2",
    )
    db_session.add(
        WorkOrderAssignmentQueue(
            work_order_mirror_id=assigned_by_queue.id,
            crm_work_order_id=assigned_by_queue.crm_work_order_id,
            assigned_technician_id=profile.id,
        )
    )
    db_session.commit()

    jobs = field_jobs.list(db_session, _auth(user))

    assert [job.id for job in jobs] == [
        assigned_by_crm.crm_work_order_id,
        assigned_by_queue.crm_work_order_id,
    ]
    assert {job.id for job in field_jobs.list(db_session, _auth(other_user))} == {
        "wo-queue-assigned",
        "wo-hidden",
    }


def test_field_job_detail_404_does_not_leak_unassigned_jobs(db_session):
    user = _user(db_session)
    _profile(db_session, user, crm_person_id="crm-tech-1")
    other_user = _user(db_session, "Other")
    _profile(db_session, other_user, crm_person_id="crm-tech-2")
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-hidden",
        assigned_to_crm_person_id="crm-tech-2",
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        field_jobs.get_detail(db_session, _auth(user), "wo-hidden")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Job not found"


def test_field_job_detail_returns_customer_and_location(db_session):
    user = _user(db_session)
    _profile(db_session, user, crm_person_id="crm-tech-1")
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-detail",
        crm_ticket_id="ticket-1",
        crm_project_id="project-1",
        assigned_to_crm_person_id="crm-tech-1",
        access_notes="Call on arrival",
        metadata_={"location": {"lat": 9.07, "lng": 7.49}},
    )
    db_session.commit()

    detail = field_jobs.get_detail(db_session, _auth(user), "wo-detail")

    assert detail.job.id == "wo-detail"
    assert detail.customer is not None
    assert detail.customer.name == "Adaeze Nwosu"
    assert detail.location.latitude == 9.07
    assert detail.location.longitude == 7.49
    assert detail.location.source == "cached"
    assert detail.ticket_ref == "ticket-1"
    assert detail.project_id == "project-1"
    assert detail.access_notes == "Call on arrival"


def test_field_job_destinations_include_customer_nearby_assets_and_other(db_session):
    user = _user(db_session)
    _profile(db_session, user, crm_person_id="crm-tech-1")
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-destinations",
        assigned_to_crm_person_id="crm-tech-1",
        metadata_={"location": {"lat": 9.071, "lng": 7.451}},
    )
    db_session.add_all(
        [
            FdhCabinet(
                name="FDH Jabi",
                code="FDH-JB",
                latitude=9.0711,
                longitude=7.4511,
            ),
            FiberSpliceClosure(
                name="Closure 14",
                latitude=9.0712,
                longitude=7.4512,
            ),
            FiberAccessPoint(
                name="NAP 4",
                code="NAP-4",
                latitude=9.0713,
                longitude=7.4513,
            ),
            FdhCabinet(
                name="Far FDH",
                latitude=9.2,
                longitude=7.6,
            ),
        ]
    )
    db_session.commit()

    destinations = field_jobs.list_destinations(
        db_session, _auth(user), "wo-destinations"
    )

    assert destinations[0] == {
        "destination_type": "customer",
        "destination_id": str(subscriber.id),
        "label": "Customer site",
        "latitude": 9.071,
        "longitude": 7.451,
        "address_text": "Plot 14, Jabi District",
    }
    assert [item["destination_type"] for item in destinations[1:-1]] == [
        "cabinet",
        "closure",
        "fiber_access_point",
    ]
    assert destinations[-1]["destination_type"] == "other"
    assert "Far FDH" not in {item["label"] for item in destinations}


def test_field_job_destinations_work_without_coordinates(db_session):
    user = _user(db_session)
    _profile(db_session, user, crm_person_id="crm-tech-1")
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-address-only",
        assigned_to_crm_person_id="crm-tech-1",
        metadata_={},
    )
    db_session.commit()

    destinations = field_jobs.list_destinations(
        db_session, _auth(user), "wo-address-only"
    )

    assert [item["destination_type"] for item in destinations] == ["customer", "other"]
    assert destinations[0]["latitude"] is None
    assert destinations[0]["address_text"] == "Plot 14, Jabi District"


def test_field_me_counts_open_jobs_and_completed_today(db_session):
    user = _user(db_session)
    _profile(db_session, user, crm_person_id="crm-tech-1")
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-open",
        status="dispatched",
        assigned_to_crm_person_id="crm-tech-1",
    )
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-done",
        status="completed",
        completed_at=datetime.now(UTC),
        assigned_to_crm_person_id="crm-tech-1",
    )
    db_session.commit()

    me = field_jobs.me(db_session, _auth(user))

    assert me.name == "Ade Tech"
    assert me.open_jobs == 1
    assert me.completed_today == 1
