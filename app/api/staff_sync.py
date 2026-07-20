"""
Staff-account provisioning API (ERP staff sync).

Lets the ERP (HR system of record) drive staff lifecycle here: on hire it
creates + invites a SystemUser; on termination it deactivates the account
(which also revokes live sessions and disables credentials).

Auth: mounted with the standard user guard, so a scoped ``X-Api-Key``
principal works. Routes require the same RBAC permission keys as the admin
user-management UI (``rbac:assign`` to mutate, ``rbac:roles:read`` to read),
so an integration key carries exactly those scopes and nothing else.
Thin wrapper — logic lives in ``app/services/staff_provisioning.py``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.system_user import SystemUser
from app.services import staff_provisioning
from app.services.auth_dependencies import require_permission
from app.services.common import coerce_uuid

router = APIRouter(prefix="/staff-accounts", tags=["staff-sync"])

RoleName = Annotated[str, Field(min_length=1, max_length=120)]


class StaffAccountCreate(BaseModel):
    email: EmailStr
    first_name: str = Field(min_length=1, max_length=120)
    last_name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="staff", min_length=1, max_length=120)
    roles: list[RoleName] | None = Field(default=None, min_length=1, max_length=20)
    send_invite: bool = True


class StaffAccountRolesUpdate(BaseModel):
    roles: list[RoleName] = Field(min_length=1, max_length=20)


class StaffAccountRead(BaseModel):
    id: UUID
    email: str
    display_name: str | None
    is_active: bool
    roles: list[str] = Field(default_factory=list)
    created: bool = False
    invited: bool = False


def _to_read(
    db: Session,
    user: SystemUser,
    *,
    created: bool = False,
    invited: bool = False,
) -> StaffAccountRead:
    return StaffAccountRead(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_active=user.is_active,
        roles=staff_provisioning.get_role_names(db, user),
        created=created,
        invited=invited,
    )


@router.post(
    "",
    response_model=StaffAccountRead,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def create_staff_account(payload: StaffAccountCreate, db: Session = Depends(get_db)):
    """Create + invite a staff account. Idempotent on email."""
    try:
        user, created, invited = staff_provisioning.create_staff_account(
            db,
            email=payload.email,
            first_name=payload.first_name,
            last_name=payload.last_name,
            role=payload.role,
            roles=payload.roles,
            send_invite=payload.send_invite,
        )
    except staff_provisioning.UnknownRoleError as exc:
        raise HTTPException(status_code=422, detail=f"Unknown role(s): {exc}")
    return _to_read(db, user, created=created, invited=invited)


@router.get(
    "",
    response_model=StaffAccountRead,
    dependencies=[Depends(require_permission("rbac:roles:read"))],
)
def get_staff_account(email: EmailStr = Query(...), db: Session = Depends(get_db)):
    """Look up a staff account by email (used by the ERP reconcile sweep)."""
    user = staff_provisioning.find_by_email(db, email)
    if not user:
        raise HTTPException(status_code=404, detail="Staff account not found")
    return _to_read(db, user)


@router.put(
    "/{user_id}/roles",
    response_model=StaffAccountRead,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def update_staff_account_roles(
    user_id: str, payload: StaffAccountRolesUpdate, db: Session = Depends(get_db)
):
    try:
        normalized_user_id = coerce_uuid(user_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail="Staff account not found")
    user = db.get(SystemUser, normalized_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Staff account not found")
    try:
        staff_provisioning.sync_managed_roles(db, user=user, role_names=payload.roles)
    except staff_provisioning.UnknownRoleError as exc:
        raise HTTPException(status_code=422, detail=f"Unknown role(s): {exc}")
    return _to_read(db, user)


@router.post(
    "/{user_id}/activate",
    response_model=StaffAccountRead,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def activate_staff_account(user_id: str, db: Session = Depends(get_db)):
    try:
        user = staff_provisioning.set_staff_account_active(
            db, user_id=user_id, is_active=True
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="Staff account not found")
    return _to_read(db, user)


@router.post(
    "/{user_id}/deactivate",
    response_model=StaffAccountRead,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def deactivate_staff_account(user_id: str, db: Session = Depends(get_db)):
    """Disable a staff account: credentials off, live sessions revoked."""
    try:
        user = staff_provisioning.set_staff_account_active(
            db, user_id=user_id, is_active=False
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="Staff account not found")
    return _to_read(db, user)
