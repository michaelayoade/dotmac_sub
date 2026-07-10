from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile
from app.models.field_material import FieldInventoryItem, FieldWorkOrderMaterial
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order_mirror import WorkOrderMirror
from app.services.auth_dependencies import require_user_auth
from app.services.field.jobs import field_jobs
from app.services.field.materials import field_materials


def _user(db_session, name: str = "Material") -> SystemUser:
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


def _profile(
    db_session, user: SystemUser, crm_person_id: str = "crm-material-tech"
) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=crm_person_id,
        title="Installer",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Material",
        last_name="Customer",
        email=f"material-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _work_order(db_session, subscriber: Subscriber, **overrides) -> WorkOrderMirror:
    row = WorkOrderMirror(
        crm_work_order_id=overrides.pop("crm_work_order_id", "wo-material"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Install materials"),
        status=overrides.pop("status", "in_progress"),
        assigned_to_crm_person_id=overrides.pop(
            "assigned_to_crm_person_id", "crm-material-tech"
        ),
        scheduled_start=overrides.pop("scheduled_start", datetime.now(UTC)),
        **overrides,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _item(db_session, **overrides) -> FieldInventoryItem:
    item = FieldInventoryItem(
        sku=overrides.pop("sku", f"SKU-{uuid4().hex[:6]}"),
        name=overrides.pop("name", "Drop cable"),
        unit=overrides.pop("unit", "m"),
        **overrides,
    )
    db_session.add(item)
    db_session.flush()
    return item


def _material(
    db_session,
    work_order: WorkOrderMirror,
    item: FieldInventoryItem,
    **overrides,
) -> FieldWorkOrderMaterial:
    material = FieldWorkOrderMaterial(
        work_order_mirror_id=work_order.id,
        crm_work_order_id=work_order.crm_work_order_id,
        item_id=item.id,
        allocated_quantity=overrides.pop("allocated_quantity", 50),
        consumed_quantity=overrides.pop("consumed_quantity", 0),
        status=overrides.pop("status", "reserved"),
        **overrides,
    )
    db_session.add(material)
    db_session.flush()
    return material


def test_list_consume_and_surface_materials_in_job_detail(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-materials")
    item = _item(db_session, name="Drop cable", unit="m")
    material = _material(db_session, work_order, item, allocated_quantity=50)
    db_session.commit()

    listed = field_materials.list_for_job(db_session, _auth(user), "wo-materials")
    assert listed[0]["name"] == "Drop cable"
    assert listed[0]["remaining_quantity"] == 50

    consumed = field_materials.consume(
        db_session,
        _auth(user),
        "wo-materials",
        [{"material_id": material.id, "consumed_quantity": 50}],
    )

    assert consumed[0]["status"] == "used"
    assert consumed[0]["remaining_quantity"] == 0
    db_session.refresh(material)
    assert material.consumed_quantity == 50
    assert material.status == "used"
    db_session.refresh(work_order)
    assert work_order.metadata_["native_field_source"] == "sub"
    assert "materials" in work_order.metadata_["native_field_activity"]

    detail = field_jobs.get_detail(db_session, _auth(user), "wo-materials")
    assert len(detail.materials) == 1
    assert detail.materials[0].status == "used"


def test_material_consumption_is_monotonic_and_validates_quantity(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(
        db_session, subscriber, crm_work_order_id="wo-material-rules"
    )
    material = _material(
        db_session,
        work_order,
        _item(db_session),
        allocated_quantity=10,
        consumed_quantity=4,
    )
    db_session.commit()

    lower = field_materials.consume(
        db_session,
        _auth(user),
        "wo-material-rules",
        [{"material_id": material.id, "consumed_quantity": 2}],
    )
    assert lower[0]["consumed_quantity"] == 4

    with pytest.raises(HTTPException) as exc:
        field_materials.consume(
            db_session,
            _auth(user),
            "wo-material-rules",
            [{"material_id": material.id, "consumed_quantity": 11}],
        )
    assert exc.value.status_code == 422


def test_materials_do_not_leak_hidden_jobs(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    other = _user(db_session, "Other")
    _profile(db_session, other, crm_person_id="other-material-tech")
    subscriber = _subscriber(db_session)
    work_order = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-material-hidden",
        assigned_to_crm_person_id="other-material-tech",
    )
    material = _material(db_session, work_order, _item(db_session))
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        field_materials.consume(
            db_session,
            _auth(user),
            "wo-material-hidden",
            [{"material_id": material.id, "consumed_quantity": 1}],
        )

    assert exc.value.status_code == 404


def test_material_api(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(
        db_session, subscriber, crm_work_order_id="wo-material-api"
    )
    material = _material(
        db_session,
        work_order,
        _item(db_session, name="Connector", unit="pcs"),
        allocated_quantity=4,
    )
    db_session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    client = TestClient(app)

    listed = client.get("/api/v1/field/jobs/wo-material-api/materials")
    assert listed.status_code == 200
    assert listed.json()[0]["name"] == "Connector"

    consumed = client.post(
        "/api/v1/field/jobs/wo-material-api/materials/consume",
        json={"items": [{"material_id": str(material.id), "consumed_quantity": 4}]},
    )
    assert consumed.status_code == 200
    assert consumed.json()[0]["status"] == "used"
