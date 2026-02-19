"""Service helpers for admin system user listing/statistics."""

from __future__ import annotations

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from app.models.auth import MFAMethod, UserCredential
from app.models.rbac import Role
from app.models.rbac import SubscriberRole as SubscriberRoleModel
from app.models.subscriber import Subscriber
from app.services.common import coerce_uuid


def get_user_stats(db: Session) -> dict[str, int]:
    """Return summary statistics for system users page."""
    total = db.scalar(select(func.count()).select_from(Subscriber)) or 0
    active = (
        db.scalar(
            select(func.count())
            .select_from(Subscriber)
            .where(Subscriber.is_active.is_(True))
        )
        or 0
    )

    admin_role_id = db.scalar(
        select(Role.id)
        .where(func.lower(Role.name) == "admin")
        .where(Role.is_active.is_(True))
        .limit(1)
    )
    admins = 0
    if admin_role_id:
        admins = (
            db.scalar(
                select(func.count(func.distinct(SubscriberRoleModel.subscriber_id))).where(
                    SubscriberRoleModel.role_id == admin_role_id
                )
            )
            or 0
        )

    active_credential = exists(
        select(UserCredential.id)
        .where(UserCredential.subscriber_id == Subscriber.id)
        .where(UserCredential.is_active.is_(True))
    )
    pending_credential = exists(
        select(UserCredential.id)
        .where(UserCredential.subscriber_id == Subscriber.id)
        .where(UserCredential.is_active.is_(True))
        .where(UserCredential.must_change_password.is_(True))
    )
    pending = (
        db.scalar(
            select(func.count())
            .select_from(Subscriber)
            .where(or_(~active_credential, pending_credential))
        )
        or 0
    )

    return {"total": total, "active": active, "admins": admins, "pending": pending}


def list_users(
    db: Session,
    *,
    search: str | None,
    role_id: str | None,
    status: str | None,
    offset: int,
    limit: int,
) -> tuple[list[dict], int]:
    """Return paginated users list with role and auth metadata."""
    stmt = select(Subscriber)

    if search:
        search_value = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                Subscriber.first_name.ilike(search_value),
                Subscriber.last_name.ilike(search_value),
                Subscriber.email.ilike(search_value),
                Subscriber.display_name.ilike(search_value),
            )
        )

    if role_id:
        stmt = (
            stmt.join(SubscriberRoleModel, SubscriberRoleModel.subscriber_id == Subscriber.id)
            .where(SubscriberRoleModel.role_id == coerce_uuid(role_id))
            .distinct()
        )

    if status:
        if status == "active":
            stmt = stmt.where(Subscriber.is_active.is_(True))
        elif status == "inactive":
            stmt = stmt.where(Subscriber.is_active.is_(False))
        elif status == "pending":
            active_credential = exists(
                select(UserCredential.id)
                .where(UserCredential.subscriber_id == Subscriber.id)
                .where(UserCredential.is_active.is_(True))
            )
            pending_credential = exists(
                select(UserCredential.id)
                .where(UserCredential.subscriber_id == Subscriber.id)
                .where(UserCredential.is_active.is_(True))
                .where(UserCredential.must_change_password.is_(True))
            )
            stmt = stmt.where(or_(~active_credential, pending_credential))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    people = db.execute(
        stmt.order_by(Subscriber.last_name.asc(), Subscriber.first_name.asc())
        .offset(offset)
        .limit(limit)
    ).scalars().all()

    person_ids = [person.id for person in people]
    if not person_ids:
        return [], total

    credentials = db.execute(
        select(UserCredential).where(UserCredential.subscriber_id.in_(person_ids))
    ).scalars().all()

    credential_info: dict = {}
    for credential in credentials:
        info = credential_info.setdefault(
            credential.subscriber_id,
            {"last_login": None, "has_active": False, "must_change_password": False},
        )
        if credential.is_active:
            info["has_active"] = True
            if credential.must_change_password:
                info["must_change_password"] = True
        if credential.last_login_at and (
            info["last_login"] is None or credential.last_login_at > info["last_login"]
        ):
            info["last_login"] = credential.last_login_at

    mfa_enabled = set(
        db.execute(
            select(MFAMethod.subscriber_id)
            .where(MFAMethod.subscriber_id.in_(person_ids))
            .where(MFAMethod.enabled.is_(True))
            .where(MFAMethod.is_active.is_(True))
        )
        .scalars()
        .all()
    )

    roles_rows = db.execute(
        select(SubscriberRoleModel, Role)
        .join(Role, Role.id == SubscriberRoleModel.role_id)
        .where(SubscriberRoleModel.subscriber_id.in_(person_ids))
        .order_by(SubscriberRoleModel.assigned_at.desc())
    ).all()
    role_map: dict = {}
    for person_role, role in roles_rows:
        role_map.setdefault(person_role.subscriber_id, []).append(
            {
                "id": str(role.id),
                "name": role.name,
                "is_active": role.is_active,
            }
        )

    users: list[dict] = []
    for person in people:
        name = person.display_name or f"{person.first_name} {person.last_name}".strip()
        info = credential_info.get(person.id, {})
        users.append(
            {
                "id": str(person.id),
                "name": name,
                "email": person.email,
                "roles": role_map.get(person.id, []),
                "is_active": bool(person.is_active),
                "mfa_enabled": person.id in mfa_enabled,
                "last_login": info.get("last_login"),
            }
        )

    return users, total


def list_active_roles(db: Session) -> list[Role]:
    roles = (
        db.execute(
            select(Role)
            .where(Role.is_active.is_(True))
            .order_by(Role.name.asc())
            .limit(500)
        )
        .scalars()
        .all()
    )
    return list(roles)


def build_users_page_state(
    db: Session,
    *,
    search: str | None,
    role: str | None,
    status: str | None,
    offset: int,
    limit: int,
) -> dict[str, object]:
    users, total = list_users(
        db,
        search=search,
        role_id=role,
        status=status,
        offset=offset,
        limit=limit,
    )
    return {
        "users": users,
        "search": search,
        "role": role,
        "status": status,
        "stats": get_user_stats(db),
        "roles": list_active_roles(db),
        "pagination": total > limit,
        "total": total,
        "offset": offset,
        "limit": limit,
    }
