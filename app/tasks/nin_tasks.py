from __future__ import annotations

import logging
import uuid
from typing import Any

from celery.exceptions import MaxRetriesExceededError, Retry
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.models.subscriber import (
    NINVerificationStatus,
    Subscriber,
    SubscriberNINVerification,
)
from app.services import nin_service
from app.services.db_session_adapter import db_session_adapter
from app.services.nin_matching import (
    mask_nin,
    match_subscriber_nin_response,
    normalize_nin,
)
from app.services.nin_verifications import (
    record_nin_verification_failure_committed,
    record_nin_verification_outcome_committed,
)

logger = logging.getLogger(__name__)


def _latest_pending_verification(
    db: Session,
    subscriber_id: uuid.UUID,
    nin: str,
) -> SubscriberNINVerification | None:
    return (
        db.query(SubscriberNINVerification)
        .filter(
            SubscriberNINVerification.subscriber_id == subscriber_id,
            SubscriberNINVerification.nin == nin,
            SubscriberNINVerification.status == NINVerificationStatus.pending,
        )
        .order_by(SubscriberNINVerification.created_at.desc())
        .first()
    )


def _get_or_create_pending_verification(
    db: Session,
    subscriber_id: uuid.UUID,
    nin: str,
) -> SubscriberNINVerification:
    verification = _latest_pending_verification(db, subscriber_id, nin)
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


@celery_app.task(
    bind=True,
    name="app.tasks.nin_tasks.verify_nin_task",
    max_retries=3,
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def verify_nin_task(self, subscriber_id: str, nin: str) -> dict[str, Any]:
    normalized_nin = normalize_nin(nin)
    db = db_session_adapter.create_session()
    try:
        subscriber_uuid = uuid.UUID(str(subscriber_id))
        subscriber = db.get(Subscriber, subscriber_uuid)
        if subscriber is None:
            logger.warning(
                "nin_verification_subscriber_missing",
                extra={"subscriber_id": str(subscriber_id)},
            )
            return {"status": "failed", "reason": "Subscriber not found"}

        verification = _get_or_create_pending_verification(
            db,
            subscriber_uuid,
            normalized_nin,
        )
        db.commit()

        try:
            lookup = nin_service.lookup_nin(normalized_nin)
        except nin_service.MonoNINError as exc:
            reason = str(exc)
            if exc.retryable and self.request.retries < self.max_retries:
                verification.failure_reason = reason
                db.commit()
                raise self.retry(exc=exc)

            record_nin_verification_failure_committed(
                db,
                verification,
                subscriber,
                reason=reason,
                mono_response=exc.response_payload or {"error": reason},
            )
            logger.warning(
                "nin_verification_failed",
                extra={
                    "subscriber_id": str(subscriber_id),
                    "nin": mask_nin(normalized_nin),
                    "retryable": exc.retryable,
                },
            )
            return {"status": "failed", "reason": reason}

        match_result = match_subscriber_nin_response(subscriber, lookup["data"])
        return record_nin_verification_outcome_committed(
            db,
            verification,
            subscriber,
            match_result=match_result,
            mono_response=lookup["raw"],
        )
    except Retry:
        raise
    except MaxRetriesExceededError as exc:
        db.rollback()
        logger.exception(
            "nin_verification_retry_exhausted",
            extra={
                "subscriber_id": str(subscriber_id),
                "nin": mask_nin(normalized_nin),
            },
        )
        raise exc
    except Exception:
        db.rollback()
        logger.exception(
            "nin_verification_unhandled_error",
            extra={
                "subscriber_id": str(subscriber_id),
                "nin": mask_nin(normalized_nin),
            },
        )
        raise
    finally:
        db.close()
