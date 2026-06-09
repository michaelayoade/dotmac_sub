from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.notification import Notification
from app.models.provisioning import ServiceOrder
from app.models.subscriber import Subscriber, SubscriberContact
from app.models.support import (
    AutomationActionType,
    AutomationTrigger,
    TicketAssignee,
    TicketChannel,
    TicketComment,
    TicketCommentAuthorType,
)
from app.models.system_user import SystemUser
from app.schemas.support import (
    TicketCommentCreate,
    TicketCreate,
    TicketMergeRequest,
    TicketUpdate,
)
from app.services import support as support_service
from app.services import support_automation
from app.services.customer_identity_resolution import (
    rebuild_identity_index_for_subscriber,
)


def _ticket_payload(subscriber_id):
    return TicketCreate(
        title="Internet unstable",
        description="Packet loss observed",
        subscriber_id=subscriber_id,
        customer_account_id=subscriber_id,
        channel="web",
        priority="normal",
    )


def _system_user(**overrides) -> SystemUser:
    return SystemUser(
        first_name=overrides.pop("first_name", "Support"),
        last_name=overrides.pop("last_name", "Tech"),
        display_name=overrides.pop("display_name", "Support Tech"),
        email=overrides.pop("email", f"{uuid4().hex}@example.com"),
        phone=overrides.pop("phone", "+15550000000"),
        **overrides,
    )


def test_ticket_create_defaults_to_open_and_generates_number(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session,
        _ticket_payload(subscriber.id),
        actor_id=str(subscriber.id),
    )

    assert ticket.status == "open"
    assert ticket.number is not None
    assert ticket.number != ""


def test_ticket_resolved_and_closed_set_timestamps(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session, _ticket_payload(subscriber.id), actor_id=str(subscriber.id)
    )

    resolved = support_service.tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(status="resolved"),
        actor_id=str(subscriber.id),
    )
    assert resolved.resolved_at is not None

    closed = support_service.tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(status="closed"),
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


def test_merge_moves_comments_assignees_and_blocks_source_mutations(
    db_session, subscriber
):
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
        TicketCommentCreate(
            body="Please fix", is_internal=False, author_person_id=subscriber.id
        ),
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
    assert source.status == "merged"
    assert source.merged_into_ticket_id == target.id

    target_comments = (
        db_session.query(TicketComment)
        .filter(TicketComment.ticket_id == target.id)
        .all()
    )
    assert any("Please fix" in item.body for item in target_comments)

    assignee_rows = (
        db_session.query(TicketAssignee)
        .filter(TicketAssignee.ticket_id == target.id)
        .all()
    )
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
    technician = _system_user(display_name="Technician")
    manager = _system_user(display_name="Manager")
    coordinator = _system_user(display_name="Coordinator")
    db_session.add_all([technician, manager, coordinator])
    db_session.commit()

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Notify test",
            description="",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            technician_person_id=technician.id,
            ticket_manager_person_id=manager.id,
            site_coordinator_person_id=coordinator.id,
            service_team_id=uuid4(),
        ),
        actor_id=str(subscriber.id),
    )

    support_service.tickets.update(
        db_session,
        str(ticket.id),
        TicketUpdate(priority="high"),
        actor_id=str(subscriber.id),
    )

    assert db_session.query(Notification).count() == 0


def test_ticket_assignments_accept_system_user_ids(db_session, subscriber):
    technician = _system_user(display_name="Field Tech")
    manager = _system_user(display_name="Project Manager")
    coordinator = _system_user(display_name="Site Coordinator")
    assignee = _system_user(display_name="Queue Assignee")
    db_session.add_all([technician, manager, coordinator, assignee])
    db_session.commit()

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Staff assignment",
            description="Assign to internal users",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            technician_person_id=technician.id,
            ticket_manager_person_id=manager.id,
            site_coordinator_person_id=coordinator.id,
            assignee_person_ids=[assignee.id],
        ),
        actor_id=str(subscriber.id),
    )

    db_session.refresh(ticket)
    assignee_rows = (
        db_session.query(TicketAssignee)
        .filter(TicketAssignee.ticket_id == ticket.id)
        .all()
    )

    assert ticket.technician_person_id == technician.id
    assert ticket.ticket_manager_person_id == manager.id
    assert ticket.site_coordinator_person_id == coordinator.id
    assert any(row.person_id == assignee.id for row in assignee_rows)


