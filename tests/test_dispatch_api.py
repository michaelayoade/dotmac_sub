from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dispatch import router
from app.db import get_db
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.auth_dependencies import require_user_auth


def _client(db_session) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    # The per-endpoint operations:dispatch:read/write/assign gates need an
    # authenticated principal; grant an admin principal so these tests exercise
    # endpoint behaviour (auth itself is covered by the RBAC guard suite).
    app.dependency_overrides[require_user_auth] = lambda: {
        "roles": ["admin"],
        "scopes": [],
    }
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


def _work_order(db_session) -> WorkOrder:
    sub = Subscriber(
        first_name="Adaeze",
        last_name="Nwosu",
        email=f"adaeze-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(sub)
    db_session.flush()
    row = WorkOrder(
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


def test_dispatch_api_native_work_order_header_crud(db_session):
    client = _client(db_session)
    user = _system_user(db_session)
    sub = Subscriber(
        first_name="Adaeze",
        last_name="Nwosu",
        email=f"native-wo-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(sub)
    db_session.commit()

    created = client.post(
        "/api/v1/dispatch/work-orders",
        json={
            "public_id": "sub-wo-api-1",
            "subscriber_id": str(sub.id),
            "title": "Fibre install",
            "status": "scheduled",
            "work_type": "install",
            "required_skills": ["fiber"],
            "tags": ["native"],
            "metadata": {"source_ref": "api"},
        },
    )
    assert created.status_code == 201
    assert created.json()["public_id"] == "sub-wo-api-1"
    assert created.json()["crm_work_order_id"] is None
    assert created.json()["metadata"]["native_source"] == "sub"

    patched = client.patch(
        "/api/v1/dispatch/work-orders/sub-wo-api-1",
        json={"status": "dispatched", "assigned_to_name": "Ade Tech"},
    )
    assert patched.status_code == 422
    assert "assignment command" in patched.json()["detail"].lower()

    technician = client.post(
        "/api/v1/dispatch/technicians",
        json={"system_user_id": str(user.id), "region": "Jabi"},
    )
    preview = client.post(
        "/api/v1/dispatch/work-orders/sub-wo-api-1/assignment-preview",
        json={"technician_id": technician.json()["id"]},
    )
    assert preview.status_code == 200
    assert preview.json()["previous"]["status"] == "scheduled"
    assert preview.json()["result"]["status"] == "dispatched"
    queue = client.post(
        "/api/v1/dispatch/assignment-queue",
        json={
            "crm_work_order_id": "sub-wo-api-1",
            "assigned_technician_id": technician.json()["id"],
            "status": "assigned",
        },
    )
    assert queue.status_code == 201
    assert queue.json()["crm_work_order_id"] == "sub-wo-api-1"

    listed = client.get(
        f"/api/v1/dispatch/work-orders?subscriber_id={sub.id}&status=dispatched"
    )
    assert listed.status_code == 200
    assert [item["public_id"] for item in listed.json()["items"]] == ["sub-wo-api-1"]


def test_granular_dispatch_permissions_are_role_builder_assignable():
    """Operators must be able to build new roles from the granular dispatch perms."""
    from scripts.seed.seed_rbac import (
        ADMIN_ONLY_PERMISSION_KEYS,
        DEFAULT_PERMISSIONS,
    )

    seeded = {key for key, _ in DEFAULT_PERMISSIONS}
    for perm in (
        "operations:dispatch:read",
        "operations:dispatch:write",
        "operations:dispatch:assign",
    ):
        assert perm in seeded, f"{perm} must be seeded"
        # role builder lists/assigns only is_ui_assignable perms (not admin-only)
        assert perm not in ADMIN_ONLY_PERMISSION_KEYS, (
            f"{perm} must be role-builder-assignable"
        )
    assert "operations:dispatch" not in seeded, "coarse permission must be retired"


def test_dispatch_router_registered_with_permission_guard():
    from app.main import _DEFERRED_API_ROUTER_SPECS

    # Router floor is operations:dispatch:read; mutations add write/assign gates.
    assert (
        "app.api.dispatch",
        "router",
        "api",
        "perm:operations:dispatch:read",
    ) in _DEFERRED_API_ROUTER_SPECS
