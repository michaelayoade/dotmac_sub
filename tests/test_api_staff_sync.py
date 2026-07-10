"""Staff-account provisioning API (ERP staff sync) — service-level behavior.

Exercises the route handlers directly against the test DB (the router's
permission dependencies are covered by the shared require_permission tests).
"""

import pytest
from fastapi import HTTPException

from app.api.staff_sync import (
    StaffAccountCreate,
    create_staff_account,
    deactivate_staff_account,
    get_staff_account,
)
from app.models.rbac import Role
from app.models.system_user import SystemUser


@pytest.fixture()
def staff_role(db_session):
    role = db_session.query(Role).filter(Role.name == "staff").first()
    if not role:
        role = Role(name="staff", description="Baseline staff role")
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
