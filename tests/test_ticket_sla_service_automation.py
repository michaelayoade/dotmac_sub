from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.event_store import EventStore
from app.models.subscription_pause import (
    SubscriptionPauseBillingPolicy,
    SubscriptionPauseResumePolicy,
)
from app.models.support import Ticket, TicketStatus
from app.models.ticket_workflow import (
    SlaBreach,
    SlaClock,
    SlaClockStatus,
    SlaPolicy,
    SlaTarget,
    WorkflowEntityType,
)
from app.services import account_lifecycle, ticket_sla_service_automation
from app.services.events.types import EventType
from app.services.owner_commands import CommandContext


def _pause_command(ticket_id):
    event_id = uuid4()
    return ticket_sla_service_automation.PauseTicketServiceForSlaBreachCommand(
        ticket_id=ticket_id,
        event_id=event_id,
        rule_id=uuid4(),
        rule_version_id=uuid4(),
        step_index=0,
        selection_policy=(
            ticket_sla_service_automation.TicketSlaServiceSelectionPolicy.unique_active_subscription
        ),
        resume_policy=(SubscriptionPauseResumePolicy.manual_after_ticket_resolution),
        billing_policy=(
            SubscriptionPauseBillingPolicy.extend_by_effective_pause_duration
        ),
        context=CommandContext.system(
            actor="automation-runtime",
            scope="subscription:pause",
            reason="test ticket SLA pause consequence",
            correlation_id=event_id,
            causation_id=event_id,
            idempotency_key=f"automation-pause-test:{event_id}",
        ),
    )


def _seed_authoritative_breach(db_session, *, ticket_id, command):
    breached_at = datetime.now(UTC) - timedelta(minutes=1)
    policy = SlaPolicy(
        name=f"Resolution SLA {uuid4()}",
        entity_type=WorkflowEntityType.ticket.value,
        is_active=True,
    )
    db_session.add(policy)
    db_session.flush()
    db_session.add(
        SlaTarget(
            policy_id=policy.id,
            target_minutes=30,
            is_active=True,
        )
    )
    db_session.flush()
    clock = SlaClock(
        policy_id=policy.id,
        entity_type=WorkflowEntityType.ticket.value,
        entity_id=ticket_id,
        status=SlaClockStatus.breached.value,
        started_at=breached_at - timedelta(minutes=30),
        due_at=breached_at,
        breached_at=breached_at,
    )
    db_session.add(clock)
    db_session.flush()
    breach = SlaBreach(clock_id=clock.id, breached_at=breached_at)
    event = EventStore(
        event_id=command.event_id,
        event_type=EventType.support_ticket_sla_breached.value,
        payload={"ticket_id": str(ticket_id), "sla_clock_id": str(clock.id)},
    )
    db_session.add_all((breach, event))
    db_session.commit()
    return clock, breach


def test_pauses_the_only_active_service(
    db_session, subscriber, active_subscription, monkeypatch
):
    ticket = Ticket(
        title="Resolution SLA breached",
        status=TicketStatus.open.value,
        priority="urgent",
        customer_account_id=subscriber.id,
    )
    db_session.add(ticket)
    db_session.flush()
    ticket_id = ticket.id
    subscription_id = active_subscription.id
    db_session.commit()
    command = _pause_command(ticket_id)
    clock, breach = _seed_authoritative_breach(
        db_session, ticket_id=ticket_id, command=command
    )

    episode_id = uuid4()
    cause_id = uuid4()
    captured = []

    def fake_pause(db, command):
        captured.append(command)
        return account_lifecycle.PauseSubscriptionCauseOutcome(
            episode_id=episode_id,
            cause_id=cause_id,
            subscription_id=subscription_id,
            account_status=subscriber.status,
            cause_added=False,
            replayed=False,
        )

    monkeypatch.setattr(account_lifecycle, "pause_subscription_for_cause", fake_pause)

    outcome = (
        ticket_sla_service_automation.pause_unique_active_service_for_ticket_sla_breach(
            db_session,
            command,
        )
    )

    assert outcome.subscription_id == subscription_id
    assert outcome.pause_episode_id == episode_id
    assert outcome.pause_cause_id == cause_id
    assert captured[0].billing_policy is (
        SubscriptionPauseBillingPolicy.extend_by_effective_pause_duration
    )
    assert captured[0].ticket_id == ticket_id
    assert captured[0].sla_clock_id == clock.id
    assert captured[0].sla_breach_id == breach.id


def test_pause_rejects_an_unverified_sla_event(
    db_session, subscriber, active_subscription, monkeypatch
):
    ticket = Ticket(
        title="Unverified SLA event",
        status=TicketStatus.open.value,
        priority="urgent",
        customer_account_id=subscriber.id,
    )
    db_session.add(ticket)
    db_session.flush()
    ticket_id = ticket.id
    db_session.commit()

    called = False

    def fake_pause(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(account_lifecycle, "pause_subscription_for_cause", fake_pause)

    with pytest.raises(
        ticket_sla_service_automation.TicketSlaServiceAutomationError
    ) as exc_info:
        ticket_sla_service_automation.pause_unique_active_service_for_ticket_sla_breach(
            db_session,
            _pause_command(ticket_id),
        )

    assert exc_info.value.code.endswith(".sla_breach_not_authoritative")
    assert not called


def test_resolved_ticket_is_not_paused(
    db_session, subscriber, active_subscription, monkeypatch
):
    ticket = Ticket(
        title="Already resolved",
        status=TicketStatus.closed.value,
        priority="urgent",
        customer_account_id=subscriber.id,
    )
    db_session.add(ticket)
    db_session.flush()
    ticket_id = ticket.id
    db_session.commit()
    command = _pause_command(ticket_id)
    _seed_authoritative_breach(db_session, ticket_id=ticket_id, command=command)

    called = False

    def fake_pause(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(account_lifecycle, "pause_subscription_for_cause", fake_pause)

    with pytest.raises(
        ticket_sla_service_automation.TicketSlaServiceAutomationError
    ) as exc_info:
        ticket_sla_service_automation.pause_unique_active_service_for_ticket_sla_breach(
            db_session,
            command,
        )

    assert exc_info.value.code.endswith(".ticket_already_resolved")
    assert not called
