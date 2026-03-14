from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.notification import Notification
from app.models.provisioning import ServiceOrder
from app.models.support import TicketAssignee, TicketComment, TicketPriority, TicketStatus
from app.schemas.support import (
    TicketCommentCreate,
    TicketCreate,
    TicketMergeRequest,
    TicketUpdate,
)
from app.services import support as support_service


def _ticket_payload(subscriber_id):
    return TicketCreate(
        title="Internet unstable",
        description="Packet loss observed",
        subscriber_id=subscriber_id,
        customer_account_id=subscriber_id,
        channel="web",
        priority=TicketPriority.normal,
    )


def test_ticket_create_defaults_to_open_and_generates_number(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session,
        _ticket_payload(subscriber.id),
        actor_id=str(subscriber.id),
    )

    assert ticket.status == TicketStatus.open
    assert ticket.number is not None
    assert ticket.number != ""


def test_ticket_resolved_and_closed_set_timestamps(db_session, subscriber):
    ticket = support_service.tickets.create(db_session, _ticket_payload(subscriber.id), actor_id=str(subscriber.id))

    resolved = support_service.tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(status=TicketStatus.resolved),
        actor_id=str(subscriber.id),
    )
    assert resolved.resolved_at is not None

    closed = support_service.tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(status=TicketStatus.closed),
        actor_id=str(subscriber.id),
    )
    assert closed.closed_at is not None


def test_field_visit_tag_creates_work_order_once(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Fiber issue",
            description="Needs onsite check",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            tags=["field_visit"],
        ),
        actor_id=str(subscriber.id),
    )

    db_session.refresh(ticket)
    work_order_id = (ticket.metadata_ or {}).get("work_order_id")
    assert work_order_id is not None
    assert db_session.get(ServiceOrder, work_order_id) is not None

    # Updating with field_visit again should not duplicate work order.
    support_service.tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(tags=["field_visit"]),
        actor_id=str(subscriber.id),
    )
    assert db_session.query(ServiceOrder).count() == 1


def test_merge_moves_comments_assignees_and_blocks_source_mutations(db_session, subscriber):
    source = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Source",
            description="source",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            assignee_person_ids=[subscriber.id],
        ),
        actor_id=str(subscriber.id),
    )
    target = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Target",
            description="target",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
        ),
        actor_id=str(subscriber.id),
    )

    support_service.tickets.create_comment(
        db_session,
        str(source.id),
        TicketCommentCreate(body="Please fix", is_internal=False, author_person_id=subscriber.id),
        actor_id=str(subscriber.id),
    )

    merged = support_service.tickets.merge(
        db_session,
        str(source.id),
        TicketMergeRequest(target_ticket_id=target.id, reason="duplicate"),
        actor_id=str(subscriber.id),
    )

    assert merged.id == target.id
    db_session.refresh(source)
    assert source.status == TicketStatus.merged
    assert source.merged_into_ticket_id == target.id

    target_comments = db_session.query(TicketComment).filter(TicketComment.ticket_id == target.id).all()
    assert any("Please fix" in item.body for item in target_comments)

    assignee_rows = db_session.query(TicketAssignee).filter(TicketAssignee.ticket_id == target.id).all()
    assert any(str(row.person_id) == str(subscriber.id) for row in assignee_rows)

    with pytest.raises(HTTPException) as exc:
        support_service.tickets.update(
            db_session,
            str(source.id),
            TicketUpdate(title="forbidden"),
            actor_id=str(subscriber.id),
        )
    assert exc.value.status_code == 409


def test_assignment_notifications_wired_but_disabled(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Notify test",
            description="",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            technician_person_id=subscriber.id,
            ticket_manager_person_id=subscriber.id,
            site_coordinator_person_id=subscriber.id,
            service_team_id=uuid4(),
        ),
        actor_id=str(subscriber.id),
    )

    support_service.tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(priority=TicketPriority.high),
        actor_id=str(subscriber.id),
    )

    assert db_session.query(Notification).count() == 0
