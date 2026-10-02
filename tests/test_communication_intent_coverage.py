"""Intent planning and shared physical delivery preserve source outcomes."""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.event_store import EventStatus, EventStore
from app.models.notification import (
    CommunicationIntentRecipient,
    CommunicationIntentRecord,
    Notification,
    NotificationChannel,
    NotificationIntentCoverage,
    NotificationStatus,
    SuppressionReason,
    SuppressionScope,
)
from app.models.subscription_engine import SettingValueType
from app.services.communication_eligibility import suppress
from app.services.communication_intents import (
    CommunicationIntent,
    cover_planned_recipient,
    execute_planned_recipient,
    plan_intent,
    recheck_covered_notification,
    record_delivery_outcome,
    submit,
)
from app.services.customer_notification_policy import has_recent_notification
from app.services.domain_errors import DomainError
from app.services.notification import NotificationDeliveryLatency


def _intent(subscriber, event_type: str, body: str) -> CommunicationIntent:
    return CommunicationIntent(
        subscriber_id=subscriber.id,
        event_type=event_type,
        category="billing",
        subject="Payment receipt",
        body=body,
        channels=(NotificationChannel.email,),
        include_reseller=False,
        recipients={NotificationChannel.email: subscriber.email},
        delivery_latency=NotificationDeliveryLatency.immediate,
    )


def test_planning_does_not_send_and_shared_delivery_covers_both_sources(
    db_session, subscriber
):
    db_session.add(
        DomainSetting(
            domain=SettingDomain.notification,
            key="notification_dedupe_window_minutes",
            value_type=SettingValueType.integer,
            value_text="10",
            is_active=True,
        )
    )
    db_session.commit()
    first = plan_intent(
        db_session, _intent(subscriber, "payment_received", "Receipt body")
    )
    second_intent = CommunicationIntent(
        **{
            **_intent(subscriber, "invoice_paid", "Invoice body").__dict__,
            "dedupe_key": "invoice-paid-covered-replay",
        }
    )
    second = plan_intent(db_session, second_intent)
    evidence = db_session.query(EventStore).all()
    assert evidence and all(row.status is EventStatus.completed for row in evidence)
    assert all(row.processed_at is not None for row in evidence)
    assert db_session.query(Notification).count() == 0
    assert first.recipients[0].accepted and second.recipients[0].accepted
    assert first.recipients[0].planned_at is not None

    floor = first.recipients[0].planned_at + timedelta(seconds=60)
    execution = execute_planned_recipient(
        db_session, first.recipients[0].decision_id, minimum_send_at=floor
    )
    notification = execution.notification
    assert notification is not None
    assert notification.send_at == floor
    notification.body = "Receipt body\n\nInvoice body"
    cover_planned_recipient(
        db_session, second.recipients[0].decision_id, notification.id
    )
    db_session.flush()

    assert db_session.query(Notification).count() == 1
    assert db_session.query(NotificationIntentCoverage).count() == 2
    replay = submit(db_session, second_intent)
    assert replay.replayed
    assert [row.id for row in replay.deliveries] == [notification.id]
    assert [row.id for row in replay.queued] == [notification.id]
    assert has_recent_notification(
        db_session,
        subscriber_id=subscriber.id,
        channel=NotificationChannel.email,
        event_type="invoice_paid",
        category="billing",
        recipient=subscriber.email,
    )
    notification.status = NotificationStatus.delivered
    notification.sent_at = datetime.now(UTC)
    record_delivery_outcome(db_session, notification)
    assert (
        db_session.get(CommunicationIntentRecord, first.intent_id).status == "delivered"
    )
    assert (
        db_session.get(CommunicationIntentRecord, second.intent_id).status
        == "delivered"
    )


def test_claim_recheck_suppresses_sources_without_crediting_delivery(
    db_session, subscriber
):
    plan = plan_intent(
        db_session, _intent(subscriber, "payment_received", "Receipt body")
    )
    execution = execute_planned_recipient(db_session, plan.recipients[0].decision_id)
    notification = execution.notification
    assert notification is not None
    suppress(
        db_session,
        subscriber_id=subscriber.id,
        channel=NotificationChannel.email,
        address=subscriber.email,
        scope=SuppressionScope.all,
        reason=SuppressionReason.bounce,
        note="provider:webhook",
    )

    checked = recheck_covered_notification(db_session, notification.id)
    assert checked.eligible_decision_ids == ()
    assert checked.suppressed_decision_ids == (plan.recipients[0].decision_id,)
    coverage = db_session.query(NotificationIntentCoverage).one()
    assert coverage.status == "suppressed"
    replay = execute_planned_recipient(db_session, plan.recipients[0].decision_id)
    assert not replay.queued
    assert replay.suppression_reason is not None
    notification.status = NotificationStatus.delivered
    record_delivery_outcome(db_session, notification)
    assert (
        db_session.get(CommunicationIntentRecord, plan.intent_id).status == "suppressed"
    )


def test_submit_dedupe_replay_keeps_one_physical_delivery(db_session, subscriber):
    intent = CommunicationIntent(
        **{
            **_intent(subscriber, "payment_received", "Receipt body").__dict__,
            "dedupe_key": "coverage-dedupe-replay",
        }
    )
    first = submit(db_session, intent)
    replay = submit(db_session, intent)
    assert replay.replayed
    assert replay.intent_id == first.intent_id
    assert db_session.query(Notification).count() == 1
    events = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "communication_intent.planned")
        .all()
    )
    assert len(events) == 1
    assert events[0].payload["schema_version"] == 1
    assert events[0].payload["intent_id"] == str(first.intent_id)
    assert events[0].payload["accepted_count"] == 1


@pytest.mark.parametrize(
    "claim_status", (NotificationStatus.failed, NotificationStatus.sending)
)
def test_retry_and_stuck_claim_recheck_retains_or_suppresses_covered_source(
    db_session, subscriber, claim_status
):
    plan = plan_intent(
        db_session, _intent(subscriber, "payment_received", "Receipt body")
    )
    decision_id = plan.recipients[0].decision_id
    notification = execute_planned_recipient(db_session, decision_id).notification
    assert notification is not None
    notification.status = claim_status

    accepted = recheck_covered_notification(db_session, notification.id)
    assert accepted.eligible_decision_ids == (decision_id,)
    assert accepted.suppressed_decision_ids == ()

    suppress(
        db_session,
        subscriber_id=subscriber.id,
        channel=NotificationChannel.email,
        address=subscriber.email,
        scope=SuppressionScope.all,
        reason=SuppressionReason.bounce,
        note="provider:retry-policy",
    )
    rejected = recheck_covered_notification(db_session, notification.id)
    assert rejected.eligible_decision_ids == ()
    assert rejected.suppressed_decision_ids == (decision_id,)
    assert db_session.query(NotificationIntentCoverage).one().status == "suppressed"

    notification.status = NotificationStatus.delivered
    with pytest.raises(DomainError) as failure:
        recheck_covered_notification(db_session, notification.id)
    assert failure.value.code == "communications.intents.notification_not_pending"


def test_recent_event_identity_follows_accepted_coverage_not_primary_fk(
    db_session, subscriber
):
    db_session.add(
        DomainSetting(
            domain=SettingDomain.notification,
            key="notification_dedupe_window_minutes",
            value_type=SettingValueType.integer,
            value_text="10",
            is_active=True,
        )
    )
    db_session.flush()
    primary = plan_intent(
        db_session, _intent(subscriber, "payment_received", "Receipt body")
    ).recipients[0]
    secondary = plan_intent(
        db_session, _intent(subscriber, "invoice_paid", "Paid body")
    ).recipients[0]
    notification = execute_planned_recipient(
        db_session, primary.decision_id
    ).notification
    assert notification is not None
    notification.body = "Receipt body\n\nPaid body"
    cover_planned_recipient(db_session, secondary.decision_id, notification.id)
    primary_decision = db_session.get(CommunicationIntentRecipient, primary.decision_id)
    primary_decision.decision = "suppressed"
    primary_decision.suppression_reason = "source_policy"
    primary_coverage = (
        db_session.query(NotificationIntentCoverage)
        .filter(NotificationIntentCoverage.intent_recipient_id == primary.decision_id)
        .one()
    )
    primary_coverage.status = "suppressed"
    notification.body = "Paid body"
    db_session.flush()

    def recent(event_type: str) -> bool:
        return has_recent_notification(
            db_session,
            subscriber_id=subscriber.id,
            channel=NotificationChannel.email,
            event_type=event_type,
            category="billing",
            recipient=subscriber.email,
        )

    assert not recent("payment_received")
    assert recent("invoice_paid")

    legacy = Notification(
        subscriber_id=subscriber.id,
        channel=NotificationChannel.email,
        event_type="legacy_receipt",
        category="billing",
        recipient=subscriber.email,
        body="Legacy physical row",
        status=NotificationStatus.queued,
    )
    db_session.add(legacy)
    db_session.flush()
    assert recent("legacy_receipt")
