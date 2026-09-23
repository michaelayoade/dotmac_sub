"""Retry selection regressions through the typed maintenance command.

The normal db_session lane is fast unit coverage. The integration case uses
only the repository's migration-prepared PostgreSQL target.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.team_inbox import InboxConversation, InboxMessage
from app.services import (
    team_inbox_maintenance,
    team_inbox_operations,
    team_inbox_outbound,
)
from app.services.owner_commands import CommandContext

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def _conversation(db: Session) -> UUID:
    conversation = InboxConversation(
        id=uuid4(),
        channel_type="email",
        subject="Retry selection regression",
        status="open",
        contact_address="retry@example.com",
    )
    db.add(conversation)
    db.flush()
    return conversation.id


def _message(
    db: Session,
    *,
    conversation_id: UUID,
    position: int,
    metadata: dict[str, object] | None = None,
    direction: str = "outbound",
    delivery_status: str = "failed",
) -> UUID:
    message = InboxMessage(
        id=uuid4(),
        conversation_id=conversation_id,
        channel_type="email",
        direction=direction,
        body="Retry selection test",
        from_address="support@example.com",
        to_addresses=["retry@example.com"],
        created_at=_EPOCH + timedelta(seconds=position),
        metadata_={"delivery_status": delivery_status, **(metadata or {})},
    )
    db.add(message)
    db.flush()
    return message.id


def _command(
    *, limit: int = 50, max_retry_count: int = 5
) -> team_inbox_maintenance.RetryFailedOutboundCommand:
    return team_inbox_maintenance.RetryFailedOutboundCommand(
        context=CommandContext.system(
            actor="test:inbox-retry-selection",
            scope="team-inbox:maintenance",
            reason="verify bounded failed-message retry selection",
        ),
        limit=limit,
        max_retry_count=max_retry_count,
    )


def _mock_delivery(
    monkeypatch: pytest.MonkeyPatch, *, accepted: bool = True
) -> list[UUID]:
    attempts: list[UUID] = []

    def send(
        db: Session,
        *,
        conversation: InboxConversation,
        payload: team_inbox_outbound.InboxReplyPayload,
        now: datetime | None = None,
        record_failure: bool = False,
    ) -> team_inbox_outbound.InboxReplyResult:
        assert record_failure is False
        assert payload.metadata is not None
        attempts.append(UUID(str(payload.metadata["retry_of_message_id"])))
        return team_inbox_outbound.InboxReplyResult(
            kind="queued" if accepted else "invalid_message",
            conversation_id=str(conversation.id),
            message_id=str(uuid4()) if accepted else None,
            reason=None if accepted else "Delivery policy rejected the retry",
        )

    monkeypatch.setattr(team_inbox_outbound, "send_inbox_reply", send)
    return attempts


def _assert_exhausted_batch_does_not_starve(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation_id = _conversation(db)
    eligible_id = _message(db, conversation_id=conversation_id, position=0)
    exhausted_ids = {
        _message(
            db,
            conversation_id=conversation_id,
            position=position,
            metadata={"retry_count": 5},
        )
        for position in range(1, 51)
    }
    # Commit fixture construction before entering the real public owner.
    db.commit()
    attempts = _mock_delivery(monkeypatch)

    result = team_inbox_maintenance.retry_failed_outbound(db, _command())

    assert result == team_inbox_maintenance.MaintenanceOutcome(changed=1, skipped=0)
    assert attempts == [eligible_id]
    # A replay cannot resubmit the already-retried message.
    assert team_inbox_maintenance.retry_failed_outbound(db, _command()).changed == 0
    assert attempts == [eligible_id]
    # Exhausted messages remain failed and visible to operators.
    visible = team_inbox_operations.list_failed_outbound_messages(db, limit=100)
    assert {message.id for message in visible} == exhausted_ids
    assert all(message.metadata_["retry_count"] == 5 for message in visible)


def test_exhausted_newest_batch_does_not_starve_older_work(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_exhausted_batch_does_not_starve(db_session, monkeypatch)


@pytest.mark.integration
def test_postgresql_exhausted_batch_reaches_eligible_work(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert db_session.get_bind().dialect.name == "postgresql"
    _assert_exhausted_batch_does_not_starve(db_session, monkeypatch)


@pytest.mark.parametrize(
    ("metadata", "eligible"),
    [
        ({}, True),
        ({"retry_count": None}, True),
        ({"retry_count": 0}, True),
        ({"retry_count": "4"}, True),
        ({"retry_count": 5}, False),
        ({"retry_count": "6"}, False),
        ({"retry_count": "broken"}, False),
        ({"retry_count": "9" * 40}, False),
        ({"retry_count": -1}, False),
        ({"retry_count": True}, False),
        ({"retry_count": False}, False),
        ({"retry_count": 1.5}, False),
        ({"retry_count": ""}, False),
    ],
)
def test_retry_counter_is_fail_closed_without_crashing_the_sweep(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    metadata: dict[str, object],
    eligible: bool,
) -> None:
    conversation_id = _conversation(db_session)
    message_id = _message(
        db_session,
        conversation_id=conversation_id,
        position=0,
        metadata=metadata,
    )
    db_session.commit()
    attempts = _mock_delivery(monkeypatch)

    result = team_inbox_maintenance.retry_failed_outbound(db_session, _command())

    assert result.changed == int(eligible)
    assert attempts == ([message_id] if eligible else [])
    message = db_session.get(InboxMessage, message_id)
    assert message is not None
    assert message.metadata_["delivery_status"] == ("retried" if eligible else "failed")
    if not eligible:
        assert message.metadata_ == {"delivery_status": "failed", **metadata}


def test_limit_and_newest_eligible_order_remain_bounded(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation_id = _conversation(db_session)
    older = _message(db_session, conversation_id=conversation_id, position=1)
    newest = _message(db_session, conversation_id=conversation_id, position=2)
    _message(
        db_session,
        conversation_id=conversation_id,
        position=3,
        direction="inbound",
    )
    _message(
        db_session,
        conversation_id=conversation_id,
        position=4,
        delivery_status="sent",
    )
    db_session.commit()
    attempts = _mock_delivery(monkeypatch)

    assert team_inbox_maintenance.retry_failed_outbound(
        db_session, _command(limit=1)
    ).changed == 1
    assert attempts == [newest]
    assert team_inbox_maintenance.retry_failed_outbound(
        db_session, _command(limit=1)
    ).changed == 1
    assert attempts == [newest, older]


def test_rejected_delivery_is_not_counted_as_success_and_budget_is_preserved(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation_id = _conversation(db_session)
    message_id = _message(
        db_session,
        conversation_id=conversation_id,
        position=0,
        metadata={"retry_count": 1},
    )
    db_session.commit()
    attempts = _mock_delivery(monkeypatch, accepted=False)

    result = team_inbox_maintenance.retry_failed_outbound(
        db_session, _command(max_retry_count=2)
    )
    assert result == team_inbox_maintenance.MaintenanceOutcome(changed=0, skipped=1)
    assert attempts == [message_id]
    # The rejected attempt used its final retry; the next sweep must not send.
    assert team_inbox_maintenance.retry_failed_outbound(
        db_session, _command(max_retry_count=2)
    ) == team_inbox_maintenance.MaintenanceOutcome(changed=0, skipped=0)
    assert attempts == [message_id]
    message = db_session.get(InboxMessage, message_id)
    assert message is not None
    assert message.metadata_["delivery_status"] == "failed"
    assert message.metadata_["retry_count"] == 2
    assert message.metadata_["last_retry_result"] == "invalid_message"
    assert db_session.scalar(select(func.count(InboxMessage.id))) == 1
