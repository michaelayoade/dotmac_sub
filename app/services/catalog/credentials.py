"""Access credential management service."""

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.catalog import AccessCredential, RadiusProfile
from app.models.subscriber import Subscriber
from app.schemas.catalog import AccessCredentialCreate, AccessCredentialUpdate
from app.services.common import apply_ordering, apply_pagination
from app.services.credential_crypto import reject_reserved_service_password
from app.services.crud import CRUDManager
from app.services.query_builders import apply_active_state, apply_optional_equals

logger = logging.getLogger(__name__)


def _refuse_reserved_secret(secret_hash: str | None) -> None:
    """Refuse a secret reference or at-rest wrapper written through the API.

    ``secret_hash`` is stored through the credential encryption layer, which
    keeps a reference raw and later resolves it; a service credential must
    never be able to name an app secret. The message never echoes the value.
    """
    try:
        reject_reserved_service_password(secret_hash)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _sync_credential_to_radius(db: Session, credential: AccessCredential) -> None:
    """Reconcile RADIUS state for any active subscriptions using this credential."""
    try:
        from app.models.catalog import Subscription, SubscriptionStatus
        from app.services.radius import reconcile_subscription_connectivity

        active_subscriptions = (
            db.query(Subscription)
            .filter(Subscription.subscriber_id == credential.subscriber_id)
            .filter(Subscription.status == SubscriptionStatus.active)
            .all()
        )
        for subscription in active_subscriptions:
            reconcile_subscription_connectivity(
                db, str(subscription.id)
            ).require_projected()
    except Exception as exc:
        # Don't fail the operation if RADIUS sync fails
        logger.warning(
            f"Failed to sync credential {credential.username} to RADIUS: {exc}"
        )


def _refuse_unrecorded_profile(db: Session, subscription_id, profile_id) -> None:
    """D1: a credential may carry only its service package's own profile."""
    from app.models.catalog import Subscription
    from app.services import access_rate_profile

    subscription = db.get(Subscription, subscription_id) if subscription_id else None
    access_rate_profile.refuse_unrecorded_override(
        db,
        offer_id=subscription.offer_id if subscription is not None else None,
        profile_id=profile_id,
    )


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
        if payload.subscription_id:
            from app.models.catalog import Subscription

            subscription = db.get(Subscription, payload.subscription_id)
            if not subscription:
                raise HTTPException(status_code=404, detail="Subscription not found")
            if subscription.subscriber_id != payload.subscriber_id:
                raise HTTPException(
                    status_code=400,
                    detail="Subscription does not belong to the credential subscriber",
                )
        if payload.radius_profile_id:
            profile = db.get(RadiusProfile, payload.radius_profile_id)
            if not profile:
                raise HTTPException(status_code=404, detail="RADIUS profile not found")
            _refuse_unrecorded_profile(
                db, payload.subscription_id, payload.radius_profile_id
            )
        _refuse_reserved_secret(payload.secret_hash)
        # The profile field is accepted for API compatibility but never
        # stored: the rate owner derives a service's rate from its package.
        credential = AccessCredential(
            **payload.model_dump(exclude={"radius_profile_id"})
        )
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
        query = apply_optional_equals(
            query, {AccessCredential.subscriber_id: subscriber_id}
        )
        query = apply_active_state(query, AccessCredential.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": AccessCredential.created_at,
                "username": AccessCredential.username,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, credential_id: str, payload: AccessCredentialUpdate):
        credential = db.get(AccessCredential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="Access credential not found")
        data = payload.model_dump(exclude_unset=True)
        _refuse_reserved_secret(data.get("secret_hash"))
        if "subscriber_id" in data:
            subscriber = db.get(Subscriber, data["subscriber_id"])
            if not subscriber:
                raise HTTPException(status_code=404, detail="Subscriber not found")
        target_subscriber_id = data.get("subscriber_id", credential.subscriber_id)
        if "subscription_id" in data and data["subscription_id"]:
            from app.models.catalog import Subscription

            subscription = db.get(Subscription, data["subscription_id"])
            if not subscription:
                raise HTTPException(status_code=404, detail="Subscription not found")
            if subscription.subscriber_id != target_subscriber_id:
                raise HTTPException(
                    status_code=400,
                    detail="Subscription does not belong to the credential subscriber",
                )
        if "radius_profile_id" in data and data["radius_profile_id"]:
            profile = db.get(RadiusProfile, data["radius_profile_id"])
            if not profile:
                raise HTTPException(status_code=404, detail="RADIUS profile not found")
            _refuse_unrecorded_profile(
                db,
                data.get("subscription_id", credential.subscription_id),
                data["radius_profile_id"],
            )
        data.pop("radius_profile_id", None)  # accepted, never stored (see create)
        for key, value in data.items():
            setattr(credential, key, value)
        if "is_active" in data:
            # An explicit REST/admin change: record why, or clear on reactivation.
            credential.deactivation_reason = None if data["is_active"] else "manual"
        db.commit()
        db.refresh(credential)

        # Sync to RADIUS immediately
        _sync_credential_to_radius(db, credential)

        return credential

    @classmethod
    def delete(cls, db: Session, credential_id: str):
        credential = db.get(AccessCredential, credential_id)
        if credential is not None:
            credential.deactivation_reason = "manual"
        return super().delete(db, credential_id)
