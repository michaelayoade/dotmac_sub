from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session
from app.schemas.common import ListResponse

from app.db import SessionLocal
from app.schemas.rbac import (
    PermissionCreate,
    PermissionRead,
    PermissionUpdate,
    PersonRoleCreate,
    PersonRoleRead,
    PersonRoleUpdate,
    RoleCreate,
    RolePermissionCreate,
    RolePermissionRead,
    RolePermissionUpdate,
    RoleRead,
    RoleUpdate,
)
from app.services import rbac as rbac_service
from app.services.auth_dependencies import require_permission

router = APIRouter(prefix="/rbac", tags=["rbac"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post(
    "/roles",
    response_model=RoleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("rbac:roles:write"))],
)
def create_role(payload: RoleCreate, db: Session = Depends(get_db)):
    return rbac_service.roles.create(db, payload)


@router.get(
    "/roles/{role_id}",
    response_model=RoleRead,
    dependencies=[Depends(require_permission("rbac:roles:read"))],
)
def get_role(role_id: str, db: Session = Depends(get_db)):
    return rbac_service.roles.get(db, role_id)


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
    return rbac_service.roles.list_response(db, is_active, order_by, order_dir, limit, offset)


@router.patch(
    "/roles/{role_id}",
    response_model=RoleRead,
    dependencies=[Depends(require_permission("rbac:roles:write"))],
)
def update_role(role_id: str, payload: RoleUpdate, db: Session = Depends(get_db)):
    return rbac_service.roles.update(db, role_id, payload)


@router.delete(
    "/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("rbac:roles:delete"))],
)
def delete_role(role_id: str, db: Session = Depends(get_db)):
    rbac_service.roles.delete(db, role_id)


@router.post(
    "/permissions",
    response_model=PermissionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("rbac:permissions:write"))],
)
def create_permission(payload: PermissionCreate, db: Session = Depends(get_db)):
    return rbac_service.permissions.create(db, payload)


@router.get(
    "/permissions/{permission_id}",
    response_model=PermissionRead,
    dependencies=[Depends(require_permission("rbac:permissions:read"))],
)
def get_permission(permission_id: str, db: Session = Depends(get_db)):
    return rbac_service.permissions.get(db, permission_id)


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
    return rbac_service.permissions.list_response(db, is_active, order_by, order_dir, limit, offset)


@router.patch(
    "/permissions/{permission_id}",
    response_model=PermissionRead,
    dependencies=[Depends(require_permission("rbac:permissions:write"))],
)
def update_permission(
    permission_id: str, payload: PermissionUpdate, db: Session = Depends(get_db)
):
    return rbac_service.permissions.update(db, permission_id, payload)


@router.delete(
    "/permissions/{permission_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("rbac:permissions:delete"))],
)
def delete_permission(permission_id: str, db: Session = Depends(get_db)):
    rbac_service.permissions.delete(db, permission_id)


@router.post(
    "/role-permissions",
    response_model=RolePermissionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("rbac:roles:write"))],
)
def create_role_permission(
    payload: RolePermissionCreate, db: Session = Depends(get_db)
):
    return rbac_service.role_permissions.create(db, payload)


@router.get(
    "/role-permissions/{link_id}",
    response_model=RolePermissionRead,
    dependencies=[Depends(require_permission("rbac:roles:read"))],
)
def get_role_permission(link_id: str, db: Session = Depends(get_db)):
    return rbac_service.role_permissions.get(db, link_id)


@router.get(
    "/role-permissions",
    response_model=ListResponse[RolePermissionRead],
    dependencies=[Depends(require_permission("rbac:roles:read"))],
)
def list_role_permissions(
    role_id: str | None = None,
    permission_id: str | None = None,
    order_by: str = Query(default="role_id"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return rbac_service.role_permissions.list_response(
        db, role_id, permission_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/role-permissions/{link_id}",
    response_model=RolePermissionRead,
    dependencies=[Depends(require_permission("rbac:roles:write"))],
)
def update_role_permission(
    link_id: str, payload: RolePermissionUpdate, db: Session = Depends(get_db)
):
    return rbac_service.role_permissions.update(db, link_id, payload)


@router.delete(
    "/role-permissions/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("rbac:roles:write"))],
)
def delete_role_permission(link_id: str, db: Session = Depends(get_db)):
    rbac_service.role_permissions.delete(db, link_id)


@router.post(
    "/person-roles",
    response_model=PersonRoleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def create_person_role(payload: PersonRoleCreate, db: Session = Depends(get_db)):
    return rbac_service.person_roles.create(db, payload)


@router.get(
    "/person-roles/{link_id}",
    response_model=PersonRoleRead,
    dependencies=[Depends(require_permission("rbac:roles:read"))],
)
def get_person_role(link_id: str, db: Session = Depends(get_db)):
    return rbac_service.person_roles.get(db, link_id)


@router.get(
    "/person-roles",
    response_model=ListResponse[PersonRoleRead],
    dependencies=[Depends(require_permission("rbac:roles:read"))],
)
def list_person_roles(
    person_id: str | None = None,
    role_id: str | None = None,
    order_by: str = Query(default="assigned_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return rbac_service.person_roles.list_response(
        db, person_id, role_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/person-roles/{link_id}",
    response_model=PersonRoleRead,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def update_person_role(
    link_id: str, payload: PersonRoleUpdate, db: Session = Depends(get_db)
):
    return rbac_service.person_roles.update(db, link_id, payload)


@router.delete(
    "/person-roles/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("rbac:assign"))],
)
def delete_person_role(link_id: str, db: Session = Depends(get_db)):
    rbac_service.person_roles.delete(db, link_id)