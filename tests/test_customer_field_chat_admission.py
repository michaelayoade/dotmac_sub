"""Customer preflight is advisory; the canonical owner admits every write."""

from dataclasses import replace
from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.event_store import EventStore
from app.models.team_inbox import InboxMessage, InboxMessageDirection
from app.services import customer_field_job_chat as customer
from app.services import team_inbox_commands as commands
from app.services.owner_commands import CommandContext
from tests.test_field_job_chat import _depart, _profile, _subscriber, _user, _work_order


def _visit(db):
    subscriber = _subscriber(db)
    order = _work_order(db, subscriber)
    conversation, _ = _depart(db, order, _profile(db, _user(db)))
    subscriber_id, public_id, conversation_id = (
        subscriber.id,
        order.public_id,
        conversation.id,
    )
    db.commit()
    command_id = uuid4()
    command = commands.RecordFieldJobCustomerMessageCommand(
        subscriber_id=subscriber_id,
        work_order_public_id=public_id,
        body="Please ring the bell",
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor=f"subscriber:{subscriber_id}",
            scope="customer:field-job-chat:write",
            reason="Customer message admission test",
            idempotency_key=str(command_id),
        ),
    )
    return command, order, conversation, conversation_id


def _inbound_count(db, conversation_id):
    return (
        db.query(InboxMessage)
        .filter_by(
            conversation_id=conversation_id,
            direction=InboxMessageDirection.inbound.value,
        )
        .count()
    )


@pytest.mark.parametrize("race", ["closed", "reassigned"])
def test_change_after_customer_preflight_is_rechecked_by_owner(
    db_session, monkeypatch, race
):
    command, order, conversation, conversation_id = _visit(db_session)
    original = commands.record_field_job_customer_message
    other = _subscriber(db_session)
    other_id = other.id
    db_session.commit()

    def changed_before_admission(db, *, command):
        if race == "closed":
            conversation.status = "resolved"
        else:
            order.subscriber_id = other_id
        db.commit()
        return original(db, command=command)

    monkeypatch.setattr(
        commands, "record_field_job_customer_message", changed_before_admission
    )
    with pytest.raises(customer.FieldJobChatError) as caught:
        customer.send_message(
            db_session,
            command=command,
        )
    assert caught.value.reason == (
        customer.CustomerChatErrorReason.closed
        if race == "closed"
        else customer.CustomerChatErrorReason.not_found
    )
    assert _inbound_count(db_session, conversation_id) == 0


def test_exact_command_replay_is_one_message_and_changed_body_is_rejected(db_session):
    command, _, _, conversation_id = _visit(db_session)
    first = commands.record_field_job_customer_message(db_session, command=command)
    replay = commands.record_field_job_customer_message(db_session, command=command)
    assert first.message_id == replay.message_id
    assert not first.replayed and replay.replayed
    assert _inbound_count(db_session, conversation_id) == 1
    event = (
        db_session.query(EventStore)
        .filter_by(event_type="field_job.customer_message_created")
        .one()
    )
    audit = (
        db_session.query(AuditEvent)
        .filter_by(action="field_job.customer_message_created")
        .one()
    )
    assert event.payload["message_id"] == str(first.message_id)
    assert audit.entity_id == str(first.message_id)
    assert "body" not in event.payload and "body" not in audit.metadata_
    db_session.commit()
    with pytest.raises(commands.FieldJobCustomerMessageError) as caught:
        commands.record_field_job_customer_message(
            db_session, command=replace(command, body="Changed")
        )
    assert (
        caught.value.reason
        == commands.FieldJobCustomerMessageReason.idempotency_conflict
    )


def test_replay_after_closure_cannot_bypass_current_state(db_session):
    command, _, conversation, _ = _visit(db_session)
    commands.record_field_job_customer_message(db_session, command=command)
    conversation.status = "resolved"
    db_session.commit()
    with pytest.raises(commands.FieldJobCustomerMessageError) as caught:
        commands.record_field_job_customer_message(db_session, command=command)
    assert caught.value.reason == commands.FieldJobCustomerMessageReason.closed


def test_reparented_visit_does_not_expose_prior_customer_history(db_session):
    command, order, _, _ = _visit(db_session)
    commands.record_field_job_customer_message(db_session, command=command)
    other = _subscriber(db_session)
    other_id = other.id
    order.subscriber_id = other_id
    db_session.commit()
    result = customer.get_thread(
        db_session,
        query=customer.CustomerChatThreadQuery(
            subscriber_id=other_id, work_order_public_id=command.work_order_public_id
        ),
    )
    assert result.available is False
    assert result.messages == ()
    availability = customer.chat_availability(
        db_session,
        query=customer.CustomerChatAvailabilityQuery(
            subscriber_id=other_id, work_order_public_id=command.work_order_public_id
        ),
    )
    assert availability.reason == customer.CustomerChatAvailabilityReason.not_found


def test_owner_rejects_spoofed_context_actor(db_session):
    command, _, _, conversation_id = _visit(db_session)
    context = replace(command.context, actor=f"subscriber:{uuid4()}")
    with pytest.raises(commands.FieldJobCustomerMessageError) as caught:
        commands.record_field_job_customer_message(
            db_session, command=replace(command, context=context)
        )
    assert caught.value.reason == commands.FieldJobCustomerMessageReason.invalid_request
    assert _inbound_count(db_session, conversation_id) == 0


def test_event_staging_failure_rolls_back_message_and_audit(db_session, monkeypatch):
    command, _, _, conversation_id = _visit(db_session)

    def fail_event(*args, **kwargs):
        raise RuntimeError("Injected staging failure")

    monkeypatch.setattr(commands, "emit_event", fail_event)
    with pytest.raises(RuntimeError, match="Injected staging failure"):
        commands.record_field_job_customer_message(db_session, command=command)
    assert _inbound_count(db_session, conversation_id) == 0
    assert (
        db_session.query(AuditEvent)
        .filter_by(action="field_job.customer_message_created")
        .count()
        == 0
    )
