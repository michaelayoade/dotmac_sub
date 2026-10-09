from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from app.models.billing import ServiceEntitlement, ServiceEntitlementStatus
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionAnchorBasis,
    ServiceExtensionEntry,
    ServiceExtensionScope,
    ServiceExtensionStatus,
)
from app.models.subscriber import SubscriberStatus
from app.models.subscription_pause import (
    SubscriptionPauseBillingPolicy,
    SubscriptionPauseCause,
    SubscriptionPauseEpisode,
    SubscriptionPauseEpisodeStatus,
    SubscriptionPauseReason,
    SubscriptionPauseResumePolicy,
    SubscriptionPauseSource,
)
from app.services import account_lifecycle
from app.services.owner_commands import CommandContext


def _context(*, reason: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext.system(
        actor="pause-lifecycle-test",
        scope="subscription:pause-test",
        reason=reason,
        correlation_id=command_id,
        causation_id=command_id,
        idempotency_key=f"pause-lifecycle-test:{command_id}",
    )


def _as_utc(value: datetime | None) -> datetime:
    assert value is not None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def test_pause_and_resume_preserve_exact_unused_service_time(
    db_session, subscriber, active_subscription
):
    effective_at = datetime(2026, 9, 20, 8, 30, tzinfo=UTC)
    resumed_at = effective_at + timedelta(days=4, hours=3, minutes=2, seconds=1)
    original_anchor = datetime(2026, 10, 1, 8, 30, tzinfo=UTC)
    active_subscription.next_billing_at = original_anchor
    db_session.commit()

    pause_context = _context(reason="confirmed resolution SLA breach")
    paused = account_lifecycle.pause_subscription_for_cause(
        db_session,
        account_lifecycle.PauseSubscriptionCauseCommand(
            subscription_id=active_subscription.id,
            reason=SubscriptionPauseReason.ticket_resolution_sla_breach,
            source_type=SubscriptionPauseSource.automation_workflow,
            source_id=f"test-source:{pause_context.command_id}",
            selection_policy="unique_active_subscription",
            resume_policy=(
                SubscriptionPauseResumePolicy.manual_after_ticket_resolution
            ),
            billing_policy=(
                SubscriptionPauseBillingPolicy.extend_by_effective_pause_duration
            ),
            requested_at=effective_at,
            effective_at=effective_at,
            actor=pause_context.actor,
            idempotency_key=pause_context.idempotency_key or "",
            context=pause_context,
        ),
    )

    db_session.refresh(active_subscription)
    db_session.refresh(subscriber)
    assert active_subscription.status is SubscriptionStatus.paused
    assert _as_utc(active_subscription.next_billing_at) == original_anchor
    assert subscriber.status is SubscriberStatus.paused

    resume_context = _context(reason="linked ticket resolved")
    resumed = account_lifecycle.release_pause_cause_and_resume_subscription(
        db_session,
        account_lifecycle.ResumePausedSubscriptionCauseCommand(
            cause_id=paused.cause_id,
            preview_fingerprint="test-preview-fingerprint",
            resumed_at=resumed_at,
            actor=resume_context.actor,
            reason="linked ticket resolved and reviewed",
            context=resume_context,
        ),
    )

    expected_seconds = int((resumed_at - effective_at).total_seconds())
    expected_anchor = original_anchor + timedelta(seconds=expected_seconds)
    db_session.refresh(active_subscription)
    db_session.refresh(subscriber)
    episode = db_session.get(SubscriptionPauseEpisode, paused.episode_id)
    cause = db_session.get(SubscriptionPauseCause, paused.cause_id)

    assert resumed.paused_seconds == expected_seconds
    assert _as_utc(resumed.resulting_next_billing_at) == expected_anchor
    assert active_subscription.status is SubscriptionStatus.active
    assert _as_utc(active_subscription.next_billing_at) == expected_anchor
    assert subscriber.status is SubscriberStatus.active
    assert episode is not None
    assert episode.status == SubscriptionPauseEpisodeStatus.resumed.value
    assert episode.effective_duration_seconds == expected_seconds
    assert cause is not None
    assert cause.status == "released"


def test_prepaid_resume_grants_exact_pause_compensation_once(
    db_session, subscriber, active_subscription
):
    effective_at = datetime(2026, 9, 20, 8, 30, tzinfo=UTC)
    resumed_at = effective_at + timedelta(days=15)
    original_anchor = datetime(2026, 10, 1, 8, 30, tzinfo=UTC)
    active_subscription.billing_mode = BillingMode.prepaid
    active_subscription.next_billing_at = original_anchor
    db_session.add(
        ServiceEntitlement(
            account_id=subscriber.id,
            subscription_id=active_subscription.id,
            starts_at=datetime(2026, 9, 1, 8, 30, tzinfo=UTC),
            ends_at=original_anchor,
            amount_funded=active_subscription.unit_price or 0,
            currency="NGN",
            status=ServiceEntitlementStatus.active,
            metadata_={"source": "test_paid_prepaid_invoice"},
        )
    )
    db_session.commit()

    pause_context = _context(reason="confirmed resolution SLA breach")
    paused = account_lifecycle.pause_subscription_for_cause(
        db_session,
        account_lifecycle.PauseSubscriptionCauseCommand(
            subscription_id=active_subscription.id,
            reason=SubscriptionPauseReason.ticket_resolution_sla_breach,
            source_type=SubscriptionPauseSource.automation_workflow,
            source_id=f"test-source:{pause_context.command_id}",
            selection_policy="unique_active_subscription",
            resume_policy=(
                SubscriptionPauseResumePolicy.manual_after_ticket_resolution
            ),
            billing_policy=(
                SubscriptionPauseBillingPolicy.extend_by_effective_pause_duration
            ),
            requested_at=effective_at,
            effective_at=effective_at,
            actor=pause_context.actor,
            idempotency_key=pause_context.idempotency_key or "",
            context=pause_context,
        ),
    )

    resume_context = _context(reason="linked ticket resolved")
    command = account_lifecycle.ResumePausedSubscriptionCauseCommand(
        cause_id=paused.cause_id,
        preview_fingerprint="prepaid-preview-fingerprint",
        resumed_at=resumed_at,
        actor=resume_context.actor,
        reason="linked ticket resolved and reviewed",
        context=resume_context,
    )
    first = account_lifecycle.release_pause_cause_and_resume_subscription(
        db_session, command
    )
    replay = account_lifecycle.release_pause_cause_and_resume_subscription(
        db_session, command
    )

    compensation = tuple(
        db_session.scalars(
            select(ServiceEntitlement).where(
                ServiceEntitlement.source_pause_episode_id == paused.episode_id
            )
        ).all()
    )
    assert first.resumed
    assert replay.replayed
    assert len(compensation) == 1
    assert _as_utc(compensation[0].starts_at) == original_anchor
    assert _as_utc(compensation[0].ends_at) == original_anchor + timedelta(days=15)
    assert _as_utc(active_subscription.next_billing_at) == _as_utc(
        compensation[0].ends_at
    )


def test_prepaid_resume_preserves_unused_applied_extension_time(
    db_session, subscriber, active_subscription
):
    effective_at = datetime(2026, 10, 5, 15, 32, tzinfo=UTC)
    resumed_at = effective_at + timedelta(days=1)
    entitlement_end = datetime(2026, 10, 2, tzinfo=UTC)
    original_anchor = datetime(2026, 10, 11, tzinfo=UTC)
    active_subscription.billing_mode = BillingMode.prepaid
    active_subscription.next_billing_at = original_anchor
    entitlement = ServiceEntitlement(
        account_id=subscriber.id,
        subscription_id=active_subscription.id,
        starts_at=datetime(2026, 9, 2, tzinfo=UTC),
        ends_at=entitlement_end,
        amount_funded=active_subscription.unit_price or 0,
        currency="NGN",
        status=ServiceEntitlementStatus.active,
        metadata_={"source": "test_funded_prepaid_renewal"},
    )
    extension = ServiceExtension(
        reason="Cabinet disconnection compensation",
        window_start=datetime(2026, 9, 12, tzinfo=UTC),
        window_end=datetime(2026, 9, 23, tzinfo=UTC),
        days=9,
        scope_type=ServiceExtensionScope.subscribers,
        scope_subscriber_ids=[str(subscriber.id)],
        status=ServiceExtensionStatus.applied,
        applied_at=datetime(2026, 9, 23, 16, 5, tzinfo=UTC),
    )
    db_session.add_all((entitlement, extension))
    db_session.flush()
    db_session.add(
        ServiceExtensionEntry(
            extension_id=extension.id,
            subscription_id=active_subscription.id,
            subscriber_id=subscriber.id,
            previous_next_billing_at=entitlement_end,
            grant_starts_at=entitlement_end,
            grant_ends_at=original_anchor,
            anchor_basis=ServiceExtensionAnchorBasis.existing_billing_anchor,
            new_next_billing_at=original_anchor,
        )
    )
    db_session.commit()

    pause_context = _context(reason="confirmed resolution SLA breach")
    paused = account_lifecycle.pause_subscription_for_cause(
        db_session,
        account_lifecycle.PauseSubscriptionCauseCommand(
            subscription_id=active_subscription.id,
            reason=SubscriptionPauseReason.ticket_resolution_sla_breach,
            source_type=SubscriptionPauseSource.automation_workflow,
            source_id=f"test-source:{pause_context.command_id}",
            selection_policy="unique_active_subscription",
            resume_policy=(
                SubscriptionPauseResumePolicy.manual_after_ticket_resolution
            ),
            billing_policy=(
                SubscriptionPauseBillingPolicy.extend_by_effective_pause_duration
            ),
            requested_at=effective_at,
            effective_at=effective_at,
            actor=pause_context.actor,
            idempotency_key=pause_context.idempotency_key or "",
            context=pause_context,
        ),
    )

    resume_context = _context(reason="linked ticket resolved")
    outcome = account_lifecycle.release_pause_cause_and_resume_subscription(
        db_session,
        account_lifecycle.ResumePausedSubscriptionCauseCommand(
            cause_id=paused.cause_id,
            preview_fingerprint="extension-backed-prepaid-preview",
            resumed_at=resumed_at,
            actor=resume_context.actor,
            reason="linked ticket resolved and extension coverage reviewed",
            context=resume_context,
        ),
    )

    compensation = db_session.scalar(
        select(ServiceEntitlement).where(
            ServiceEntitlement.source_pause_episode_id == paused.episode_id
        )
    )
    assert outcome.resumed
    assert compensation is not None
    assert _as_utc(compensation.starts_at) == original_anchor
    assert _as_utc(compensation.ends_at) == original_anchor + timedelta(days=1)
    assert compensation.currency == "NGN"
    assert len((compensation.metadata_ or {})["coverage_fingerprint"]) == 64


def test_pause_replay_reuses_the_original_cause(db_session, active_subscription):
    effective_at = datetime(2026, 9, 20, 8, 30, tzinfo=UTC)
    active_subscription.next_billing_at = effective_at + timedelta(days=10)
    db_session.commit()
    context = _context(reason="confirmed resolution SLA breach")
    command = account_lifecycle.PauseSubscriptionCauseCommand(
        subscription_id=active_subscription.id,
        reason=SubscriptionPauseReason.ticket_resolution_sla_breach,
        source_type=SubscriptionPauseSource.automation_workflow,
        source_id=f"test-source:{context.command_id}",
        selection_policy="unique_active_subscription",
        resume_policy=SubscriptionPauseResumePolicy.manual_after_ticket_resolution,
        billing_policy=(
            SubscriptionPauseBillingPolicy.extend_by_effective_pause_duration
        ),
        requested_at=effective_at,
        effective_at=effective_at,
        actor=context.actor,
        idempotency_key=context.idempotency_key or "",
        context=context,
    )

    first = account_lifecycle.pause_subscription_for_cause(db_session, command)
    replay = account_lifecycle.pause_subscription_for_cause(db_session, command)

    assert replay.replayed
    assert replay.episode_id == first.episode_id
    assert replay.cause_id == first.cause_id
    cause_count = len(db_session.scalars(select(SubscriptionPauseCause.id)).all())
    assert cause_count == 1
