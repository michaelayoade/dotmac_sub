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
    SubscriptionPauseCause,
    SubscriptionPauseEpisode,
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


def _pause_service_for_ticket(db_session, ticket_id):
    command = _pause_command(ticket_id)
    _seed_authoritative_breach(db_session, ticket_id=ticket_id, command=command)
    return (
        ticket_sla_service_automation.pause_unique_active_service_for_ticket_sla_breach(
            db_session,
            command,
        )
    )


def _resume_context(subscription_id, cause_id):
    return CommandContext.system(
        actor="support-reviewer",
        scope=f"subscription:{subscription_id}",
        reason="linked effective ticket resolved",
        idempotency_key=f"ticket-merge-pause-resume:{cause_id}",
    )


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


def test_resume_follows_full_same_account_merge_chain_to_closed_ticket(
    db_session, subscriber, active_subscription
):
    source = Ticket(
        number=f"SRC-{uuid4().hex[:8]}",
        title="Original SLA ticket",
        status=TicketStatus.open.value,
        priority="urgent",
        customer_account_id=subscriber.id,
    )
    db_session.add(source)
    db_session.flush()
    paused = _pause_service_for_ticket(db_session, source.id)

    middle = Ticket(
        number=f"MID-{uuid4().hex[:8]}",
        title="First merge destination",
        status=TicketStatus.canceled.value,
        priority="urgent",
        customer_account_id=subscriber.id,
    )
    destination = Ticket(
        number=f"DST-{uuid4().hex[:8]}",
        title="Effective resolved ticket",
        status=TicketStatus.closed.value,
        priority="urgent",
        customer_account_id=subscriber.id,
    )
    db_session.add_all((middle, destination))
    db_session.flush()
    source.status = TicketStatus.canceled.value
    source.merged_into_ticket_id = middle.id
    middle.merged_into_ticket_id = destination.id
    db_session.commit()

    resumed_at = datetime.now(UTC) + timedelta(hours=1)
    preview = ticket_sla_service_automation.preview_ticket_service_resume(
        db_session,
        cause_id=paused.pause_cause_id,
        proposed_resumed_at=resumed_at,
    )

    assert preview.eligible
    assert preview.source_ticket_id == source.id
    assert preview.source_ticket_number == source.number
    assert preview.source_ticket_display_status == "merged"
    assert preview.effective_ticket_id == destination.id
    assert preview.effective_ticket_status is TicketStatus.closed
    assert tuple(hop.ticket_id for hop in preview.merge_chain) == (
        source.id,
        middle.id,
        destination.id,
    )
    subscription_id = active_subscription.id
    cause_id = paused.pause_cause_id
    context = _resume_context(subscription_id, cause_id)
    db_session.rollback()

    outcome = ticket_sla_service_automation.resume_ticket_paused_service(
        db_session,
        ticket_sla_service_automation.ResumeTicketPausedServiceCommand(
            subscription_id=subscription_id,
            cause_id=cause_id,
            preview_fingerprint=preview.fingerprint,
            resumed_at=resumed_at,
            actor=context.actor,
            reason=context.reason,
            context=context,
        ),
    )

    assert outcome.ticket_id == source.id
    assert outcome.access_restored


def test_resume_preview_blocks_open_merge_destination(
    db_session, subscriber, active_subscription
):
    source = Ticket(
        title="Merged SLA ticket",
        status=TicketStatus.open.value,
        priority="urgent",
        customer_account_id=subscriber.id,
    )
    db_session.add(source)
    db_session.flush()
    paused = _pause_service_for_ticket(db_session, source.id)
    destination = Ticket(
        title="Still open destination",
        status=TicketStatus.open.value,
        priority="urgent",
        customer_account_id=subscriber.id,
    )
    db_session.add(destination)
    db_session.flush()
    source.status = TicketStatus.canceled.value
    source.merged_into_ticket_id = destination.id
    db_session.commit()

    preview = ticket_sla_service_automation.preview_ticket_service_resume(
        db_session,
        cause_id=paused.pause_cause_id,
    )

    assert not preview.eligible
    assert preview.effective_ticket_id == destination.id
    assert preview.effective_ticket_status is TicketStatus.open
    assert "ticket_is_not_resolved" in preview.blocking_reasons


