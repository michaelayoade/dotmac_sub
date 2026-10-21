"""Strict fresh staff/Person admission; never a generic permission gate or writer."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.field_vendor import FieldVendorUser
from app.models.party import Party
from app.models.rbac import Role, SystemUserRole
from app.models.system_user import SystemUser, SystemUserType
from app.services.domain_errors import DomainError
from app.services.permission_admission import PermissionAdmission
from app.services.permission_authorization import (
    PermissionPrincipal,
    PermissionPrincipalKind,
    has_permission,
)


@dataclass(frozen=True, slots=True)
class AuthorizedStaffPerson:
    system_user_id: UUID
    person_id: UUID


def revalidate_staff_admission(
    db: Session, *, admission: PermissionAdmission, permission: str, write: bool
) -> AuthorizedStaffPerson:
    """Fresh server-owned RBAC, staff identity and leave check; never commits."""
    from app.services.erp_staff_access import active_leave_restriction

    def denied() -> DomainError:
        return DomainError(
            code="auth.staff_permission_admission.denied",
            message="Current staff permission admission required",
        )

    if (
        admission.principal_type != "system_user"
        or permission not in admission.admitted_permissions
    ):
        raise denied()
    user = db.scalar(
        select(SystemUser)
        .where(SystemUser.id == admission.principal_id)
        .execution_options(populate_existing=True)
    )
    if (
        user is None
        or not user.is_active
        or user.person_party_id is None
        or user.user_type != SystemUserType.system_user
    ):
        raise denied()
    if (
        db.scalar(
            select(FieldVendorUser.id)
            .where(FieldVendorUser.system_user_id == user.id)
            .limit(1)
        )
        is not None
    ):
        raise denied()
    person = db.scalar(
        select(Party)
        .where(Party.id == user.person_party_id)
        .execution_options(populate_existing=True)
    )
    if person is None or person.party_type != "person" or person.status != "active":
        raise denied()
    roles = db.scalars(
        select(Role.name)
        .join(SystemUserRole, SystemUserRole.role_id == Role.id)
        .where(SystemUserRole.system_user_id == user.id, Role.is_active.is_(True))
    ).all()
    auth = PermissionPrincipal(
        user.id, PermissionPrincipalKind.system_user, frozenset(roles), frozenset()
    )
    if not has_permission(auth, db, permission):
        raise denied()
    if write and active_leave_restriction(db, system_user_id=user.id) is not None:
        raise denied()
    return AuthorizedStaffPerson(user.id, person.id)
