"""Subscriber NIN verification persistence helpers."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.subscriber import (
    NINVerificationStatus,
    Subscriber,
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


def begin_subscriber_nin_verification_committed(
    db: Session,
    *,
    subscriber_id: str | uuid.UUID,
    normalized_nin: str,
) -> dict[str, str]:
    from app.services.queue_adapter import enqueue_task
    from app.tasks.nin_tasks import verify_nin_task

    try:
        subscriber_uuid = uuid.UUID(str(subscriber_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Subscriber not found") from exc

    subscriber = db.get(Subscriber, subscriber_uuid)
    if subscriber is None:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    try:
        verification = get_or_create_pending_nin_verification(
            db,
            subscriber_uuid,
            normalized_nin,
        )
        db.commit()

        if verification.status == NINVerificationStatus.success:
            return {"status": "already_verified", "task_id": ""}

        dispatch = enqueue_task(
            verify_nin_task,
            args=[str(subscriber.id), normalized_nin],
            queue="nin",
            source="subscriber_nin_verification",
        )
        if not dispatch.queued:
            verification.status = NINVerificationStatus.failed
            verification.is_match = False
            verification.match_score = 0
            verification.failure_reason = (
                dispatch.error or "NIN verification could not be queued"
            )
            db.commit()
            raise HTTPException(
                status_code=503,
                detail=dispatch.error or "NIN verification could not be queued",
            )
        return {"status": "queued", "task_id": dispatch.task_id or ""}
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        raise
