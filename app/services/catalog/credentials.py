"""Access credential management service."""

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.catalog import AccessCredential, RadiusProfile
from app.models.subscriber import SubscriberAccount
from app.services.common import apply_ordering, apply_pagination
from app.services.response import ListResponseMixin
from app.schemas.catalog import AccessCredentialCreate, AccessCredentialUpdate

logger = logging.getLogger(__name__)


def _sync_credential_to_radius(db: Session, credential: AccessCredential) -> None:
    """Sync credential to RADIUS immediately (non-blocking)."""
    try:
        from app.services.radius import sync_credential_to_radius
        sync_credential_to_radius(db, credential)
    except Exception as exc:
        # Don't fail the operation if RADIUS sync fails
        logger.warning(f"Failed to sync credential {credential.username} to RADIUS: {exc}")


class AccessCredentials(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: AccessCredentialCreate):
        account = db.get(SubscriberAccount, payload.account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Subscriber account not found")
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

    @staticmethod
    def get(db: Session, credential_id: str):
        credential = db.get(AccessCredential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="Access credential not found")
        return credential

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(AccessCredential)
        if account_id:
            query = query.filter(AccessCredential.account_id == account_id)
        if is_active is None:
            query = query.filter(AccessCredential.is_active.is_(True))
        else:
            query = query.filter(AccessCredential.is_active == is_active)
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
        if "account_id" in data:
            account = db.get(SubscriberAccount, data["account_id"])
            if not account:
                raise HTTPException(status_code=404, detail="Subscriber account not found")
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

    @staticmethod
    def delete(db: Session, credential_id: str):
        credential = db.get(AccessCredential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="Access credential not found")
        credential.is_active = False
        db.commit()
