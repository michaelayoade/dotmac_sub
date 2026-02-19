"""Service layer for user profile (/me) endpoints."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.schemas.auth_flow import AvatarUploadResponse, MeResponse, MeUpdateRequest
from app.services import avatar as avatar_service

logger = logging.getLogger(__name__)


def _build_me_response(
    person: Subscriber,
    roles: list[str],
    scopes: list[str],
) -> MeResponse:
    """Build a MeResponse from a Subscriber and auth claims."""
    return MeResponse(
        id=person.id,
        first_name=person.first_name,
        last_name=person.last_name,
        display_name=person.display_name,
        avatar_url=person.avatar_url,
        email=person.email,
        email_verified=person.email_verified,
        phone=person.phone,
        date_of_birth=person.date_of_birth,
        gender=person.gender.value if person.gender else "unknown",
        preferred_contact_method=(
            person.preferred_contact_method.value
            if person.preferred_contact_method
            else None
        ),
        locale=person.locale,
        timezone=person.timezone,
        roles=roles,
        scopes=scopes,
    )


def _get_subscriber_or_404(db: Session, subscriber_id: UUID) -> Subscriber:
    """Fetch a subscriber by ID or raise 404."""
    person = db.get(Subscriber, subscriber_id)
    if not person:
        raise HTTPException(status_code=404, detail="User not found")
    return person


def get_me(
    db: Session,
    subscriber_id: UUID,
    roles: list[str],
    scopes: list[str],
) -> MeResponse:
    """Return the current user's profile information."""
    person = _get_subscriber_or_404(db, subscriber_id)
    return _build_me_response(person, roles, scopes)


def update_me(
    db: Session,
    subscriber_id: UUID,
    payload: MeUpdateRequest,
    roles: list[str],
    scopes: list[str],
) -> MeResponse:
    """Update the current user's profile and return the updated profile."""
    person = _get_subscriber_or_404(db, subscriber_id)

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
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
