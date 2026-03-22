"""Service layer for user profile (/me) endpoints."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.schemas.auth_flow import AvatarUploadResponse, MeResponse, MeUpdateRequest
from app.services import avatar as avatar_service

logger = logging.getLogger(__name__)


def _build_me_response(
    person: Subscriber | SystemUser,
    roles: list[str],
    scopes: list[str],
) -> MeResponse:
    """Build a MeResponse from a subscriber or system user and auth claims."""
    gender = "unknown"
    preferred_contact_method: str | None = None
    if isinstance(person, Subscriber):
        if person.gender is not None:
            gender = person.gender.value
        if person.preferred_contact_method is not None:
            preferred_contact_method = person.preferred_contact_method.value

    return MeResponse(
        id=person.id,
        first_name=person.first_name,
        last_name=person.last_name,
        display_name=person.display_name,
        avatar_url=getattr(person, "avatar_url", None),
        email=person.email,
        email_verified=getattr(person, "email_verified", False),
        phone=person.phone,
        date_of_birth=getattr(person, "date_of_birth", None),
        gender=gender,
        preferred_contact_method=preferred_contact_method,
        locale=getattr(person, "locale", None),
        timezone=getattr(person, "timezone", None),
        roles=roles,
        scopes=scopes,
    )


def _get_subscriber_or_404(db: Session, subscriber_id: UUID) -> Subscriber:
    """Fetch a subscriber by ID or raise 404."""
    person = db.get(Subscriber, subscriber_id)
    if not person:
        raise HTTPException(status_code=404, detail="User not found")
    return person


def _get_system_user_or_404(db: Session, user_id: UUID) -> SystemUser:
    """Fetch a system user by ID or raise 404."""
    user = db.get(SystemUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def get_me(
    db: Session,
    principal_id: UUID,
    principal_type: str,
    roles: list[str],
    scopes: list[str],
) -> MeResponse:
    """Return the current user's profile information."""
    principal: Subscriber | SystemUser
    if principal_type == "system_user":
        principal = _get_system_user_or_404(db, principal_id)
    else:
        principal = _get_subscriber_or_404(db, principal_id)
    return _build_me_response(principal, roles, scopes)


def update_me(
    db: Session,
    principal_id: UUID,
    principal_type: str,
    payload: MeUpdateRequest,
    roles: list[str],
    scopes: list[str],
) -> MeResponse:
    """Update the current user's profile and return the updated profile."""
    person: Subscriber | SystemUser
    disallowed_fields: set[str]
    if principal_type == "system_user":
        person = _get_system_user_or_404(db, principal_id)
        disallowed_fields = {"date_of_birth", "gender", "preferred_contact_method", "locale", "timezone"}
    else:
        person = _get_subscriber_or_404(db, principal_id)
        disallowed_fields = set()

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field in disallowed_fields:
            continue
        setattr(person, field, value)

    db.flush()
    db.refresh(person)

    return _build_me_response(person, roles, scopes)


async def upload_avatar(
    db: Session,
    subscriber_id: UUID,
    file: UploadFile,
) -> AvatarUploadResponse:
    """Upload and replace the current user's avatar."""
    person = _get_subscriber_or_404(db, subscriber_id)

    # Delete old avatar if exists
    avatar_service.delete_avatar(person.avatar_url)

    # Save new avatar
    avatar_url = await avatar_service.save_avatar(file, str(person.id))

    # Update person record
    person.avatar_url = avatar_url
    db.flush()

    return AvatarUploadResponse(avatar_url=avatar_url)


def delete_avatar(
    db: Session,
    subscriber_id: UUID,
) -> None:
    """Delete the current user's avatar."""
    person = _get_subscriber_or_404(db, subscriber_id)

    avatar_service.delete_avatar(person.avatar_url)
    person.avatar_url = None
    db.flush()
