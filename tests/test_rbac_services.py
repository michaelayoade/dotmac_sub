from uuid import uuid4

from app.services import rbac_catalog
from app.services.owner_commands import CommandContext


def _context(scope: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:rbac-service-test",
        scope=scope,
        reason="RBAC service compatibility test",
        idempotency_key=f"rbac-test:{command_id}",
    )


def test_role_permission_link_flow(db_session):
    role = rbac_catalog.create_role(
        db_session,
        rbac_catalog.CreateRoleCommand(
            context=_context(rbac_catalog.ROLE_WRITE_SCOPE),
            name="support",
        ),
    )
    permission = rbac_catalog.create_permission(
        db_session,
        rbac_catalog.CreatePermissionCommand(
            context=_context(rbac_catalog.PERMISSION_WRITE_SCOPE),
            key="tickets:read",
            description="Read Tickets",
        ),
    )
    link = rbac_catalog.grant_role_permission(
        db_session,
        rbac_catalog.GrantRolePermissionCommand(
            context=_context(rbac_catalog.ROLE_WRITE_SCOPE),
            role_id=role.id,
            permission_id=permission.id,
        ),
    )
    items = rbac_catalog.list_role_permissions(
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
    role = rbac_catalog.create_role(
        db_session,
        rbac_catalog.CreateRoleCommand(
            context=_context(rbac_catalog.ROLE_WRITE_SCOPE),
            name="billing",
        ),
    )
    rbac_catalog.update_role(
        db_session,
        rbac_catalog.UpdateRoleCommand(
            context=_context(rbac_catalog.ROLE_WRITE_SCOPE),
            role_id=role.id,
            name="billing-ops",
        ),
    )
    rbac_catalog.deactivate_role(
        db_session,
        rbac_catalog.DeactivateRoleCommand(
            context=_context(rbac_catalog.ROLE_DELETE_SCOPE),
            role_id=role.id,
        ),
    )
    active = rbac_catalog.list_roles(
        db_session,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    inactive = rbac_catalog.list_roles(
        db_session,
        is_active=False,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert all(item.id != role.id for item in active)
    assert any(item.id == role.id for item in inactive)


def test_permission_update(db_session):
    permission = rbac_catalog.create_permission(
        db_session,
        rbac_catalog.CreatePermissionCommand(
            context=_context(rbac_catalog.PERMISSION_WRITE_SCOPE),
            key="billing:write",
            description="Billing Write",
        ),
    )
    updated = rbac_catalog.update_permission(
        db_session,
        rbac_catalog.UpdatePermissionCommand(
            context=_context(rbac_catalog.PERMISSION_WRITE_SCOPE),
            permission_id=permission.id,
            description="Billing Write Access",
            update_description=True,
        ),
    )
    assert updated.description == "Billing Write Access"
