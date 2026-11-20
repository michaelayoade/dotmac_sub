"""Exercise actual PostgreSQL cancellation and Inbox transaction recovery."""

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import subscriber_summary, team_inbox_projection, team_inbox_read
from app.web.admin import inbox
from tests.test_team_inbox_read import (
    assert_inbox_queue_renders_without_assignment_totals,
)


def _cancel_read(db: Session) -> None:
    db.execute(text("SET LOCAL statement_timeout = '25ms'"))
    db.execute(text("SELECT pg_sleep(1)"))


@pytest.mark.parametrize("fragment", [False, True])
def test_required_conversation_read_stops_and_can_be_retried(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, fragment: bool
) -> None:
    assert db_session.get_bind().dialect.name == "postgresql"
    conversation_id, subscriber_id = uuid4(), uuid4()
    lookup_calls = 0

    def read_subscriber(*, db: Session, subscriber_id: str) -> None:
        nonlocal lookup_calls
        lookup_calls += 1
        if lookup_calls == 1:
            _cancel_read(db)

    def read_projection(
        db: Session,
        *,
        conversation_id: UUID,
        actor_person_id: UUID | None,
        message_page: team_inbox_read.InboxTimelineMessagePageRequest,
        actor_permission_keys: frozenset[str],
        include_contact_candidates: bool,
        include_label_usage_counts: bool,
    ) -> team_inbox_projection.InboxConversationProjection | None:
        subscriber_summary.subscriber_summary(db, str(subscriber_id))
        # Before the fix this statement would raise 25P02 after the swallowed
        # cancellation. It should run successfully on the retried request.
        assert db.scalar(text("SELECT 1")) == 1
        return None

    def session_for_request() -> Session:
        return db_session

    app = FastAPI()
    app.include_router(inbox.router)
    app.dependency_overrides[get_db] = session_for_request
    for route in inbox.router.routes:
        for dependency in getattr(route, "dependencies", ()):
            app.dependency_overrides[dependency.dependency] = lambda: None
    monkeypatch.setattr(
        subscriber_summary.subscriber_service.subscribers, "get", read_subscriber
    )
    monkeypatch.setattr(
        team_inbox_projection, "get_conversation_projection", read_projection
    )

    client = TestClient(app)
    headers = {"HX-Request": "true"} if fragment else {}
    url = f"/inbox/{conversation_id}"
    response = client.get(url, headers=headers, follow_redirects=False)
    assert response.status_code == 503
    assert "Refresh to try again." in response.text
    assert not db_session.in_transaction()

    retry = client.get(url, headers=headers, follow_redirects=False)
    assert retry.status_code == 303
    assert lookup_calls == 2


def test_optional_assignment_counts_recover_the_postgresql_session(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert db_session.get_bind().dialect.name == "postgresql"
    assert_inbox_queue_renders_without_assignment_totals(
        db_session,
        monkeypatch,
        _cancel_read,
        reason=team_inbox_projection.InboxAssignmentCountsUnavailableReason.query_canceled,
    )
    assert db_session.scalar(text("SELECT 1")) == 1
