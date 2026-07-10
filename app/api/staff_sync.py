"""
Staff-account provisioning API (ERP staff sync).

Lets the ERP (HR system of record) drive staff lifecycle here: on hire it
creates + invites a SystemUser; on termination it deactivates the account
(which also revokes live sessions and disables credentials).

Auth: mounted with the standard user guard, so a scoped ``X-Api-Key``
principal works. Routes require the same RBAC permission keys as the admin
user-management UI (``rbac:assign`` to mutate, ``rbac:roles:read`` to read),
so an integration key carries exactly those scopes and nothing else.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.rbac import Role
from app.models.system_user import SystemUser
from app.services import web_system_user_mutations as user_mutations
from app.services.auth_dependencies import require_permission

router = APIRouter(prefix="/staff-accounts", tags=["staff-sync"])


class StaffAccountCreate(BaseModel):
    email: EmailStr
    first_name: str = Field(min_length=1, max_length=120)
    last_name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="staff", min_length=1, max_length=120)
    send_invite: bool = True


class StaffAccountRead(BaseModel):
    id: UUID
    email: str
    display_name: str | None
    is_active: bool
    created: bool = False
    invited: bool = False


def _to_read(user: SystemUser, *, created: bool = False, invited: bool = False) -> StaffAccountRead:
    return StaffAccountRead(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_active=user.is_active,
        created=created,
        invited=invited,
    )


@router.post(
    "",
    response_model=StaffAccountRead,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def create_staff_account(payload: StaffAccountCreate, db: Session = Depends(get_db)):
    """Create + invite a staff account. Idempotent on email.

    If a SystemUser with this email already exists it is returned as-is
    (``created=false``) — the caller decides whether to activate/deactivate
    separately. Otherwise the user is created with the named role and a
    must-change temp credential, and an invite (password-set) email is sent.
    """
    email = payload.email.strip().lower()
    existing = db.query(SystemUser).filter(SystemUser.email == email).first()
    if existing:
        return _to_read(existing)

    role = db.query(Role).filter(Role.name == payload.role).first()
    if not role:
        raise HTTPException(status_code=422, detail=f"Unknown role: {payload.role}")

    user, _temp_password = user_mutations.create_user_with_role_and_password(
        db,
        first_name=payload.first_name.strip(),
        last_name=payload.last_name.strip(),
        email=email,
        role_id=str(role.id),
    )

    invited = False
    if payload.send_invite:
        try:
            user_mutations.send_user_invite_for_user(db, user_id=str(user.id))
            invited = True
        except Exception:
            # Account creation stands even if the invite email fails; the
            # caller (or an admin) can resend from the users screen.
            invited = False

    return _to_read(user, created=True, invited=invited)


@router.get(
    "",
    response_model=StaffAccountRead,
    dependencies=[Depends(require_permission("rbac:roles:read"))],
)
def get_staff_account(email: EmailStr = Query(...), db: Session = Depends(get_db)):
    """Look up a staff account by email (used by the ERP reconcile sweep)."""
    user = (
        db.query(SystemUser)
        .filter(SystemUser.email == email.strip().lower())
        .first()
    )
    if not user:
        raise HTTPException(status_code=404, detail="Staff account not found")
    return _to_read(user)


@router.post(
    "/{user_id}/activate",
    response_model=StaffAccountRead,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def activate_staff_account(user_id: str, db: Session = Depends(get_db)):
    try:
        user = user_mutations.set_user_active(db, user_id=user_id, is_active=True)
    except ValueError:
        raise HTTPException(status_code=404, detail="Staff account not found")
    return _to_read(user)


@router.post(
    "/{user_id}/deactivate",
    response_model=StaffAccountRead,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def deactivate_staff_account(user_id: str, db: Session = Depends(get_db)):
    """Disable a staff account: credentials off, live sessions revoked."""
    try:
        user = user_mutations.set_user_active(db, user_id=user_id, is_active=False)
    except ValueError:
        raise HTTPException(status_code=404, detail="Staff account not found")
    return _to_read(user)
