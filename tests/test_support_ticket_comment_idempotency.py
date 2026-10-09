from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.support import TicketComment, TicketCommentAuthorType
from app.models.system_user import SystemUser
from app.schemas.support import TicketCommentCreate, TicketCreate
from app.services import support as support_service
from app.services.domain_errors import DomainError


def _staff_user() -> SystemUser:
    return SystemUser(
        first_name="Idempotency",
        last_name="Tester",
        display_name="Idempotency Tester",
        email=f"comment-idempotency-{uuid4().hex}@example.com",
        phone="+15550000000",
    )


def _ticket(db_session, subscriber):
    return support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Idempotent comment target",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
        ),
        actor_id=str(subscriber.id),
    )


@pytest.mark.parametrize("is_internal", [True, False])
def test_staff_comment_creation_supports_both_visibility_modes(
    db_session, subscriber, is_internal
) -> None:
    staff = _staff_user()
    db_session.add(staff)
    db_session.commit()
    ticket = _ticket(db_session, subscriber)
    key = uuid4()

    comment = support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        TicketCommentCreate(
            body="Internal note" if is_internal else "Customer-visible reply",
            is_internal=is_internal,
            idempotency_key=key,
            author_type=TicketCommentAuthorType.staff,
            author_system_user_id=staff.id,
        ),
        actor_id=str(staff.id),
    )

    assert comment.is_internal is is_internal
    assert comment.idempotency_key == key
    assert comment.idempotency_fingerprint is not None


def test_repeated_comment_submission_returns_original_comment(
    db_session, subscriber
) -> None:
    ticket = _ticket(db_session, subscriber)
    key = uuid4()

    first = support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        TicketCommentCreate(
            body="Original note",
            is_internal=True,
            idempotency_key=key,
        ),
        actor_id=None,
    )
    replay = support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        TicketCommentCreate(
            body="Original note",
            is_internal=True,
            idempotency_key=key,
        ),
        actor_id=None,
    )

    assert replay.id == first.id
    assert replay.body == "Original note"
    assert (
        db_session.query(TicketComment)
        .filter(
            TicketComment.ticket_id == ticket.id,
            TicketComment.idempotency_key == key,
        )
        .count()
        == 1
    )


def test_reused_comment_key_with_different_inputs_is_rejected(
    db_session, subscriber
) -> None:
    ticket = _ticket(db_session, subscriber)
    key = uuid4()
    support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        TicketCommentCreate(
            body="Original note",
            is_internal=True,
            idempotency_key=key,
        ),
        actor_id=None,
    )

    with pytest.raises(
        DomainError, match="already used with different details"
    ) as error:
        support_service.tickets.create_comment(
            db_session,
            str(ticket.id),
            TicketCommentCreate(
                body="Conflicting reply",
                is_internal=False,
                idempotency_key=key,
            ),
            actor_id=None,
        )

    assert error.value.code == "ticket_comment_idempotency_conflict"


def test_different_comment_idempotency_keys_create_separate_comments(
    db_session, subscriber
) -> None:
    ticket = _ticket(db_session, subscriber)

    comments = [
        support_service.tickets.create_comment(
            db_session,
            str(ticket.id),
            TicketCommentCreate(
                body=f"Legitimate comment {index}",
                is_internal=True,
                idempotency_key=uuid4(),
            ),
            actor_id=None,
        )
        for index in range(2)
    ]

    assert comments[0].id != comments[1].id
    assert (
        db_session.query(TicketComment)
        .filter(TicketComment.ticket_id == ticket.id)
        .count()
        == 2
    )


def test_comment_form_has_server_key_and_submit_lock() -> None:
    template = Path("templates/admin/support/tickets/detail.html").read_text(
        encoding="utf-8"
    )

    assert 'name="idempotency_key" value="{{ comment_idempotency_key }}"' in template
    assert "if (submitting) { $event.preventDefault(); }" in template
    assert ':disabled="submitting"' in template
    assert '@pageshow.window="submitting = false"' in template
    assert "submitting: false" in template


def test_historical_duplicate_report_is_read_only() -> None:
    report = Path(
        "scripts/support/report_historical_duplicate_ticket_comments.sql"
    ).read_text(encoding="utf-8")
    statement = "\n".join(
        line for line in report.splitlines() if not line.lstrip().startswith("--")
    )

    assert statement.lstrip().upper().startswith("WITH ORDERED_COMMENTS AS")
    assert not re.search(
        r"\b(?:UPDATE|DELETE|INSERT|MERGE|TRUNCATE)\b", statement.upper()
    )


def test_migration_adds_nullable_ticket_scoped_unique_key() -> None:
    migration = Path(
        "alembic/versions/661_support_ticket_comment_idempotency.py"
    ).read_text(encoding="utf-8")

    assert (
        'down_revision: str | None = "658_regional_report_billing_indexes"' in migration
    )
    assert '"idempotency_key"' in migration
    assert '"idempotency_fingerprint"' in migration
    assert "nullable=True" in migration
    assert '"uq_support_ticket_comments_ticket_idempotency_key"' in migration
    assert '["ticket_id", "idempotency_key"]' in migration
    assert '"ck_support_ticket_comments_idempotency_evidence"' in migration
