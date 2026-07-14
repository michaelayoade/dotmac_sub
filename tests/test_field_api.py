from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile
from app.models.network import FdhCabinet
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order_mirror import WorkOrderMirror
from app.services.auth_dependencies import require_user_auth


def _client(db_session, auth: dict) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: auth
    return TestClient(app)


def _seed(db_session):
    user = SystemUser(
        first_name="Ade",
        last_name="Tech",
        display_name="Ade Tech",
        email=f"ade-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    sub = Subscriber(
        first_name="Adaeze",
        last_name="Nwosu",
        email=f"adaeze-{uuid4().hex[:8]}@example.com",
    )
    db_session.add_all([user, sub])
    db_session.flush()
    db_session.add(
        TechnicianProfile(
            person_id=user.id,
            system_user_id=user.id,
            crm_person_id="crm-tech-1",
            title="Installer",
            region="Jabi",
        )
    )
    db_session.add(
        WorkOrderMirror(
            crm_work_order_id="wo-field-api",
            subscriber_id=sub.id,
            title="Fibre install",
            status="dispatched",
            priority="high",
            work_type="install",
            assigned_to_crm_person_id="crm-tech-1",
            address="Plot 14, Jabi District",
            scheduled_start=datetime.now(UTC),
            metadata_={"location": {"lat": 9.071, "lng": 7.451}},
        )
    )
    db_session.add(
        FdhCabinet(
            name="FDH API",
            code="FDH-API",
            latitude=9.0711,
            longitude=7.4511,
        )
    )
    db_session.commit()
    auth = {
        "principal_id": str(user.id),
        "person_id": str(user.id),
        "subscriber_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }
    return auth


def test_field_api_me_jobs_and_detail(db_session):
    auth = _seed(db_session)
    client = _client(db_session, auth)

    me = client.get("/api/v1/field/me")
    assert me.status_code == 200
    assert me.json()["name"] == "Ade Tech"
    assert me.json()["open_jobs"] == 1

    jobs = client.get("/api/v1/field/jobs")
    assert jobs.status_code == 200
    assert jobs.json()["count"] == 1
    assert jobs.json()["items"][0]["id"] == "wo-field-api"
    assert jobs.json()["items"][0]["status_presentation"] == {
        "value": "dispatched",
        "label": "Dispatched",
        "tone": "info",
        "icon": "info",
    }

    detail = client.get("/api/v1/field/jobs/wo-field-api")
    assert detail.status_code == 200
    assert detail.json()["job"]["title"] == "Fibre install"
    assert detail.json()["job"]["status_presentation"]["value"] == "dispatched"
    assert detail.json()["customer"]["name"] == "Adaeze Nwosu"
    assert detail.json()["completion_requirements"] == {
        "evidence_required": True,
        "minimum_photo_count": 1,
        "customer_signoff_required": True,
        "signature_unavailable_reason_allowed": True,
    }

    destinations = client.get("/api/v1/field/jobs/wo-field-api/destinations")
    assert destinations.status_code == 200
    assert [item["destination_type"] for item in destinations.json()["items"]] == [
        "customer",
        "cabinet",
        "other",
    ]

    location = client.patch(
        "/api/v1/field/jobs/wo-field-api/location",
        json={"latitude": 9.081, "longitude": 7.462},
    )
    assert location.status_code == 200
    assert location.json()["latitude"] == 9.081
    assert location.json()["longitude"] == 7.462
    assert location.json()["source"] == "manual"


def test_field_router_registered_as_self_scoped_surface():
    from app.main import _DEFERRED_API_ROUTER_SPECS
    from tests.architecture.test_route_permission_guards import _ALLOWLIST_PREFIXES

    assert ("app.api.field", "router", "api", "user") in _DEFERRED_API_ROUTER_SPECS
    assert "/api/v1/field" in _ALLOWLIST_PREFIXES
