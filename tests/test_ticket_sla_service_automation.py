from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.billing import ServiceEntitlement, ServiceEntitlementStatus
from app.models.catalog import BillingMode
from app.models.event_store import EventStore
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionAnchorBasis,
    ServiceExtensionEntry,
    ServiceExtensionScope,
    ServiceExtensionStatus,
)
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


def test_resume_accepts_applied_extension_as_prepaid_coverage(
    db_session, subscriber, active_subscription
):
    now = datetime.now(UTC)
    entitlement_end = now - timedelta(days=3)
    captured_anchor = now + timedelta(days=6)
    active_subscription.billing_mode = BillingMode.prepaid
    active_subscription.next_billing_at = captured_anchor
    entitlement = ServiceEntitlement(
        account_id=subscriber.id,
        subscription_id=active_subscription.id,
        starts_at=entitlement_end - timedelta(days=30),
        ends_at=entitlement_end,
        amount_funded=active_subscription.unit_price or 0,
        currency="NGN",
        status=ServiceEntitlementStatus.active,
        metadata_={"source": "test_funded_prepaid_renewal"},
    )
    extension = ServiceExtension(
        reason="reviewed cabinet outage compensation",
        window_start=now - timedelta(days=20),
        window_end=now - timedelta(days=9),
        days=9,
        scope_type=ServiceExtensionScope.subscribers,
        scope_subscriber_ids=[str(subscriber.id)],
        status=ServiceExtensionStatus.applied,
        applied_at=now - timedelta(days=8),
    )
    ticket = Ticket(
        title="Resolution SLA breached during extension grant",
        status=TicketStatus.open.value,
        priority="urgent",
        customer_account_id=subscriber.id,
    )
    db_session.add_all((entitlement, extension, ticket))
    db_session.flush()
    db_session.add(
        ServiceExtensionEntry(
            extension_id=extension.id,
            subscription_id=active_subscription.id,
            subscriber_id=subscriber.id,
            previous_next_billing_at=entitlement_end,
            grant_starts_at=entitlement_end,
            grant_ends_at=captured_anchor,
            anchor_basis=ServiceExtensionAnchorBasis.existing_billing_anchor,
            new_next_billing_at=captured_anchor,
        )
    )
    ticket_id = ticket.id
    db_session.commit()

    pause_command = _pause_command(ticket_id)
    _seed_authoritative_breach(
        db_session,
        ticket_id=ticket_id,
        command=pause_command,
    )
    paused = (
        ticket_sla_service_automation.pause_unique_active_service_for_ticket_sla_breach(
            db_session,
            pause_command,
        )
    )
    ticket = db_session.get(Ticket, ticket_id)
    assert ticket is not None
    ticket.status = TicketStatus.closed.value
    ticket.closed_at = now
    db_session.commit()

    resumed_at = datetime.now(UTC) + timedelta(days=1)
    preview = ticket_sla_service_automation.preview_ticket_service_resume(
        db_session,
        cause_id=paused.pause_cause_id,
        proposed_resumed_at=resumed_at,
    )

    assert preview.eligible
    assert preview.blocking_reasons == ()
    subscription_id = active_subscription.id
    pause_cause_id = paused.pause_cause_id
    context = CommandContext.system(
        actor="support-reviewer",
        scope=f"subscription:{subscription_id}",
        reason="linked ticket resolved and extension coverage reviewed",
        idempotency_key=f"ticket-pause-extension-resume:{pause_cause_id}",
    )
    db_session.rollback()
    outcome = ticket_sla_service_automation.resume_ticket_paused_service(
        db_session,
        ticket_sla_service_automation.ResumeTicketPausedServiceCommand(
            subscription_id=subscription_id,
            cause_id=pause_cause_id,
            preview_fingerprint=preview.fingerprint,
            resumed_at=resumed_at,
            actor=context.actor,
            reason=context.reason,
            context=context,
        ),
    )

    assert outcome.access_restored
