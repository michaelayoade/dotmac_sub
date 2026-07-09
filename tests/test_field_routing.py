from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order_mirror import WorkOrderMirror
from app.services.auth_dependencies import require_user_auth
from app.services.field.routing import field_routing


def _user(db_session, name: str = "Route") -> SystemUser:
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


def _auth(user: SystemUser) -> dict:
    return {
        "principal_id": str(user.id),
        "person_id": str(user.id),
        "subscriber_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }


def _profile(db_session, user: SystemUser, crm_person_id: str = "crm-route-tech"):
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=crm_person_id,
        title="Installer",
        region="Jabi",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    sub = Subscriber(
        first_name="Route",
        last_name="Customer",
        email=f"route-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(sub)
    db_session.flush()
    return sub


def _work_order(db_session, subscriber: Subscriber, **overrides) -> WorkOrderMirror:
    row = WorkOrderMirror(
        crm_work_order_id=overrides.pop("crm_work_order_id"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Field job"),
        status=overrides.pop("status", "dispatched"),
        assigned_to_crm_person_id=overrides.pop(
            "assigned_to_crm_person_id", "crm-route-tech"
        ),
        address=overrides.pop("address", "Jabi"),
        scheduled_start=overrides.pop("scheduled_start", datetime.now(UTC)),
        metadata_=overrides.pop("metadata_", None),
        **overrides,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_day_route_orders_by_nearest_neighbour_and_keeps_unlocated_last(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    far = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-far",
        title="Far job",
        metadata_={"location": {"lat": 9.09, "lng": 7.49}},
    )
    near = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-near",
        title="Near job",
        metadata_={"location": {"lat": 9.071, "lng": 7.451}},
    )
    unlocated = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-unlocated",
        title="No coordinates",
        metadata_={},
    )
    db_session.commit()

    route = field_routing.order_day_route(
        db_session,
        _auth(user),
        start_latitude=9.0709,
        start_longitude=7.4509,
    )

    assert [stop["work_order_id"] for stop in route] == [
        near.crm_work_order_id,
        far.crm_work_order_id,
        unlocated.crm_work_order_id,
    ]
    assert [stop["sequence"] for stop in route] == [1, 2, 3]
    assert route[0]["work_order_mirror_id"] == near.id
    assert route[-1]["distance_km"] is None


def test_day_route_excludes_other_technicians_and_terminal_jobs(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-visible",
        metadata_={"location": {"lat": 9.071, "lng": 7.451}},
    )
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-done",
        status="completed",
        metadata_={"location": {"lat": 9.072, "lng": 7.452}},
    )
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-hidden",
        assigned_to_crm_person_id="other-tech",
        metadata_={"location": {"lat": 9.073, "lng": 7.453}},
    )
    db_session.commit()

    route = field_routing.order_day_route(
        db_session,
        _auth(user),
        start_latitude=9.0709,
        start_longitude=7.4509,
    )

    assert [stop["work_order_id"] for stop in route] == ["wo-visible"]


def test_day_route_api(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-route-api",
        metadata_={"location": {"lat": 9.071, "lng": 7.451}},
    )
    db_session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)

    resp = TestClient(app).get(
        "/api/v1/field/locations/route",
        params={"start_lat": 9.0709, "start_lng": 7.4509},
    )

    assert resp.status_code == 200
    assert resp.json()["route"][0]["work_order_id"] == "wo-route-api"
