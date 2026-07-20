from __future__ import annotations

from uuid import uuid4

from app.models.notification import (
    CommunicationIntentRecord,
    Notification,
    NotificationChannel,
)
from app.models.project import Project, ProjectTask
from app.models.support import Ticket
from app.models.work_order import WorkOrder
from app.services import customer_experience_communications


def test_field_event_intent_carries_full_native_lineage_and_dedupes(
    db_session, subscriber
):
    subscriber.phone = "+2348012345678"
    project = Project(name="Install", subscriber_id=subscriber.id, status="active")
    ticket = Ticket(title="No optical signal", subscriber_id=subscriber.id)
    db_session.add_all([project, ticket])
    db_session.flush()
    task = ProjectTask(
        project_id=project.id,
        ticket_id=ticket.id,
        title="Restore signal",
        status="in_progress",
    )
    db_session.add(task)
    db_session.flush()
    visit = WorkOrder(
        public_id="sub-customer-comms",
        subscriber_id=subscriber.id,
        project_id=project.id,
        project_task_id=task.id,
        origin_ticket_id=ticket.id,
        title="Re-splice customer drop",
        status="completed",
    )
    db_session.add(visit)
    db_session.flush()
    field_event_id = uuid4()

    customer_experience_communications.request_field_event(
        db_session,
        work_order=visit,
        event="complete",
        field_event_id=field_event_id,
    )
    customer_experience_communications.request_field_event(
        db_session,
        work_order=visit,
        event="complete",
        field_event_id=field_event_id,
    )
    db_session.commit()

    intents = db_session.query(CommunicationIntentRecord).all()
    assert len(intents) == 1
    intent = intents[0]
    assert intent.dedupe_key == f"field-event:{field_event_id}"
    assert intent.metadata_["work_order_id"] == visit.public_id
    assert intent.metadata_["work_order_pk"] == str(visit.id)
    assert intent.metadata_["project_id"] == str(project.id)
    assert intent.metadata_["project_task_id"] == str(task.id)
    assert intent.metadata_["ticket_id"] == str(ticket.id)
    assert intent.metadata_["field_event_id"] == str(field_event_id)
    assert intent.metadata_["customer_experience"] is True
    rows = (
        db_session.query(Notification)
        .filter(Notification.communication_intent_id == intent.id)
        .all()
    )
    assert {row.channel for row in rows} == {
        NotificationChannel.email,
        NotificationChannel.whatsapp,
        NotificationChannel.push,
    }
    assert all(row.subscriber_id == subscriber.id for row in rows)


def test_en_route_defaults_to_direct_whatsapp_and_push(db_session, subscriber):
    subscriber.phone = "+2348012345678"
    visit = WorkOrder(
        public_id="sub-customer-en-route",
        subscriber_id=subscriber.id,
        title="Customer visit",
        status="in_progress",
    )
    db_session.add(visit)
    db_session.flush()

    customer_experience_communications.request_field_event(
        db_session,
        work_order=visit,
        event="en_route",
        field_event_id=uuid4(),
    )

    rows = (
        db_session.query(Notification)
        .filter(Notification.event_type == "work_order_en_route")
        .all()
    )
    assert {row.channel for row in rows} == {
        NotificationChannel.whatsapp,
        NotificationChannel.push,
    }
    assert any(row.recipient == subscriber.phone for row in rows)
