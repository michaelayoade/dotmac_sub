from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.common import ListResponse
from app.schemas.rbac import (
    PermissionCreate,
    PermissionRead,
    PermissionUpdate,
    RoleCreate,
    RolePermissionCreate,
    RolePermissionRead,
    RolePermissionUpdate,
    RoleRead,
    RoleUpdate,
    SubscriberRoleCreate,
    SubscriberRoleRead,
    SubscriberRoleUpdate,
)
from app.services import rbac_catalog, subscriber_assignments
from app.services.auth_dependencies import require_permission
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/rbac", tags=["rbac"])


def _context(auth: dict, *, scope: str, reason: str, key: str) -> CommandContext:
    principal_id = str(auth.get("principal_id") or "").strip()
    if not principal_id:
        raise HTTPException(status_code=403, detail="Authorized actor is missing")
    actor_type = "api_key" if auth.get("principal_type") == "api_key" else "user"
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"{actor_type}:{principal_id}",
        scope=scope,
        reason=reason,
        idempotency_key=key,
    )


def _domain_error(exc: DomainError) -> HTTPException:
    if exc.code.endswith("_not_found"):
        status_code = 404
    elif exc.code.endswith(("_conflict", "_in_use")) or exc.code.endswith(
        ".active_caller_transaction"
    ):
        status_code = 409
    elif exc.code.endswith(
        (
            ".invalid_command",
            ".invalid_scope",
            ".invalid_role_name",
            ".invalid_permission_key",
            ".invalid_permissions",
            ".protected_role",
            ".protected_permission",
        )
    ):
        status_code = 422
    else:
        status_code = 500
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": exc.message, "details": exc.details},
    )


