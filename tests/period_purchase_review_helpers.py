"""Staff fixtures for exercising the same approval commands as production."""

from uuid import uuid4

from sqlalchemy import select

from app.models.rbac import Permission, Role, RolePermission, SystemUserRole
from app.models.system_user import SystemUser


def create_review_staff(db):
    principal = SystemUser(
        first_name="Finance",
        last_name="Reviewer",
        email=f"{uuid4()}@example.com",
        is_active=True,
    )
    role = Role(name=f"review-{uuid4()}", is_active=True)
    db.add_all([principal, role])
    db.flush()
    db.add(SystemUserRole(system_user_id=principal.id, role_id=role.id))
    for key in (
        "billing:outage_compensation:approve",
        "billing:prepaid_reconciliation:repair",
    ):
        permission = db.scalar(select(Permission).where(Permission.key == key))
        if permission is None:
            permission = Permission(key=key, is_active=True)
            db.add(permission)
            db.flush()
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db.flush()
    return principal
