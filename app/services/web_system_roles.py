"""Service helpers for admin system role/permission listing pages."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.rbac import Permission, Role, SubscriberRole as SubscriberRoleModel


def get_roles_page_data(
    db: Session,
    *,
    page: int,
    per_page: int,
) -> dict[str, object]:
    """Return paginated roles and role-user counts."""
    offset = (page - 1) * per_page

    roles = db.execute(
        select(Role)
        .order_by(Role.created_at.desc())
        .offset(offset)
        .limit(per_page)
    ).scalars().all()

    total = (
        db.scalar(
            select(func.count())
            .select_from(Role)
            .where(Role.is_active.is_(True))
        )
        or 0
    )
    total_pages = (total + per_page - 1) // per_page

    user_counts_rows = db.execute(
        select(
            SubscriberRoleModel.role_id,
            func.count(func.distinct(SubscriberRoleModel.subscriber_id)),
        ).group_by(SubscriberRoleModel.role_id)
    ).all()
    user_counts = {str(role_id): count for role_id, count in user_counts_rows}

    return {
        "roles": roles,
        "user_counts": user_counts,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


def get_permissions_page_data(
    db: Session,
    *,
    page: int,
    per_page: int,
) -> dict[str, object]:
    """Return paginated permissions list and active total."""
    offset = (page - 1) * per_page

    permissions = db.execute(
        select(Permission)
        .order_by(Permission.created_at.desc())
        .offset(offset)
        .limit(per_page)
    ).scalars().all()

    total = (
        db.scalar(
            select(func.count())
            .select_from(Permission)
            .where(Permission.is_active.is_(True))
        )
        or 0
    )
    total_pages = (total + per_page - 1) // per_page

    return {
        "permissions": permissions,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }
