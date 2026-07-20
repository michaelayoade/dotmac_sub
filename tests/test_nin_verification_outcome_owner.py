"""NIN verification outcome ownership.

`app/services/nin_verifications.py` is the single writer for the verification
outcome transition and the subscriber projection
(`metadata_["nin_verified"]` / `nin_last_checked_at`); the celery task is a
thin adapter and must not perform either write itself.
"""

from __future__ import annotations

import inspect

from app.models.subscriber import NINVerificationStatus, SubscriberNINVerification
from app.services.nin_verifications import (
    record_nin_verification_failure_committed,
    record_nin_verification_outcome_committed,
)


def _pending(db, subscriber, nin="12345678901"):
    v = SubscriberNINVerification(
        subscriber_id=subscriber.id, nin=nin, status=NINVerificationStatus.pending
    )
    db.add(v)
    db.flush()
    return v


def test_match_outcome_transitions_row_and_projection_together(db_session, subscriber):
    verification = _pending(db_session, subscriber)

    result = record_nin_verification_outcome_committed(
        db_session,
        verification,
        subscriber,
        match_result={"is_match": True, "match_score": 93},
        mono_response={"data": {"ok": True}},
    )

    assert result == {"status": "success", "is_match": True, "match_score": 93}
    assert verification.status == NINVerificationStatus.success
    assert verification.failure_reason is None
    assert verification.verified_at is not None
    assert subscriber.metadata_["nin_verified"] is True
    assert subscriber.metadata_["nin_last_checked_at"]


def test_mismatch_outcome_records_identity_mismatch(db_session, subscriber):
    verification = _pending(db_session, subscriber)

    result = record_nin_verification_outcome_committed(
        db_session,
        verification,
        subscriber,
        match_result={"is_match": False, "match_score": 10},
        mono_response={"data": {}},
    )

    assert result["status"] == "failed"
    assert verification.status == NINVerificationStatus.failed
    assert verification.failure_reason == "Subscriber identity mismatch"
    assert subscriber.metadata_["nin_verified"] is False


def test_failure_writer_records_reason_and_projection(db_session, subscriber):
    verification = _pending(db_session, subscriber)

    record_nin_verification_failure_committed(
        db_session,
        verification,
        subscriber,
        reason="Mono lookup failed",
        mono_response={"error": "boom"},
    )

    assert verification.status == NINVerificationStatus.failed
    assert verification.is_match is False
    assert verification.match_score == 0
    assert verification.failure_reason == "Mono lookup failed"
    assert verification.verified_at is not None
    assert subscriber.metadata_["nin_verified"] is False


def test_failure_writer_tolerates_missing_subscriber(db_session, subscriber):
    verification = _pending(db_session, subscriber)

    record_nin_verification_failure_committed(
        db_session, verification, None, reason="Subscriber not found"
    )

    assert verification.status == NINVerificationStatus.failed
    assert (subscriber.metadata_ or {}).get("nin_verified") is None


def test_task_module_is_a_thin_adapter():
    """The task must not transition outcome status or write the projection.

    The one permitted verification-row write in the task is the transient
    ``failure_reason`` annotation on the retry path.
    """
    from app.tasks import nin_tasks

    source = inspect.getsource(nin_tasks)
    assert "verification.status =" not in source
    assert "metadata_" not in source
    assert "verified_at" not in source
    assert not hasattr(nin_tasks, "_mark_failed")
    assert not hasattr(nin_tasks, "_update_subscriber_metadata")
