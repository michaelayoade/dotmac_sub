"""
Staff-account provisioning service (ERP staff sync).

Business logic behind ``app/api/staff_sync.py``: idempotent create+invite,
email lookup, and activate/deactivate for SystemUser accounts driven by the
ERP (HR system of record).
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.rbac import Role
from app.models.system_user import SystemUser
from app.services import web_system_user_mutations as user_mutations

logger = logging.getLogger(__name__)


class UnknownRoleError(ValueError):
    """Requested role name does not exist."""


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
    send_invite: bool = True,
) -> tuple[SystemUser, bool, bool]:
    """Create + invite a staff account; idempotent on email.

    Returns ``(user, created, invited)``. An existing user is returned
    untouched (``created=False``) — activation state is managed separately.
    Raises :class:`UnknownRoleError` when the role name doesn't exist.
    """
    normalized = email.strip().lower()
    existing = find_by_email(db, normalized)
    if existing:
        return existing, False, False

    role_row = db.query(Role).filter(Role.name == role).first()
    if not role_row:
        raise UnknownRoleError(role)

    user, _temp_password = user_mutations.create_user_with_role_and_password(
        db,
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        email=normalized,
        role_id=str(role_row.id),
    )

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
