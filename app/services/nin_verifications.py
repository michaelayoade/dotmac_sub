"""Subscriber NIN verification ownership: persistence and outcome writers.

This module is the single writer for NIN verification outcomes. The outcome
transition (``pending`` → ``success``/``failed`` with match evidence) and the
subscriber-facing projection (``metadata_["nin_verified"]`` /
``nin_last_checked_at``) always move together through the writers below;
tasks, routes, and web handlers stay thin adapters around them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

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


def _project_subscriber_nin_status(
    subscriber: Subscriber,
    *,
    is_verified: bool,
    checked_at: datetime,
) -> None:
    """Project the verification outcome onto the subscriber's metadata.

    The projection is derived state owned by this module — no other writer
    may set ``nin_verified`` / ``nin_last_checked_at``.
    """
    metadata = dict(subscriber.metadata_ or {})
    metadata["nin_verified"] = is_verified
    metadata["nin_last_checked_at"] = checked_at.isoformat()
    subscriber.metadata_ = metadata


def record_nin_verification_outcome_committed(
    db: Session,
    verification: SubscriberNINVerification,
    subscriber: Subscriber,
    *,
    match_result: dict[str, Any],
    mono_response: dict[str, Any] | None,
) -> dict[str, Any]:
    """Decide and persist the outcome of a completed Mono NIN lookup.

    Owns the ``is_match`` → status mapping, the verification-row evidence
    fields, and the subscriber projection as one committed transition.
    """
    checked_at = datetime.now(UTC)
    is_match = bool(match_result["is_match"])
    verification.status = (
        NINVerificationStatus.success if is_match else NINVerificationStatus.failed
    )
    verification.is_match = is_match
    verification.match_score = int(match_result["match_score"])
    verification.mono_response = mono_response
    verification.failure_reason = None if is_match else "Subscriber identity mismatch"
    verification.verified_at = checked_at
    _project_subscriber_nin_status(
        subscriber, is_verified=is_match, checked_at=checked_at
    )
    db.commit()
    return {
        "status": verification.status.value,
        "is_match": is_match,
        "match_score": verification.match_score,
    }


def record_nin_verification_failure_committed(
    db: Session,
    verification: SubscriberNINVerification,
    subscriber: Subscriber | None,
    *,
    reason: str,
    mono_response: dict[str, Any] | None = None,
) -> None:
    """Persist a terminal lookup failure (provider error, retries exhausted).

    Mirrors the outcome writer: failure evidence on the verification row and
    the ``nin_verified=False`` projection commit together.
    """
    checked_at = datetime.now(UTC)
    verification.status = NINVerificationStatus.failed
    verification.is_match = False
    verification.match_score = 0
    verification.failure_reason = reason
    verification.mono_response = mono_response
    verification.verified_at = checked_at
    if subscriber is not None:
        _project_subscriber_nin_status(
            subscriber, is_verified=False, checked_at=checked_at
        )
    db.commit()


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
