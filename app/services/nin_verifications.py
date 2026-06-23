"""Subscriber NIN verification persistence helpers."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.subscriber import (
    NINVerificationStatus,
    SubscriberNINVerification,
)


def latest_nin_verification(
    db: Session,
    subscriber_id: uuid.UUID,
) -> SubscriberNINVerification | None:
    stmt = (
        select(SubscriberNINVerification)
        .where(SubscriberNINVerification.subscriber_id == subscriber_id)
        .order_by(SubscriberNINVerification.created_at.desc())
    )
    return db.scalars(stmt).first()


def get_or_create_pending_nin_verification(
    db: Session,
    subscriber_id: uuid.UUID,
    nin: str,
    *,
    allow_reverify: bool = False,
) -> SubscriberNINVerification:
    # Lock once verified: a subscriber with a prior `success` is treated as
    # final — return it instead of spawning another (paid) Mono lookup or
    # letting a different NIN overwrite a confirmed identity. Genuine
    # corrections go through an explicit admin path (allow_reverify=True).
    if not allow_reverify:
        verified = db.scalars(
            select(SubscriberNINVerification)
            .where(
                SubscriberNINVerification.subscriber_id == subscriber_id,
                SubscriberNINVerification.status == NINVerificationStatus.success,
            )
            .order_by(SubscriberNINVerification.created_at.desc())
        ).first()
        if verified is not None:
            return verified

    stmt = (
        select(SubscriberNINVerification)
        .where(
            SubscriberNINVerification.subscriber_id == subscriber_id,
            SubscriberNINVerification.nin == nin,
            SubscriberNINVerification.status == NINVerificationStatus.pending,
        )
        .order_by(SubscriberNINVerification.created_at.desc())
    )
    verification = db.scalars(stmt).first()
    if verification is not None:
        return verification

    verification = SubscriberNINVerification(
        subscriber_id=subscriber_id,
        nin=nin,
        status=NINVerificationStatus.pending,
    )
    db.add(verification)
    db.flush()
    return verification
