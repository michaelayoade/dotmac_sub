from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile
from app.models.network import OntAssignment, OntUnit, PonPort
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.auth_dependencies import require_user_auth
from app.services.field.equipment import field_equipment
from app.services.field.jobs import field_jobs


def _user(db_session, name: str = "Equipment") -> SystemUser:
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
    db_session, user: SystemUser, crm_person_id: str = "crm-equipment-tech"
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
        first_name="Equipment",
        last_name="Customer",
        email=f"equipment-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _work_order(db_session, subscriber: Subscriber, **overrides) -> WorkOrder:
    row = WorkOrder(
        crm_work_order_id=overrides.pop("crm_work_order_id", "wo-equipment"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Install ONT"),
        status=overrides.pop("status", "in_progress"),
        assigned_to_crm_person_id=overrides.pop(
            "assigned_to_crm_person_id", "crm-equipment-tech"
        ),
        scheduled_start=overrides.pop("scheduled_start", datetime.now(UTC)),
        **overrides,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _pon(db_session, olt_device, name: str) -> PonPort:
    olt_device.is_active = True
    pon = PonPort(olt_id=olt_device.id, name=name, is_active=True)
    db_session.add(pon)
    db_session.flush()
    return pon


def test_record_equipment_links_ont_subscription_pon_and_job(
    db_session, subscription, olt_device
):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = subscription.subscriber
    pon = _pon(db_session, olt_device, "0/1/1")
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-equip-link")
    db_session.commit()

    result = field_equipment.record(
        db_session,
        _auth(user),
        "wo-equip-link",
        serial_number=" zte123 ",
        subscription_id=str(subscription.id),
        pon_port_id=str(pon.id),
        vendor="ZTE",
        model="F601",
        notes="Installed in sitting room",
    )

    assert result["serial_number"] == "ZTE123"
    assert result["subscriber_id"] == subscriber.id
    assert result["crm_work_order_id"] == "wo-equip-link"
    assignment = db_session.query(OntAssignment).one()
    assert assignment.work_order_mirror_id == work_order.id
    assert assignment.subscription_id == subscription.id
    assert assignment.pon_port_id == pon.id
    assert assignment.active is True
    assert assignment.notes == "Installed in sitting room"
    detail = field_jobs.get_detail(db_session, _auth(user), "wo-equip-link")
    assert detail.equipment is not None
    assert detail.equipment.serial_number == "ZTE123"


def test_record_equipment_replaces_existing_active_assignment(
    db_session, subscription, olt_device
):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = subscription.subscriber
    pon = _pon(db_session, olt_device, "0/1/2")
    _work_order(db_session, subscriber, crm_work_order_id="wo-equip-replace")
    old_unit = OntUnit(
        serial_number="OLD123",
        vendor="ZTE",
        olt_device_id=olt_device.id,
        pon_port_id=pon.id,
    )
    db_session.add(old_unit)
    db_session.flush()
    old_assignment = OntAssignment(
        ont_unit_id=old_unit.id,
        pon_port_id=pon.id,
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        assigned_at=datetime.now(UTC),
        active=True,
    )
    db_session.add(old_assignment)
    db_session.commit()

    field_equipment.record(
        db_session,
        _auth(user),
        "wo-equip-replace",
        serial_number="new123",
        subscription_id=str(subscription.id),
        pon_port_id=str(pon.id),
    )

    db_session.refresh(old_assignment)
    assert old_assignment.active is False
    assert old_assignment.release_reason == "field_replaced"
    active = (
        db_session.query(OntAssignment)
        .filter(OntAssignment.subscriber_id == subscriber.id)
        .filter(OntAssignment.active.is_(True))
        .one()
    )
    assert active.ont_unit.serial_number == "NEW123"


def test_equipment_does_not_leak_hidden_jobs(db_session, subscription, olt_device):
    user = _user(db_session)
    _profile(db_session, user)
    other = _user(db_session, "Other")
    _profile(db_session, other, crm_person_id="other-equipment-tech")
    subscriber = subscription.subscriber
    pon = _pon(db_session, olt_device, "0/1/3")
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-equip-hidden",
        assigned_to_crm_person_id="other-equipment-tech",
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        field_equipment.record(
            db_session,
            _auth(user),
            "wo-equip-hidden",
            serial_number="ONT-HIDDEN",
            subscription_id=str(subscription.id),
            pon_port_id=str(pon.id),
        )

    assert exc.value.status_code == 404


def test_equipment_api(db_session, subscription, olt_device):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = subscription.subscriber
    pon = _pon(db_session, olt_device, "0/1/4")
    _work_order(db_session, subscriber, crm_work_order_id="wo-equip-api")
    db_session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    client = TestClient(app)

    created = client.post(
        "/api/v1/field/jobs/wo-equip-api/equipment",
        json={
            "serial_number": "ont-api-1",
            "subscription_id": str(subscription.id),
            "pon_port_id": str(pon.id),
            "vendor": "Huawei",
        },
    )

    assert created.status_code == 201
    assert created.json()["serial_number"] == "ONT-API-1"

    fetched = client.get("/api/v1/field/jobs/wo-equip-api/equipment")
    assert fetched.status_code == 200
    assert fetched.json()["vendor"] == "Huawei"