def test_ticket_create_ignores_system_user_created_by_subscriber_fk(
    db_session, subscriber
):
    system_user = _system_user(display_name="Support Admin")
    db_session.add(system_user)
    db_session.commit()

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Created by admin",
            description="",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
            created_by_person_id=system_user.id,
        ),
        actor_id=str(system_user.id),
    )

    assert ticket.created_by_person_id is None


def test_ticket_comment_stores_system_user_author_identity(db_session, subscriber):
    system_user = _system_user(display_name="Comment Admin")
    db_session.add(system_user)
    db_session.commit()

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Comment target",
            description="",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
        ),
        actor_id=str(subscriber.id),
    )

    comment = support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        TicketCommentCreate(
            body="Internal admin note",
            is_internal=True,
            author_type=TicketCommentAuthorType.staff,
            author_system_user_id=system_user.id,
        ),
        actor_id=str(system_user.id),
    )

    assert comment.author_type == TicketCommentAuthorType.staff.value
    assert comment.author_person_id is None
    assert comment.author_system_user_id == system_user.id


def test_ticket_comment_stores_customer_author_identity(db_session, subscriber):
    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Comment target",
            description="",
            subscriber_id=subscriber.id,
            customer_account_id=subscriber.id,
        ),
        actor_id=str(subscriber.id),
    )

    comment = support_service.tickets.create_comment(
        db_session,
        str(ticket.id),
        TicketCommentCreate(
            body="Customer reply",
            is_internal=False,
            author_type=TicketCommentAuthorType.customer,
            author_person_id=subscriber.id,
        ),
        actor_id=str(subscriber.id),
    )

    assert comment.author_type == TicketCommentAuthorType.customer.value
    assert comment.author_person_id == subscriber.id
    assert comment.author_system_user_id is None


def test_ticket_create_auto_links_inbound_sender_from_subscriber_contact(
    db_session, subscriber
):
    contact = SubscriberContact(
        subscriber_id=subscriber.id,
        email="linked-contact@example.com",
        contact_type="general",
    )
    db_session.add(contact)
    db_session.flush()
    rebuild_identity_index_for_subscriber(db_session, subscriber.id)

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Inbound email",
            description="Created from inbound email",
            channel=TicketChannel.email,
            inbound_sender=" LINKED-CONTACT@example.com ",
        ),
        actor_id=None,
    )

    assert ticket.subscriber_id == subscriber.id
    assert ticket.customer_account_id == subscriber.id
    assert ticket.metadata_ is not None
    assert ticket.metadata_["identity_resolution"]["status"] == "matched"
    assert (
        ticket.metadata_["identity_resolution"]["matched_via"] == "subscriber_contact"
    )
    assert ticket.metadata_["identity_resolution"]["matched_contact_id"] == str(
        contact.id
    )
    assert ticket.metadata_["identity_resolution"]["match_confidence"] == "MEDIUM"
    assert ticket.metadata_["account_sensitive_automation_allowed"] is True


def test_ticket_create_marks_ambiguous_inbound_sender_for_manual_review(
    db_session, subscriber
):
    other = Subscriber(
        first_name="Other",
        last_name="Subscriber",
        email="other@example.com",
    )
    db_session.add(other)
    db_session.flush()
    db_session.add_all(
        [
            SubscriberContact(
                subscriber_id=subscriber.id,
                phone="08012345678",
                contact_type="general",
            ),
            SubscriberContact(
                subscriber_id=other.id,
                phone="+2348012345678",
                contact_type="general",
            ),
        ]
    )
    db_session.flush()
    rebuild_identity_index_for_subscriber(db_session, subscriber.id)
    rebuild_identity_index_for_subscriber(db_session, other.id)

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Inbound SMS",
            description="Needs manual review",
            channel=TicketChannel.phone,
            inbound_sender="0801 234 5678",
        ),
        actor_id=None,
    )

    assert ticket.subscriber_id is None
    assert ticket.customer_account_id is None
    assert ticket.metadata_ is not None
    assert ticket.metadata_["identity_resolution"]["status"] == "ambiguous"
    assert ticket.metadata_["identity_resolution"]["manual_review_required"] is True
    assert ticket.metadata_["manual_review_required"] is True
    assert ticket.metadata_["automation_paused"] is True
    assert ticket.metadata_["account_sensitive_automation_allowed"] is False


def test_ticket_create_marks_historical_match_low_confidence_for_manual_review(
    db_session, subscriber
):
    from app.models.comms import CustomerNotificationEvent

    db_session.add(
        CustomerNotificationEvent(
            entity_type="support_ticket",
            entity_id=subscriber.id,
            subscriber_id=subscriber.id,
            channel="sms",
            recipient="+2348091111111",
            message="Previous notification",
        )
    )
    db_session.commit()

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Inbound SMS",
            description="Historical only",
            channel=TicketChannel.phone,
            inbound_sender="08091111111",
        ),
        actor_id=None,
    )

    assert ticket.subscriber_id == subscriber.id
    assert ticket.customer_account_id == subscriber.id
    assert (
        ticket.metadata_["identity_resolution"]["matched_via"]
        == "historical_participant"
    )
    assert ticket.metadata_["identity_resolution"]["match_confidence"] == "LOW"
    assert ticket.metadata_["manual_review_required"] is True
    assert ticket.metadata_["automation_paused"] is True
    assert ticket.metadata_["account_sensitive_automation_allowed"] is False


def test_ticket_automation_is_suppressed_for_ambiguous_identity(db_session, subscriber):
    other = Subscriber(
        first_name="Other",
        last_name="Subscriber",
        email="other-automation@example.com",
    )
    db_session.add(other)
    db_session.flush()
    db_session.add_all(
        [
            SubscriberContact(
                subscriber_id=subscriber.id,
                phone="08077777777",
                contact_type="general",
            ),
            SubscriberContact(
                subscriber_id=other.id,
                phone="+2348077777777",
                contact_type="general",
            ),
        ]
    )
    db_session.flush()
    rebuild_identity_index_for_subscriber(db_session, subscriber.id)
    rebuild_identity_index_for_subscriber(db_session, other.id)
    support_automation.create_rule(
        db_session,
        name="Auto high priority",
        trigger=AutomationTrigger.ticket_created,
        action_type=AutomationActionType.set_priority,
        action_value={"priority": "high"},
    )
    db_session.commit()

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Inbound SMS",
            description="Ambiguous identity",
            channel=TicketChannel.phone,
            priority="normal",
            inbound_sender="08077777777",
        ),
        actor_id=None,
    )

    assert ticket.priority == "normal"
    assert ticket.metadata_["automation_paused"] is True
    assert ticket.metadata_["automation_suppressed_reason"] == (
        "identity_manual_review_required"
    )


def test_ticket_automation_is_suppressed_for_low_confidence_identity(
    db_session, subscriber
):
    from app.models.comms import CustomerNotificationEvent

    db_session.add(
        CustomerNotificationEvent(
            entity_type="support_ticket",
            entity_id=subscriber.id,
            subscriber_id=subscriber.id,
            channel="sms",
            recipient="+2348066666666",
            message="Previous notification",
        )
    )
    db_session.flush()
    support_automation.create_rule(
        db_session,
        name="Auto open status",
        trigger=AutomationTrigger.ticket_created,
        action_type=AutomationActionType.set_status,
        action_value={"status": "pending_customer"},
    )
    db_session.commit()

    ticket = support_service.tickets.create(
        db_session,
        TicketCreate(
            title="Inbound SMS",
            description="Historical identity",
            channel=TicketChannel.phone,
            status="open",
            inbound_sender="08066666666",
        ),
        actor_id=None,
    )

    assert ticket.status == "open"
    assert ticket.metadata_["identity_resolution"]["match_confidence"] == "LOW"
    assert ticket.metadata_["automation_paused"] is True
