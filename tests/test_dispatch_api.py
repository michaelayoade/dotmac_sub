from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dispatch import router
from app.db import get_db
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order_mirror import WorkOrderMirror


def _client(db_session) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _system_user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Ade",
        last_name="Tech",
        email=f"ade-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _work_order(db_session) -> WorkOrderMirror:
    sub = Subscriber(
        first_name="Adaeze",
        last_name="Nwosu",
        email=f"adaeze-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(sub)
    db_session.flush()
    row = WorkOrderMirror(
        crm_work_order_id="wo-api-1",
        subscriber_id=sub.id,
        title="Fibre install",
        status="scheduled",
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_dispatch_api_skill_crud_and_listing(db_session):
    client = _client(db_session)
    skill_name = f"fiber_splicing_{uuid4().hex[:8]}"

    created = client.post(
        "/api/v1/dispatch/skills",
        json={"name": skill_name, "description": "Fiber splicing"},
    )
    assert created.status_code == 201
    skill_id = created.json()["id"]

    listed = client.get("/api/v1/dispatch/skills")
    assert listed.status_code == 200
    assert any(item["name"] == skill_name for item in listed.json()["items"])

    patched = client.patch(
        f"/api/v1/dispatch/skills/{skill_id}",
        json={"description": "Updated"},
    )
    assert patched.status_code == 200
    assert patched.json()["description"] == "Updated"

    deleted = client.delete(f"/api/v1/dispatch/skills/{skill_id}")
    assert deleted.status_code == 204
    remaining = client.get("/api/v1/dispatch/skills").json()["items"]
    assert all(item["id"] != skill_id for item in remaining)


def test_dispatch_api_technician_shift_and_assignment_queue(db_session):
    client = _client(db_session)
    user = _system_user(db_session)
    work_order = _work_order(db_session)

    technician_resp = client.post(
        "/api/v1/dispatch/technicians",
        json={"system_user_id": str(user.id), "region": "Jabi"},
    )
    assert technician_resp.status_code == 201
    technician_id = technician_resp.json()["id"]
    assert technician_resp.json()["person_id"] == str(user.id)

    start = datetime.now(UTC)
    shift_resp = client.post(
        "/api/v1/dispatch/shifts",
        json={
            "technician_id": technician_id,
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=8)).isoformat(),
            "timezone": "Africa/Lagos",
        },
    )
    assert shift_resp.status_code == 201
    assert shift_resp.json()["timezone"] == "Africa/Lagos"

    queue_resp = client.post(
        "/api/v1/dispatch/assignment-queue",
        json={
            "crm_work_order_id": work_order.crm_work_order_id,
            "assigned_technician_id": technician_id,
            "reason": "API test",
        },
    )
    assert queue_resp.status_code == 201
    queue_id = queue_resp.json()["id"]
    assert queue_resp.json()["crm_work_order_id"] == "wo-api-1"

    patched = client.patch(
        f"/api/v1/dispatch/assignment-queue/{queue_id}",
        json={"status": "assigned"},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "assigned"


def test_dispatch_router_registered_with_permission_guard():
    from app.main import _DEFERRED_API_ROUTER_SPECS

    assert (
        "app.api.dispatch",
        "router",
        "api",
        "perm:operations:dispatch",
    ) in _DEFERRED_API_ROUTER_SPECS
