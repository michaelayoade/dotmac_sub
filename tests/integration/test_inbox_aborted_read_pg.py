"""Exercise PostgreSQL cancellation without request or template rendering."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.services import (
    db_session_adapter,
    subscriber_summary,
    team_inbox_projection,
    team_inbox_sidebar_metrics,
)


def _cancel_read(db: Session) -> None:
    db.execute(text("SET LOCAL statement_timeout = '25ms'"))
    db.execute(text("SELECT pg_sleep(1)"))


def test_required_subscriber_read_stops_and_can_be_retried(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert db_session.get_bind().dialect.name == "postgresql"
    subscriber_id = uuid4()
    lookup_calls = 0

    def read_subscriber(*, db: Session, subscriber_id: UUID) -> None:
        nonlocal lookup_calls
        lookup_calls += 1
        if lookup_calls == 1:
            _cancel_read(db)

    monkeypatch.setattr(
        subscriber_summary.subscriber_service.subscribers,
        "get_optional",
        read_subscriber,
    )

    with pytest.raises(DBAPIError) as raised:
        subscriber_summary.subscriber_summary(db_session, str(subscriber_id))
    assert db_session_adapter.database_statement_was_canceled(raised.value)

    db_session_adapter.db_session_adapter.discard_failed_transaction(db_session)
    assert not db_session.in_transaction()
    assert subscriber_summary.subscriber_summary(db_session, str(subscriber_id)) is None
    assert lookup_calls == 2
    assert db_session.scalar(text("SELECT 1")) == 1


def test_optional_assignment_counts_finish_other_reads_before_cancellation(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert db_session.get_bind().dialect.name == "postgresql"

    def canceled_counts(db: Session, **_kwargs: object) -> None:
        _cancel_read(db)

    monkeypatch.setattr(
        team_inbox_sidebar_metrics,
        "get_assignment_cohort_counts",
        canceled_counts,
    )
    projection = team_inbox_projection.build_queue_projection(
        db_session, team_inbox_projection.InboxQueueRequest()
    )
    assert (
        projection.assignment_counts.unavailable_reason
        is team_inbox_projection.InboxAssignmentCountsUnavailableReason.query_canceled
    )
    db_session_adapter.db_session_adapter.discard_failed_transaction(db_session)
    assert db_session.scalar(text("SELECT 1")) == 1
