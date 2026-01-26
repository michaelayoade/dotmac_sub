from app.schemas.rbac import (
    PermissionCreate,
    PermissionUpdate,
    RoleCreate,
    RolePermissionCreate,
    RoleUpdate,
)
from app.services import rbac as rbac_service


def test_role_permission_link_flow(db_session):
    role = rbac_service.roles.create(db_session, RoleCreate(name="Support"))
    permission = rbac_service.permissions.create(
        db_session, PermissionCreate(key="tickets.read", name="Read Tickets")
    )
    link = rbac_service.role_permissions.create(
        db_session,
        RolePermissionCreate(role_id=role.id, permission_id=permission.id),
    )
    items = rbac_service.role_permissions.list(
        db_session,
        role_id=role.id,
        permission_id=None,
        order_by="role_id",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert items[0].id == link.id


def test_role_permission_soft_delete_filters(db_session):
    role = rbac_service.roles.create(db_session, RoleCreate(name="Billing"))
    rbac_service.roles.update(db_session, str(role.id), RoleUpdate(name="Billing Ops"))
    rbac_service.roles.delete(db_session, str(role.id))
    active = rbac_service.roles.list(
        db_session,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    inactive = rbac_service.roles.list(
        db_session,
        is_active=False,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert role not in active
    assert any(item.id == role.id for item in inactive)


def test_permission_update(db_session):
    permission = rbac_service.permissions.create(
        db_session, PermissionCreate(key="billing.write", description="Billing Write")
    )
    updated = rbac_service.permissions.update(
        db_session,
        str(permission.id),
        PermissionUpdate(description="Billing Write Access"),
    )
    assert updated.description == "Billing Write Access"
