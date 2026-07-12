"""
Staff-account provisioning service (ERP staff sync).

Business logic behind ``app/api/staff_sync.py``: idempotent create+invite,
email lookup, and activate/deactivate for SystemUser accounts driven by the
ERP (HR system of record).
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.rbac import Role, SystemUserRole
from app.models.system_user import SystemUser
from app.services import auth_cache
from app.services import web_system_user_mutations as user_mutations

logger = logging.getLogger(__name__)

ERP_HR_ROLE_SOURCE = "erp_hr"


class UnknownRoleError(ValueError):
    """Requested role name does not exist."""

    def __init__(self, role_names: list[str]):
        self.role_names = role_names
        super().__init__(", ".join(role_names))


def _normalize_role_names(role_names: list[str]) -> list[str]:
    return list(dict.fromkeys(name.strip() for name in role_names if name.strip()))


def get_role_names(db: Session, user: SystemUser) -> list[str]:
    rows = (
        db.query(Role.name)
        .join(SystemUserRole, SystemUserRole.role_id == Role.id)
        .filter(SystemUserRole.system_user_id == user.id, Role.is_active.is_(True))
        .order_by(Role.name)
        .all()
    )
    return [name for (name,) in rows]


def sync_managed_roles(
    db: Session, *, user: SystemUser, role_names: list[str]
) -> list[str]:
    """Make ERP HR's role grants match ``role_names`` without touching local grants."""
    desired_names = _normalize_role_names(role_names)
    if not desired_names:
        raise UnknownRoleError(["At least one active role is required"])

    roles = (
        db.query(Role)
        .filter(Role.name.in_(desired_names), Role.is_active.is_(True))
        .all()
    )
    roles_by_name = {role.name: role for role in roles}
    missing = [name for name in desired_names if name not in roles_by_name]
    if missing:
        raise UnknownRoleError(missing)

    grants = (
        db.query(SystemUserRole).filter(SystemUserRole.system_user_id == user.id).all()
    )
    desired_ids = {roles_by_name[name].id for name in desired_names}
    granted_ids = {grant.role_id for grant in grants}

    for grant in grants:
        if grant.source == ERP_HR_ROLE_SOURCE and grant.role_id not in desired_ids:
            db.delete(grant)

    for role_id in desired_ids - granted_ids:
        db.add(
            SystemUserRole(
                system_user_id=user.id,
                role_id=role_id,
                source=ERP_HR_ROLE_SOURCE,
            )
        )

    db.commit()
    auth_cache.invalidate_principal("system_user", str(user.id))
    return get_role_names(db, user)


def find_by_email(db: Session, email: str) -> SystemUser | None:
    normalized = email.strip().lower()
    return db.query(SystemUser).filter(SystemUser.email == normalized).first()


def create_staff_account(
    db: Session,
    *,
    email: str,
    first_name: str,
    last_name: str,
    role: str,
    roles: list[str] | None = None,
    send_invite: bool = True,
) -> tuple[SystemUser, bool, bool]:
    """Create + invite a staff account; idempotent on email.

    Returns ``(user, created, invited)``. Existing users keep their profile,
    while ERP-managed roles are reconciled; activation is managed separately.
    Raises :class:`UnknownRoleError` when the role name doesn't exist.
    """
    normalized = email.strip().lower()
    desired_roles = _normalize_role_names(roles or [role])
    existing = find_by_email(db, normalized)
    if existing:
        sync_managed_roles(db, user=existing, role_names=desired_roles)
        return existing, False, False

    primary_role = desired_roles[0] if desired_roles else role
    role_row = (
        db.query(Role)
        .filter(Role.name == primary_role, Role.is_active.is_(True))
        .first()
    )
    if not role_row:
        raise UnknownRoleError([primary_role])

    user, _temp_password = user_mutations.create_user_with_role_and_password(
        db,
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        email=normalized,
        role_id=str(role_row.id),
        role_source=ERP_HR_ROLE_SOURCE,
    )
    sync_managed_roles(db, user=user, role_names=desired_roles)

    invited = False
    if send_invite:
        try:
            user_mutations.send_user_invite_for_user(db, user_id=str(user.id))
            invited = True
        except Exception:  # noqa: BLE001 — account stands even if email fails
            logger.warning(
                "Staff invite email failed for %s; resend from users screen",
                normalized,
                exc_info=True,
            )

    return user, True, invited


def set_staff_account_active(
    db: Session, *, user_id: str, is_active: bool
) -> SystemUser:
    """Activate/deactivate; deactivation cascades credentials + sessions.

    Raises ``ValueError`` when the user does not exist (matches
    ``set_user_active``).
    """
    return user_mutations.set_user_active(db, user_id=user_id, is_active=is_active)
