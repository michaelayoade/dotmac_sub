"""Vendor-token context for field vendor routes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from fastapi import Depends, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.auth import AuthProvider, UserCredential
from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor
from app.services import staff_party_authentication
from app.services.auth_dependencies import require_user_auth
from app.services.auth_flow import decode_access_token
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError

VENDOR_ACCESS_MESSAGE = "Vendor access required"


class VendorAdmissionRequiredError(DomainError):
    """The principal behind an issued access token is not a vendor operator."""

    def __init__(self) -> None:
        super().__init__(
            code="vendor_auth.access_required",
            message=VENDOR_ACCESS_MESSAGE,
        )


class VendorLoginEligibilityStatus(StrEnum):
    ELIGIBLE = "eligible"
    UNKNOWN_IDENTITY = "unknown_identity"
    VENDOR_ACCESS_REQUIRED = "vendor_access_required"


@dataclass(frozen=True, slots=True)
class VendorLoginEligibilityQuery:
    identifier: str


@dataclass(frozen=True, slots=True)
class VendorLoginEligibilityResult:
    status: VendorLoginEligibilityStatus
    system_user_id: UUID | None = None
    vendor_user_id: UUID | None = None
    vendor_id: UUID | None = None


def _active_membership(db: Session, system_user_id: UUID) -> FieldVendorUser | None:
    return db.scalar(
        select(FieldVendorUser)
        .join(FieldVendor, FieldVendor.id == FieldVendorUser.vendor_id)
        .where(FieldVendorUser.system_user_id == system_user_id)
        .where(FieldVendorUser.is_active.is_(True))
        .where(FieldVendor.is_active.is_(True))
        .order_by(FieldVendorUser.created_at.desc())
        .limit(1)
    )


def resolve_vendor_login_eligibility(
    db: Session,
    query: VendorLoginEligibilityQuery,
) -> VendorLoginEligibilityResult:
    """Resolve vendor admission before credential verification.

    Unknown identifiers deliberately remain indistinguishable from bad
    credentials. Known identities without an active vendor membership receive
    the vendor-specific admission result.
    """

    identifier = query.identifier.strip().lower()
    if not identifier:
        return VendorLoginEligibilityResult(
            status=VendorLoginEligibilityStatus.UNKNOWN_IDENTITY
        )
    credential = db.scalar(
        select(UserCredential)
        .outerjoin(SystemUser, SystemUser.id == UserCredential.system_user_id)
        .where(UserCredential.provider == AuthProvider.local)
        .where(UserCredential.is_active.is_(True))
        .where(
            or_(
                UserCredential.username == query.identifier.strip(),
                func.lower(SystemUser.email) == identifier,
            )
        )
        .order_by(UserCredential.created_at.desc())
        .limit(1)
    )
    if credential is None:
        return VendorLoginEligibilityResult(
            status=VendorLoginEligibilityStatus.UNKNOWN_IDENTITY
        )
    if credential.system_user_id is None:
        return VendorLoginEligibilityResult(
            status=VendorLoginEligibilityStatus.VENDOR_ACCESS_REQUIRED
        )
    # Vendor login eligibility is a staff authentication decision, so it
    # resolves through the same owner as login, refresh and session validation.
    # A projection this path cannot resolve is not eligible — never fall back to
    # the legacy key just because this entry point is not the main one.
    try:
        principal = staff_party_authentication.resolve_staff_principal(db, credential)
    except staff_party_authentication.StaffProjectionError:
        return VendorLoginEligibilityResult(
            status=VendorLoginEligibilityStatus.VENDOR_ACCESS_REQUIRED,
            system_user_id=credential.system_user_id,
        )
    membership = _active_membership(db, credential.system_user_id)
    if principal is None or not principal.is_active or membership is None:
        return VendorLoginEligibilityResult(
            status=VendorLoginEligibilityStatus.VENDOR_ACCESS_REQUIRED,
            system_user_id=credential.system_user_id,
        )
    return VendorLoginEligibilityResult(
        status=VendorLoginEligibilityStatus.ELIGIBLE,
        system_user_id=credential.system_user_id,
        vendor_user_id=membership.id,
        vendor_id=membership.vendor_id,
    )


def _system_user_id(auth: dict) -> str:
    if auth.get("principal_type") != "system_user":
        raise HTTPException(status_code=403, detail="Vendor access required")
    return str(auth.get("principal_id") or auth.get("person_id") or "")


def vendor_context(db: Session, auth: dict) -> dict:
    system_user_id = coerce_uuid(_system_user_id(auth))
    membership = _active_membership(db, system_user_id)
    if membership is None:
        raise HTTPException(status_code=403, detail="Vendor access required")
    native_vendor = None
    if membership.vendor.crm_vendor_id:
        try:
            native_vendor = db.get(Vendor, coerce_uuid(membership.vendor.crm_vendor_id))
        except (TypeError, ValueError):
            native_vendor = None
    return {
        **auth,
        "vendor_user_id": str(membership.id),
        "vendor_id": str(membership.vendor_id),
        "vendor_role": membership.role,
        "vendor_user": membership,
        "vendor": membership.vendor,
        "native_vendor_id": str(native_vendor.id) if native_vendor else None,
        "native_vendor": native_vendor,
    }


@dataclass(frozen=True, slots=True)
class VendorTokenAdmission:
    """Which vendor an already-issued access token is admitted to operate for."""

    system_user_id: UUID
    vendor_user_id: UUID
    vendor_id: UUID
    native_vendor_id: UUID | None


def resolve_vendor_admission_for_access_token(
    db: Session,
    *,
    access_token: str,
) -> VendorTokenAdmission:
    """Decide whether an issued access token may act as a vendor operator.

    Login, MFA verification and refresh all mint a general principal token
    through ``app.services.auth_flow`` — that owner knows nothing about vendor
    membership. Every vendor transport must therefore re-check the token it was
    just handed, or a non-vendor staff principal who authenticates through a
    vendor endpoint receives a working one. ``app/api/vendor_auth.py`` calls
    this. The browser adapter still carries its own private copy of the same
    composition (``app/web/vendor_auth_flow._require_access_token_vendor``),
    written before this function existed; it should adopt this one so the
    decision has a single implementation as well as a single owner.

    Raises ``VendorAdmissionRequiredError`` when the token's principal is not an
    active member of an active vendor. The caller decides the transport
    consequence — refusing the response, and discarding the session it minted.
    """

    payload = decode_access_token(db, access_token)
    principal_id = str(payload.get("principal_id") or payload.get("sub") or "")
    principal_type = str(payload.get("principal_type") or "")
    if not principal_id or principal_type != "system_user":
        raise VendorAdmissionRequiredError()
    try:
        context = vendor_context(
            db,
            {
                "principal_id": principal_id,
                "person_id": principal_id,
                "principal_type": principal_type,
                "session_id": str(payload.get("session_id") or ""),
                "roles": [],
                "scopes": [],
            },
        )
    except HTTPException as exc:
        if exc.status_code == 403:
            raise VendorAdmissionRequiredError() from exc
        raise
    native_vendor_id = context.get("native_vendor_id")
    return VendorTokenAdmission(
        system_user_id=coerce_uuid(principal_id),
        vendor_user_id=coerce_uuid(str(context["vendor_user_id"])),
        vendor_id=coerce_uuid(str(context["vendor_id"])),
        native_vendor_id=(
            coerce_uuid(str(native_vendor_id)) if native_vendor_id else None
        ),
    )


def require_field_vendor_token(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> dict:
    return vendor_context(db, auth)


def require_native_vendor_context(
    vendor: dict = Depends(require_field_vendor_token),
) -> dict:
    if vendor.get("native_vendor") is None:
        raise HTTPException(
            status_code=409,
            detail="Vendor account is not linked to the native vendor domain",
        )
    return vendor