def test_resume_preview_blocks_merge_cycle(db_session, subscriber, active_subscription):
    source = Ticket(
        title="Cyclic merge source",
        status=TicketStatus.open.value,
        priority="urgent",
        customer_account_id=subscriber.id,
    )
    db_session.add(source)
    db_session.flush()
    paused = _pause_service_for_ticket(db_session, source.id)
    destination = Ticket(
        title="Cyclic merge destination",
        status=TicketStatus.canceled.value,
        priority="urgent",
        customer_account_id=subscriber.id,
    )
    db_session.add(destination)
    db_session.flush()
    source.status = TicketStatus.canceled.value
    source.merged_into_ticket_id = destination.id
    destination.merged_into_ticket_id = source.id
    db_session.commit()

    preview = ticket_sla_service_automation.preview_ticket_service_resume(
        db_session,
        cause_id=paused.pause_cause_id,
    )

    assert not preview.eligible
    assert "ticket_merge_cycle" in preview.blocking_reasons


def test_resume_preview_blocks_merge_customer_mismatch(
    db_session, subscriber, active_subscription
):
    source = Ticket(
        title="Same-account merge source",
        status=TicketStatus.open.value,
        priority="urgent",
        customer_account_id=subscriber.id,
    )
    db_session.add(source)
    db_session.flush()
    paused = _pause_service_for_ticket(db_session, source.id)
    destination = Ticket(
        title="Unlinked merge destination",
        status=TicketStatus.closed.value,
        priority="urgent",
    )
    db_session.add(destination)
    db_session.flush()
    source.status = TicketStatus.canceled.value
    source.merged_into_ticket_id = destination.id
    db_session.commit()

    preview = ticket_sla_service_automation.preview_ticket_service_resume(
        db_session,
        cause_id=paused.pause_cause_id,
    )

    assert not preview.eligible
    assert "ticket_merge_customer_mismatch" in preview.blocking_reasons


def test_resume_preview_blocks_missing_merge_target(subscriber):
    missing_id = uuid4()
    source = Ticket(
        id=uuid4(),
        title="Missing merge destination",
        status=TicketStatus.canceled.value,
        priority="urgent",
        customer_account_id=subscriber.id,
        merged_into_ticket_id=missing_id,
    )

    class MissingTargetSession:
        @staticmethod
        def get(model, entity_id):
            assert model is Ticket
            assert entity_id == missing_id
            return None

    resolution = ticket_sla_service_automation._resolve_ticket_pause_ticket(
        MissingTargetSession(),
        source=source,
        expected_account_id=subscriber.id,
    )

    assert resolution.effective is source
    assert resolution.blocking_reasons == ("ticket_merge_target_missing",)


def test_destination_change_invalidates_merge_aware_resume_preview(
    db_session, subscriber, active_subscription
):
    source = Ticket(
        title="Stale preview source",
        status=TicketStatus.open.value,
        priority="urgent",
        customer_account_id=subscriber.id,
    )
    db_session.add(source)
    db_session.flush()
    paused = _pause_service_for_ticket(db_session, source.id)
    destination = Ticket(
        title="Destination status changes",
        status=TicketStatus.closed.value,
        priority="urgent",
        customer_account_id=subscriber.id,
    )
    db_session.add(destination)
    db_session.flush()
    source.status = TicketStatus.canceled.value
    source.merged_into_ticket_id = destination.id
    db_session.commit()
    resumed_at = datetime.now(UTC) + timedelta(hours=1)
    preview = ticket_sla_service_automation.preview_ticket_service_resume(
        db_session,
        cause_id=paused.pause_cause_id,
        proposed_resumed_at=resumed_at,
    )
    assert preview.eligible
    subscription_id = active_subscription.id
    cause_id = paused.pause_cause_id
    destination.status = TicketStatus.open.value
    db_session.commit()

    context = _resume_context(subscription_id, cause_id)
    db_session.rollback()
    with pytest.raises(
        ticket_sla_service_automation.TicketSlaServiceAutomationError
    ) as exc_info:
        ticket_sla_service_automation.resume_ticket_paused_service(
            db_session,
            ticket_sla_service_automation.ResumeTicketPausedServiceCommand(
                subscription_id=subscription_id,
                cause_id=cause_id,
                preview_fingerprint=preview.fingerprint,
                resumed_at=resumed_at,
                actor=context.actor,
                reason=context.reason,
                context=context,
            ),
        )

    assert exc_info.value.code.endswith(".stale_resume_preview")


def _factory(session):
    """Share the test session; close() only ends the transaction."""

    session.close = session.rollback  # type: ignore[method-assign]
    return lambda: session


def _new_auto_pause(db_session, subscriber, status=TicketStatus.open.value):
    from dataclasses import replace

    ticket = Ticket(
        number=f"AUTO-{uuid4().hex[:8]}",
        title="Service fault",
        status=status,
        priority="urgent",
        customer_account_id=subscriber.id,
    )
    db_session.add(ticket)
    db_session.flush()
    command = replace(
        _pause_command(ticket.id),
        resume_policy=SubscriptionPauseResumePolicy.auto_on_ticket_resolution,
    )
    _seed_authoritative_breach(db_session, ticket_id=ticket.id, command=command)
    outcome = (
        ticket_sla_service_automation.pause_unique_active_service_for_ticket_sla_breach(
            db_session, command
        )
    )
    return ticket, outcome


def test_auto_policy_pause_stays_until_ticket_resolves(
    db_session, subscriber, active_subscription
):
    ticket, outcome = _new_auto_pause(db_session, subscriber)
    ticket_id = ticket.id

    counts = ticket_sla_service_automation.auto_resume_resolved_ticket_pauses(
        _factory(db_session)
    )

    assert counts["resumed"] == 0
    assert counts["skipped"] == 1
    db_session.refresh(active_subscription)
    assert active_subscription.status.value == "paused"
    assert db_session.get(Ticket, ticket_id).status == TicketStatus.open.value


@pytest.mark.parametrize(
    "final_status", [TicketStatus.closed.value, TicketStatus.canceled.value]
)
def test_auto_policy_pause_resumes_and_extends_billing_when_ticket_resolves(
    db_session, subscriber, active_subscription, final_status
):
    original_anchor = datetime(2030, 1, 1, tzinfo=UTC)
    active_subscription.next_billing_at = original_anchor
    db_session.commit()
    ticket, outcome = _new_auto_pause(db_session, subscriber)
    ticket.status = final_status
    db_session.commit()

    now = datetime.now(UTC) + timedelta(hours=2)
    counts = ticket_sla_service_automation.auto_resume_resolved_ticket_pauses(
        _factory(db_session), now=now
    )

    assert counts["total"] == 1 and counts["resumed"] == 1
    assert counts["skipped"] == 0 and counts["failed"] == 0
    db_session.refresh(active_subscription)
    episode = db_session.get(SubscriptionPauseEpisode, outcome.pause_episode_id)
    effective_at = episode.effective_at
    if effective_at.tzinfo is None:
        effective_at = effective_at.replace(tzinfo=UTC)
    assert active_subscription.status.value == "active"
    resulting = active_subscription.next_billing_at
    if resulting.tzinfo is None:
        resulting = resulting.replace(tzinfo=UTC)
    assert resulting == original_anchor + timedelta(
        seconds=int((now - effective_at).total_seconds())
    )

    again = ticket_sla_service_automation.auto_resume_resolved_ticket_pauses(
        _factory(db_session), now=now
    )
    assert again["total"] == 0


def test_manual_policy_pause_is_not_auto_resumed(
    db_session, subscriber, active_subscription
):
    ticket = Ticket(
        number=f"MAN-{uuid4().hex[:8]}",
        title="Manual policy",
        status=TicketStatus.open.value,
        priority="urgent",
        customer_account_id=subscriber.id,
    )
    db_session.add(ticket)
    db_session.flush()
    ticket_id = ticket.id
    db_session.commit()
    outcome = _pause_service_for_ticket(db_session, ticket_id)
    cause = db_session.get(SubscriptionPauseCause, outcome.pause_cause_id)
    assert (
        cause.resume_policy
        == SubscriptionPauseResumePolicy.manual_after_ticket_resolution.value
    )
    db_session.get(Ticket, ticket_id).status = TicketStatus.closed.value
    db_session.commit()

    counts = ticket_sla_service_automation.auto_resume_resolved_ticket_pauses(
        _factory(db_session)
    )

    assert counts["total"] == 0 and counts["resumed"] == 0
    db_session.refresh(active_subscription)
    assert active_subscription.status.value == "paused"
    assert db_session.get(SubscriptionPauseCause, outcome.pause_cause_id).status == (
        "active"
    )


def test_open_ticket_skip_is_counted_separately(
    db_session, subscriber, active_subscription
):
    _new_auto_pause(db_session, subscriber)
    counts = ticket_sla_service_automation.auto_resume_resolved_ticket_pauses(
        _factory(db_session)
    )
    assert counts["skipped_open_ticket"] == 1
    assert counts["skipped_other"] == 0 and counts["skipped_prepaid_blocker"] == 0
