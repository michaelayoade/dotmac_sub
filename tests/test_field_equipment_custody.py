from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.dispatch import TechnicianProfile
from app.models.field_asset import FieldAsset, FieldAssetCustody
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.services.field.equipment_custody import field_equipment_custody


def _user(db_session, name: str = "Custody") -> SystemUser:
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


def _profile(db_session, user: SystemUser) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        title="Installer",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def test_issue_list_and_return_field_asset_custody(db_session):
    user = _user(db_session)
    technician = _profile(db_session, user)
    asset = FieldAsset(
        asset_tag="LADDER-001",
        asset_type="tool",
        name="Extension ladder",
        status="available",
    )
    db_session.add(asset)
    db_session.commit()

    issued = field_equipment_custody.issue(
        db_session,
        asset_source="field_asset",
        asset_id=str(asset.id),
        technician_id=str(technician.id),
        condition_on_issue="good",
        notes="For Jabi installs",
    )

    assert issued["asset_label"] == "Extension ladder"
    assert issued["asset_identifier"] == "LADDER-001"
    assert issued["assigned_to"] == "Custody Tech"
    mine = field_equipment_custody.list_mine(db_session, _auth(user))
    assert [row["id"] for row in mine] == [issued["id"]]

    returned = field_equipment_custody.return_asset(
        db_session,
        str(issued["id"]),
        condition_on_return="good",
        notes="Back in store",
    )
    assert returned["status"] == "returned"
    assert returned["returned_at"] is not None
    assert db_session.query(FieldAssetCustody).one().status == "returned"


def test_cannot_issue_same_asset_to_two_technicians(db_session):
    user = _user(db_session)
    technician = _profile(db_session, user)
    other_user = _user(db_session, "OtherCustody")
    other_technician = _profile(db_session, other_user)
    asset = FieldAsset(
        asset_tag="METER-409",
        asset_type="test_equipment",
        name="Power meter",
        status="available",
    )
    db_session.add(asset)
    db_session.commit()
    field_equipment_custody.issue(
        db_session,
        asset_source="field_asset",
        asset_id=str(asset.id),
        technician_id=str(technician.id),
    )

    with pytest.raises(HTTPException) as exc:
        field_equipment_custody.issue(
            db_session,
            asset_source="field_asset",
            asset_id=str(asset.id),
            technician_id=str(other_technician.id),
        )

    assert exc.value.status_code == 409
