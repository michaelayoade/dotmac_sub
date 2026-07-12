"""Staff-account provisioning API (ERP staff sync) — service-level behavior.

Exercises the route handlers directly against the test DB (the router's
permission dependencies are covered by the shared require_permission tests).
"""

import pytest
from fastapi import HTTPException

from app.api.staff_sync import (
    StaffAccountCreate,
    StaffAccountRolesUpdate,
    create_staff_account,
    deactivate_staff_account,
    get_staff_account,
    update_staff_account_roles,
)
from app.models.rbac import Role, SystemUserRole
from app.models.system_user import SystemUser


@pytest.fixture()
def staff_role(db_session):
    role = db_session.query(Role).filter(Role.name == "staff").first()
    if not role:
        role = Role(name="staff", description="Baseline staff role")
        db_session.add(role)
        db_session.commit()
    return role


@pytest.fixture()
def field_role(db_session):
    role = db_session.query(Role).filter(Role.name == "field_technician").first()
    if not role:
        role = Role(name="field_technician", description="Field technician")
        db_session.add(role)
        db_session.commit()
    return role


def _payload(**overrides):
    base = dict(
        email="new.hire@dotmac.io",
        first_name="New",
        last_name="Hire",
        role="staff",
        send_invite=False,  # no SMTP in tests
    )
    base.update(overrides)
    return StaffAccountCreate(**base)


def test_create_is_idempotent_on_email(db_session, staff_role):
    first = create_staff_account(_payload(), db=db_session)
    assert first.created is True
    assert first.is_active is True

    again = create_staff_account(_payload(), db=db_session)
    assert again.created is False
    assert again.id == first.id

    user = db_session.get(SystemUser, first.id)
    assert user.email == "new.hire@dotmac.io"
    assert user.display_name == "New Hire"
    assert first.roles == ["staff"]


def test_create_unknown_role_is_422(db_session):
    with pytest.raises(HTTPException) as exc:
        create_staff_account(_payload(role="does-not-exist"), db=db_session)
    assert exc.value.status_code == 422


def test_deactivate_disables_account(db_session, staff_role):
    created = create_staff_account(_payload(email="leaver@dotmac.io"), db=db_session)
    result = deactivate_staff_account(str(created.id), db=db_session)
    assert result.is_active is False
    assert db_session.get(SystemUser, created.id).is_active is False


def test_get_by_email_and_404(db_session, staff_role):
    created = create_staff_account(_payload(email="lookup@dotmac.io"), db=db_session)
    found = get_staff_account(email="lookup@dotmac.io", db=db_session)
    assert found.id == created.id

    with pytest.raises(HTTPException) as exc:
        get_staff_account(email="ghost@dotmac.io", db=db_session)
    assert exc.value.status_code == 404


def test_role_sync_replaces_only_erp_managed_roles(db_session, staff_role, field_role):
    created = create_staff_account(_payload(), db=db_session)
    local_role = Role(name="incident_commander", description="Local emergency grant")
    db_session.add(local_role)
    db_session.flush()
    db_session.add(
        SystemUserRole(
            system_user_id=created.id,
            role_id=local_role.id,
            source="local",
        )
    )
    db_session.commit()

    result = update_staff_account_roles(
        str(created.id),
        StaffAccountRolesUpdate(roles=["field_technician"]),
        db=db_session,
    )

    assert result.roles == ["field_technician", "incident_commander"]
    grants = (
        db_session.query(SystemUserRole, Role.name)
        .join(Role, Role.id == SystemUserRole.role_id)
        .filter(SystemUserRole.system_user_id == created.id)
        .all()
    )
    assert {(name, grant.source) for grant, name in grants} == {
        ("field_technician", "erp_hr"),
        ("incident_commander", "local"),
    }


def test_role_sync_rejects_unknown_role_without_partial_update(db_session, staff_role):
    created = create_staff_account(_payload(), db=db_session)

    with pytest.raises(HTTPException) as exc:
        update_staff_account_roles(
            str(created.id),
            StaffAccountRolesUpdate(roles=["missing_role"]),
            db=db_session,
        )

    assert exc.value.status_code == 422
    assert get_staff_account(email=created.email, db=db_session).roles == ["staff"]
