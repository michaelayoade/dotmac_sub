"""Thin ERP HR adapter for the canonical staff-provisioning owner."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.system_user import SystemUser
from app.services import staff_provisioning
from app.services.auth_dependencies import require_permission
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/staff-accounts", tags=["staff-sync"])

RoleName = Annotated[str, Field(min_length=1, max_length=120)]
IdempotencyKey = Annotated[
    str | None,
    Header(alias="Idempotency-Key", min_length=1, max_length=200),
]


class StaffAccountCreate(BaseModel):
    email: EmailStr
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    role: str = Field(default="staff", min_length=1, max_length=80)
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
    changed: bool = False
    invite_requested: bool = False


def _actor(auth: dict) -> str:
    principal_id = str(auth.get("principal_id") or "").strip()
    if not principal_id:
        raise HTTPException(status_code=403, detail="Authorized actor is missing")
    actor_type = "api_key" if auth.get("principal_type") == "api_key" else "user"
    return f"{actor_type}:{principal_id}"


def _context(
    auth: dict,
    *,
    reason: str,
    idempotency_key: str,
) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=_actor(auth),
        scope=staff_provisioning.STAFF_ASSIGN_SCOPE,
        reason=reason,
        idempotency_key=idempotency_key,
    )


def _domain_error(exc: DomainError) -> HTTPException:
    if exc.code.endswith(".unknown_roles") or exc.code.endswith(".invalid_command"):
        status_code = 422
    elif exc.code.endswith(".staff_account_not_found"):
        status_code = 404
    elif (
        exc.code.endswith(".identity_conflict")
        or exc.code.endswith(".active_caller_transaction")
        or exc.code.endswith(".last_admin_required")
    ):
        status_code = 409
    else:
        status_code = 500
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": exc.message, "details": exc.details},
    )


def _from_outcome(
    outcome: staff_provisioning.StaffAccountOutcome,
) -> StaffAccountRead:
    return StaffAccountRead(
        id=outcome.user_id,
        email=outcome.email,
        display_name=outcome.display_name,
        is_active=outcome.is_active,
        roles=list(outcome.role_names),
        created=outcome.created,
        changed=outcome.changed,
        invite_requested=outcome.invite_requested,
    )


def _from_user(db: Session, user: SystemUser) -> StaffAccountRead:
    return StaffAccountRead(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_active=user.is_active,
        roles=staff_provisioning.get_role_names(db, user),
    )


@router.post("", response_model=StaffAccountRead)
def create_staff_account(
    payload: StaffAccountCreate,
    auth: dict = Depends(require_permission("rbac:assign")),
    db: Session = Depends(get_db),
    idempotency_key: IdempotencyKey = None,
) -> StaffAccountRead:
    """Create or reconcile a staff account by canonical email."""

    normalized_email = str(payload.email).strip().lower()
    desired_roles = tuple(payload.roles or [payload.role])
    try:
        result = staff_provisioning.provision_staff_account(
            db,
            staff_provisioning.ProvisionStaffAccountCommand(
                context=_context(
                    auth,
                    reason="ERP HR staff account reconciliation",
                    idempotency_key=(
                        idempotency_key or f"staff-account:{normalized_email}"
                    ),
                ),
                email=normalized_email,
                first_name=payload.first_name,
                last_name=payload.last_name,
                role_names=desired_roles,
                send_invite=payload.send_invite,
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return _from_outcome(result)


@router.get("", response_model=StaffAccountRead)
def get_staff_account(
    email: EmailStr = Query(...),
    _auth: dict = Depends(require_permission("rbac:roles:read")),
    db: Session = Depends(get_db),
) -> StaffAccountRead:
    """Look up a staff account for an ERP reconcile sweep."""

    user = staff_provisioning.find_by_email(db, str(email))
    if not user:
        raise HTTPException(status_code=404, detail="Staff account not found")
    return _from_user(db, user)


@router.put("/{user_id}/roles", response_model=StaffAccountRead)
def update_staff_account_roles(
    user_id: str,
    payload: StaffAccountRolesUpdate,
    auth: dict = Depends(require_permission("rbac:assign")),
    db: Session = Depends(get_db),
    idempotency_key: IdempotencyKey = None,
) -> StaffAccountRead:
    try:
        normalized_user_id = coerce_uuid(user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Staff account not found") from exc
    canonical_roles = tuple(dict.fromkeys(role.strip() for role in payload.roles))
    try:
        result = staff_provisioning.sync_staff_account_roles(
            db,
            staff_provisioning.SyncStaffRolesCommand(
                context=_context(
                    auth,
                    reason="ERP HR staff role reconciliation",
                    idempotency_key=(
                        idempotency_key
                        or f"staff-roles:{normalized_user_id}:{','.join(sorted(canonical_roles))}"
                    ),
                ),
                user_id=normalized_user_id,
                role_names=canonical_roles,
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return _from_outcome(result)


def _set_active(
    *,
    user_id: str,
    is_active: bool,
    auth: dict,
    db: Session,
    idempotency_key: str | None,
) -> StaffAccountRead:
    try:
        normalized_user_id = coerce_uuid(user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Staff account not found") from exc
    state = "active" if is_active else "inactive"
    try:
        result = staff_provisioning.set_staff_account_active(
            db,
            staff_provisioning.SetStaffAccountActiveCommand(
                context=_context(
                    auth,
                    reason=f"ERP HR staff account {state} reconciliation",
                    idempotency_key=(
                        idempotency_key or f"staff-state:{normalized_user_id}:{state}"
                    ),
                ),
                user_id=normalized_user_id,
                is_active=is_active,
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc
    return _from_outcome(result)


@router.post("/{user_id}/activate", response_model=StaffAccountRead)
def activate_staff_account(
    user_id: str,
    auth: dict = Depends(require_permission("rbac:assign")),
    db: Session = Depends(get_db),
    idempotency_key: IdempotencyKey = None,
) -> StaffAccountRead:
    return _set_active(
        user_id=user_id,
        is_active=True,
        auth=auth,
        db=db,
        idempotency_key=idempotency_key,
    )


@router.post("/{user_id}/deactivate", response_model=StaffAccountRead)
def deactivate_staff_account(
    user_id: str,
    auth: dict = Depends(require_permission("rbac:assign")),
    db: Session = Depends(get_db),
    idempotency_key: IdempotencyKey = None,
) -> StaffAccountRead:
    """Disable a staff account, credentials, and live sessions atomically."""

    return _set_active(
        user_id=user_id,
        is_active=False,
        auth=auth,
        db=db,
        idempotency_key=idempotency_key,
    )
