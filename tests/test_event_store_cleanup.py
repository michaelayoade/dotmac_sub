from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.event_store import EventStatus, EventStore
from app.models.prepaid_funding import PrepaidFundingTriggerExecution
from app.services import event_store as event_store_service


def _completed_event(*, created_at: datetime) -> EventStore:
    return EventStore(
        event_id=uuid4(),
        event_type="payment.received",
        payload={},
        status=EventStatus.completed,
        created_at=created_at,
    )


def test_cleanup_preserves_events_referenced_by_prepaid_receipts(
    db_session, subscriber
) -> None:
    old = datetime.now(UTC) - timedelta(days=60)
    protected = _completed_event(created_at=old)
    deletable = _completed_event(created_at=old)
    db_session.add_all([protected, deletable])
    db_session.flush()
    db_session.add(
        PrepaidFundingTriggerExecution(
            event_store_id=protected.id,
            event_id=protected.event_id,
            event_type=protected.event_type,
            account_id=subscriber.id,
            currency="NGN",
            effective_at=old,
            request_fingerprint="a" * 64,
            outcome_fingerprint="b" * 64,
            disposition="applied",
        )
    )
    db_session.commit()
    protected_id = protected.id
    deletable_id = deletable.id

    result = event_store_service.cleanup_completed_events(
        db_session,
        retention_days=30,
    )
    db_session.commit()

    assert db_session.get(EventStore, protected_id) is not None
    assert db_session.get(EventStore, deletable_id) is None
    assert result == {
        "deleted": 1,
        "handler_attempts_deleted": 0,
        "skipped_referenced": 1,
    }
