"""Access credential management service."""

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.catalog import AccessCredential, RadiusProfile
from app.models.subscriber import Subscriber
from app.schemas.catalog import AccessCredentialCreate, AccessCredentialUpdate
from app.services.common import apply_ordering, apply_pagination
from app.services.crud import CRUDManager
from app.services.query_builders import apply_active_state, apply_optional_equals

logger = logging.getLogger(__name__)


def _sync_credential_to_radius(db: Session, credential: AccessCredential) -> None:
    """Sync credential to RADIUS immediately (non-blocking)."""
    try:
        from app.services.radius import sync_credential_to_radius
        sync_credential_to_radius(db, credential)
    except Exception as exc:
        # Don't fail the operation if RADIUS sync fails
        logger.warning(f"Failed to sync credential {credential.username} to RADIUS: {exc}")


class AccessCredentials(CRUDManager[AccessCredential]):
    model = AccessCredential
    not_found_detail = "Access credential not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def create(db: Session, payload: AccessCredentialCreate):
        subscriber = db.get(Subscriber, payload.subscriber_id)
        if not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        if payload.radius_profile_id:
            profile = db.get(RadiusProfile, payload.radius_profile_id)
            if not profile:
                raise HTTPException(status_code=404, detail="RADIUS profile not found")
        credential = AccessCredential(**payload.model_dump())
        db.add(credential)
        db.commit()
        db.refresh(credential)

        # Sync to RADIUS immediately
        _sync_credential_to_radius(db, credential)

        return credential

    @classmethod
    def get(cls, db: Session, credential_id: str):
        return super().get(db, credential_id)

    @staticmethod
    def list(
        db: Session,
        subscriber_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(AccessCredential)
        query = apply_optional_equals(query, {AccessCredential.subscriber_id: subscriber_id})
        query = apply_active_state(query, AccessCredential.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": AccessCredential.created_at, "username": AccessCredential.username},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, credential_id: str, payload: AccessCredentialUpdate):
        credential = db.get(AccessCredential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="Access credential not found")
        data = payload.model_dump(exclude_unset=True)
        if "subscriber_id" in data:
            subscriber = db.get(Subscriber, data["subscriber_id"])
            if not subscriber:
                raise HTTPException(status_code=404, detail="Subscriber not found")
        if "radius_profile_id" in data and data["radius_profile_id"]:
            profile = db.get(RadiusProfile, data["radius_profile_id"])
            if not profile:
                raise HTTPException(status_code=404, detail="RADIUS profile not found")
        for key, value in data.items():
            setattr(credential, key, value)
        db.commit()
        db.refresh(credential)

        # Sync to RADIUS immediately
        _sync_credential_to_radius(db, credential)

        return credential

    @classmethod
    def delete(cls, db: Session, credential_id: str):
        return super().delete(db, credential_id)
