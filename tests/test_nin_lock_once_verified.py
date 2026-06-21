"""Lock-once-verified guard for NIN verification (#48c).

Once a subscriber has a `success` NIN verification, treat it as final: don't
spawn another (paid) Mono lookup or let a different NIN overwrite a confirmed
identity. An explicit admin path (allow_reverify=True) bypasses the lock.
"""

from __future__ import annotations

from app.models.subscriber import NINVerificationStatus, SubscriberNINVerification
from app.services.nin_verifications import get_or_create_pending_nin_verification


def _add(db, subscriber_id, nin, status):
    v = SubscriberNINVerification(subscriber_id=subscriber_id, nin=nin, status=status)
    db.add(v)
    db.flush()
    return v


def _pending_count(db, subscriber_id):
    return (
        db.query(SubscriberNINVerification)
        .filter_by(subscriber_id=subscriber_id, status=NINVerificationStatus.pending)
        .count()
    )


def test_locks_once_verified(db_session, subscriber):
    success = _add(
        db_session, subscriber.id, "11111111111", NINVerificationStatus.success
    )
    db_session.commit()

    # A re-verify attempt (even with a different NIN) returns the existing
    # success and creates no new pending → no new Mono call.
    result = get_or_create_pending_nin_verification(
        db_session, subscriber.id, "22222222222"
    )

    assert result.id == success.id
    assert result.status == NINVerificationStatus.success
    assert _pending_count(db_session, subscriber.id) == 0


def test_allow_reverify_bypasses_lock(db_session, subscriber):
    _add(db_session, subscriber.id, "11111111111", NINVerificationStatus.success)
    db_session.commit()

    result = get_or_create_pending_nin_verification(
        db_session, subscriber.id, "22222222222", allow_reverify=True
    )

    assert result.status == NINVerificationStatus.pending
    assert result.nin == "22222222222"


def test_failed_history_does_not_lock(db_session, subscriber):
    _add(db_session, subscriber.id, "11111111111", NINVerificationStatus.failed)
    db_session.commit()

    # A prior failure is not a lock — re-verification is allowed.
    result = get_or_create_pending_nin_verification(
        db_session, subscriber.id, "11111111111"
    )

    assert result.status == NINVerificationStatus.pending