@router.post(
    "/roles",
    response_model=RoleRead,
    status_code=status.HTTP_201_CREATED,
)
def create_role(
    payload: RoleCreate,
    auth: dict = Depends(require_permission("rbac:roles:write")),
    db: Session = Depends(get_db),
):
    try:
        return rbac_catalog.create_role(
            db,
            rbac_catalog.CreateRoleCommand(
                context=_context(
                    auth,
                    scope=rbac_catalog.ROLE_WRITE_SCOPE,
                    reason="API role catalog creation",
                    key=f"rbac-role-create:{payload.name.strip().lower()}",
                ),
                name=payload.name,
                description=payload.description,
                is_active=payload.is_active,
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc


@router.get(
    "/roles/{role_id}",
    response_model=RoleRead,
    dependencies=[Depends(require_permission("rbac:roles:read"))],
)
def get_role(role_id: UUID, db: Session = Depends(get_db)):
    role = rbac_catalog.get_role(db, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")
    return role


@router.get(
    "/roles",
    response_model=ListResponse[RoleRead],
    dependencies=[Depends(require_permission("rbac:roles:read"))],
)
def list_roles(
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return rbac_catalog.list_roles_response(
        db,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/roles/{role_id}",
    response_model=RoleRead,
)
def update_role(
    role_id: UUID,
    payload: RoleUpdate,
    auth: dict = Depends(require_permission("rbac:roles:write")),
    db: Session = Depends(get_db),
):
    try:
        return rbac_catalog.update_role(
            db,
            rbac_catalog.UpdateRoleCommand(
                context=_context(
                    auth,
                    scope=rbac_catalog.ROLE_WRITE_SCOPE,
                    reason="API role catalog update",
                    key=f"rbac-role-update:{role_id}",
                ),
                role_id=role_id,
                name=payload.name,
                description=payload.description,
                update_description="description" in payload.model_fields_set,
                is_active=payload.is_active,
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc


@router.delete(
    "/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_role(
    role_id: UUID,
    auth: dict = Depends(require_permission("rbac:roles:delete")),
    db: Session = Depends(get_db),
):
    try:
        rbac_catalog.deactivate_role(
            db,
            rbac_catalog.DeactivateRoleCommand(
                context=_context(
                    auth,
                    scope=rbac_catalog.ROLE_DELETE_SCOPE,
                    reason="API role catalog deactivation",
                    key=f"rbac-role-deactivate:{role_id}",
                ),
                role_id=role_id,
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc


@router.post(
    "/permissions",
    response_model=PermissionRead,
    status_code=status.HTTP_201_CREATED,
)
def create_permission(
    payload: PermissionCreate,
    auth: dict = Depends(require_permission("rbac:permissions:write")),
    db: Session = Depends(get_db),
):
    try:
        return rbac_catalog.create_permission(
            db,
            rbac_catalog.CreatePermissionCommand(
                context=_context(
                    auth,
                    scope=rbac_catalog.PERMISSION_WRITE_SCOPE,
                    reason="API permission catalog creation",
                    key=f"rbac-permission-create:{payload.key.strip().lower()}",
                ),
                key=payload.key,
                description=payload.description,
                is_active=payload.is_active,
                is_ui_assignable=payload.is_ui_assignable,
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc


@router.get(
    "/permissions/{permission_id}",
    response_model=PermissionRead,
    dependencies=[Depends(require_permission("rbac:permissions:read"))],
)
def get_permission(permission_id: UUID, db: Session = Depends(get_db)):
    permission = rbac_catalog.get_permission(db, permission_id)
    if permission is None:
        raise HTTPException(status_code=404, detail="Permission not found")
    return permission


@router.get(
    "/permissions",
    response_model=ListResponse[PermissionRead],
    dependencies=[Depends(require_permission("rbac:permissions:read"))],
)
def list_permissions(
    is_active: bool | None = None,
    order_by: str = Query(default="key"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return rbac_catalog.list_permissions_response(
        db,
        is_active=is_active,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/permissions/{permission_id}",
    response_model=PermissionRead,
)
def update_permission(
    permission_id: UUID,
    payload: PermissionUpdate,
    auth: dict = Depends(require_permission("rbac:permissions:write")),
    db: Session = Depends(get_db),
):
    try:
        return rbac_catalog.update_permission(
            db,
            rbac_catalog.UpdatePermissionCommand(
                context=_context(
                    auth,
                    scope=rbac_catalog.PERMISSION_WRITE_SCOPE,
                    reason="API permission catalog update",
                    key=f"rbac-permission-update:{permission_id}",
                ),
                permission_id=permission_id,
                key=payload.key,
                description=payload.description,
                update_description="description" in payload.model_fields_set,
                is_active=payload.is_active,
                is_ui_assignable=payload.is_ui_assignable,
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc


@router.delete(
    "/permissions/{permission_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_permission(
    permission_id: UUID,
    auth: dict = Depends(require_permission("rbac:permissions:delete")),
    db: Session = Depends(get_db),
):
    try:
        rbac_catalog.deactivate_permission(
            db,
            rbac_catalog.DeactivatePermissionCommand(
                context=_context(
                    auth,
                    scope=rbac_catalog.PERMISSION_DELETE_SCOPE,
                    reason="API permission catalog deactivation",
                    key=f"rbac-permission-deactivate:{permission_id}",
                ),
                permission_id=permission_id,
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc


@router.post(
    "/role-permissions",
    response_model=RolePermissionRead,
    status_code=status.HTTP_201_CREATED,
)
def create_role_permission(
    payload: RolePermissionCreate,
    auth: dict = Depends(require_permission("rbac:roles:write")),
    db: Session = Depends(get_db),
):
    try:
        return rbac_catalog.grant_role_permission(
            db,
            rbac_catalog.GrantRolePermissionCommand(
                context=_context(
                    auth,
                    scope=rbac_catalog.ROLE_WRITE_SCOPE,
                    reason="API role permission grant",
                    key=(
                        f"rbac-role-permission:{payload.role_id}:"
                        f"{payload.permission_id}"
                    ),
                ),
                role_id=payload.role_id,
                permission_id=payload.permission_id,
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc


@router.get(
    "/role-permissions/{link_id}",
    response_model=RolePermissionRead,
    dependencies=[Depends(require_permission("rbac:roles:read"))],
)
def get_role_permission(link_id: UUID, db: Session = Depends(get_db)):
    link = rbac_catalog.get_role_permission(db, link_id)
    if link is None:
        raise HTTPException(status_code=404, detail="Role permission not found")
    return link


@router.get(
    "/role-permissions",
    response_model=ListResponse[RolePermissionRead],
    dependencies=[Depends(require_permission("rbac:roles:read"))],
)
def list_role_permissions(
    role_id: UUID | None = None,
    permission_id: UUID | None = None,
    order_by: str = Query(default="role_id"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return rbac_catalog.list_role_permissions_response(
        db,
        role_id=role_id,
        permission_id=permission_id,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/role-permissions/{link_id}",
    response_model=RolePermissionRead,
)
def update_role_permission(
    link_id: UUID,
    payload: RolePermissionUpdate,
    auth: dict = Depends(require_permission("rbac:roles:write")),
    db: Session = Depends(get_db),
):
    try:
        return rbac_catalog.update_role_permission(
            db,
            rbac_catalog.UpdateRolePermissionCommand(
                context=_context(
                    auth,
                    scope=rbac_catalog.ROLE_WRITE_SCOPE,
                    reason="API role permission update",
                    key=f"rbac-role-permission-update:{link_id}",
                ),
                link_id=link_id,
                role_id=payload.role_id,
                permission_id=payload.permission_id,
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc


@router.delete(
    "/role-permissions/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_role_permission(
    link_id: UUID,
    auth: dict = Depends(require_permission("rbac:roles:write")),
    db: Session = Depends(get_db),
):
    try:
        rbac_catalog.revoke_role_permission(
            db,
            rbac_catalog.RevokeRolePermissionCommand(
                context=_context(
                    auth,
                    scope=rbac_catalog.ROLE_WRITE_SCOPE,
                    reason="API role permission revocation",
                    key=f"rbac-role-permission-revoke:{link_id}",
                ),
                link_id=link_id,
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc


@router.post(
    "/person-roles",
    response_model=SubscriberRoleRead,
    status_code=status.HTTP_201_CREATED,
)
def create_subscriber_role(
    payload: SubscriberRoleCreate,
    auth: dict = Depends(require_permission("rbac:assign")),
    db: Session = Depends(get_db),
):
    try:
        return subscriber_assignments.grant_subscriber_role(
            db,
            subscriber_assignments.GrantSubscriberRoleCommand(
                context=_context(
                    auth,
                    scope=subscriber_assignments.ASSIGNMENT_SCOPE,
                    reason="API subscriber role grant",
                    key=(
                        f"subscriber-role-grant:{payload.subscriber_id}:"
                        f"{payload.role_id}:{payload.scope_type}:{payload.scope_id}"
                    ),
                ),
                subscriber_id=payload.subscriber_id,
                role_id=payload.role_id,
                scope_type=payload.scope_type,
                scope_id=payload.scope_id,
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc


@router.get(
    "/person-roles/{link_id}",
    response_model=SubscriberRoleRead,
    dependencies=[Depends(require_permission("rbac:roles:read"))],
)
def get_subscriber_role(link_id: UUID, db: Session = Depends(get_db)):
    grant = subscriber_assignments.get_subscriber_role(db, link_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="Subscriber role not found")
    return grant


@router.get(
    "/person-roles",
    response_model=ListResponse[SubscriberRoleRead],
    dependencies=[Depends(require_permission("rbac:roles:read"))],
)
def list_subscriber_roles(
    subscriber_id: UUID | None = None,
    role_id: UUID | None = None,
    order_by: str = Query(default="assigned_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return subscriber_assignments.list_subscriber_roles_response(
        db,
        subscriber_id=subscriber_id,
        role_id=role_id,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/person-roles/{link_id}",
    response_model=SubscriberRoleRead,
)
def update_subscriber_role(
    link_id: UUID,
    payload: SubscriberRoleUpdate,
    auth: dict = Depends(require_permission("rbac:assign")),
    db: Session = Depends(get_db),
):
    try:
        return subscriber_assignments.update_subscriber_role(
            db,
            subscriber_assignments.UpdateSubscriberRoleCommand(
                context=_context(
                    auth,
                    scope=subscriber_assignments.ASSIGNMENT_SCOPE,
                    reason="API subscriber role update",
                    key=f"subscriber-role-update:{link_id}",
                ),
                grant_id=link_id,
                **payload.model_dump(exclude_unset=True),
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc


@router.delete(
    "/person-roles/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_subscriber_role(
    link_id: UUID,
    auth: dict = Depends(require_permission("rbac:assign")),
    db: Session = Depends(get_db),
):
    try:
        subscriber_assignments.revoke_subscriber_role(
            db,
            subscriber_assignments.RevokeSubscriberRoleCommand(
                context=_context(
                    auth,
                    scope=subscriber_assignments.ASSIGNMENT_SCOPE,
                    reason="API subscriber role revocation",
                    key=f"subscriber-role-revoke:{link_id}",
                ),
                grant_id=link_id,
            ),
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc
