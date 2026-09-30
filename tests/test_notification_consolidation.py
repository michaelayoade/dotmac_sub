"""One consolidated customer email per restoration episode, not up to four.

Knowledge slug ``dotmac-debt-register`` item S10 (2026-09-30): a single
payment-resumption episode correctly and independently emits up to FOUR
events -- ``payment_received``, ``invoice_paid``, ``subscription_resumed``,
``ont_online`` -- each of which used to reach ``communication_intents.submit``
directly. These tests exercise ``app.services.notification_consolidation``,
the debounce/coalescing layer that now sits between those events and a send.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.notification import (
    CommunicationIntentRecord,
    NotificationWindowCloseReason,
    SubscriberNotificationWindow,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.events.types import Event, EventType
from app.services.notification_consolidation import (
    RESTORATION_SUMMARY_TEMPLATE_CODE,
    close_and_send,
    record_restoration_fact,
)
from app.tasks.notifications import _sweep_notification_windows_stats


def _subscriber(db_session, **overrides) -> Subscriber:
    defaults = dict(
        first_name="Test",
        last_name=f"User{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex}@example.test",
        status=SubscriberStatus.active,
    )
    defaults.update(overrides)
    subscriber = Subscriber(**defaults)
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _event(event_type: EventType, subscriber_id, **payload) -> Event:
    return Event(event_type=event_type, payload=payload, subscriber_id=subscriber_id)


def _sent_records(db_session, subscriber_id) -> list[CommunicationIntentRecord]:
    return (
        db_session.query(CommunicationIntentRecord)
        .filter(CommunicationIntentRecord.subscriber_id == subscriber_id)
        .filter(
            CommunicationIntentRecord.template_code == RESTORATION_SUMMARY_TEMPLATE_CODE
        )
        .all()
    )


def _open_windows(db_session, subscriber_id):
    return (
        db_session.query(SubscriberNotificationWindow)
        .filter(SubscriberNotificationWindow.subscriber_id == subscriber_id)
        .all()
    )


def test_payment_then_resumption_then_online_produces_exactly_one_email(db_session):
    """The core claim: three of the four events collapse into one send, and
    that send's content reflects every fact actually collected -- not just
    the last one."""
    subscriber = _subscriber(db_session)

    record_restoration_fact(
        db_session,
        subscriber.id,
        _event(EventType.payment_received, subscriber.id, amount="4500"),
    )
    record_restoration_fact(
        db_session,
        subscriber.id,
        _event(EventType.subscription_resumed, subscriber.id),
    )
    # ont_online is the natural completion signal -- it closes the window and
    # sends immediately, without waiting for the sweep.
    record_restoration_fact(
        db_session, subscriber.id, _event(EventType.ont_online, subscriber.id)
    )

    sent = _sent_records(db_session, subscriber.id)
    assert len(sent) == 1
    body = sent[0].body or ""
    assert "4500" in body
    assert "resumed" in body.lower()
    assert "online" in body.lower()

    windows = _open_windows(db_session, subscriber.id)
    assert len(windows) == 1
    assert windows[0].closed_at is not None
    assert windows[0].close_reason == NotificationWindowCloseReason.completed
    assert windows[0].sent_at is not None


def test_a_fact_with_no_ont_online_waits_for_the_sweep_then_sends_once(db_session):
    """Without the completion signal, the window stays open until the sweep
    closes it on timeout -- the customer is not left with silence, but is
    also not emailed before the debounce window elapses."""
    subscriber = _subscriber(db_session)

    record_restoration_fact(
        db_session,
        subscriber.id,
        _event(EventType.payment_received, subscriber.id, amount="1200"),
    )

    assert _sent_records(db_session, subscriber.id) == []
    windows = _open_windows(db_session, subscriber.id)
    assert len(windows) == 1
    assert windows[0].closed_at is None

    # Simulate the debounce window having elapsed.
    windows[0].window_closes_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.flush()

    stats = _sweep_notification_windows_stats(db_session)
    assert stats["closed"] == 1
    assert stats["sent"] == 1

    sent = _sent_records(db_session, subscriber.id)
    assert len(sent) == 1
    assert "1200" in (sent[0].body or "")

    reloaded = _open_windows(db_session, subscriber.id)[0]
    assert reloaded.close_reason == NotificationWindowCloseReason.timeout
    assert reloaded.sent_at is not None


def test_two_different_subscribers_never_share_a_window(db_session):
    """A window is per-subscriber. One subscriber's events must never appear
    in another's consolidated email, and each gets their own send."""
    alice = _subscriber(db_session)
    bob = _subscriber(db_session)

    record_restoration_fact(
        db_session,
        alice.id,
        _event(EventType.payment_received, alice.id, amount="ALICE-AMOUNT"),
    )
    record_restoration_fact(
        db_session,
        bob.id,
        _event(EventType.payment_received, bob.id, amount="BOB-AMOUNT"),
    )
    record_restoration_fact(
        db_session, alice.id, _event(EventType.ont_online, alice.id)
    )
    record_restoration_fact(db_session, bob.id, _event(EventType.ont_online, bob.id))

    alice_sent = _sent_records(db_session, alice.id)
    bob_sent = _sent_records(db_session, bob.id)
    assert len(alice_sent) == 1
    assert len(bob_sent) == 1
    assert "ALICE-AMOUNT" in (alice_sent[0].body or "")
    assert "BOB-AMOUNT" not in (alice_sent[0].body or "")
    assert "BOB-AMOUNT" in (bob_sent[0].body or "")
    assert "ALICE-AMOUNT" not in (bob_sent[0].body or "")


def test_two_events_for_the_same_subscriber_open_exactly_one_window(db_session):
    """The partial unique index's invariant, proven at the application layer:
    a subscriber accumulates facts into ONE window, never two."""
    subscriber = _subscriber(db_session)

    record_restoration_fact(
        db_session,
        subscriber.id,
        _event(EventType.payment_received, subscriber.id),
    )
    record_restoration_fact(
        db_session,
        subscriber.id,
        _event(EventType.invoice_paid, subscriber.id),
    )

    windows = _open_windows(db_session, subscriber.id)
    assert len(windows) == 1
    assert len(windows[0].collected_events) == 2


def test_a_late_ont_online_after_the_window_already_closed_is_a_no_op(db_session):
    """The exact edge case this module exists to close: a completion signal
    arriving after its episode already timed out and sent must not reopen a
    window or send a second email -- that would recreate the multi-email
    problem in a new shape."""
    subscriber = _subscriber(db_session)

    record_restoration_fact(
        db_session,
        subscriber.id,
        _event(EventType.subscription_resumed, subscriber.id),
    )
    window = _open_windows(db_session, subscriber.id)[0]
    window.window_closes_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.flush()
    stats = _sweep_notification_windows_stats(db_session)
    assert stats["sent"] == 1
    assert len(_sent_records(db_session, subscriber.id)) == 1

    # The late signal.
    record_restoration_fact(
        db_session, subscriber.id, _event(EventType.ont_online, subscriber.id)
    )

    assert len(_sent_records(db_session, subscriber.id)) == 1
    windows = _open_windows(db_session, subscriber.id)
    assert len(windows) == 1, "a late ont_online must not open a second window"


def test_closing_an_already_closed_window_is_a_safe_no_op(db_session):
    """The cheap, in-process half of the concurrency guarantee described in
    ``close_and_send``'s docstring: calling it twice on the same window
    object sends exactly once. (The row-locked claim that makes two
    *concurrent* callers agree on who gets to call it at all is the same
    ``with_for_update(skip_locked=True)`` discipline already proven for
    ``app.tasks.notifications._deliver_notification_queue_stats`` in
    ``tests/architecture/test_notification_delivery_hotfix_boundary.py`` --
    not re-proven here structurally to avoid duplicating that guard.)"""
    subscriber = _subscriber(db_session)
    record_restoration_fact(
        db_session, subscriber.id, _event(EventType.payment_received, subscriber.id)
    )
    window = _open_windows(db_session, subscriber.id)[0]

    first = close_and_send(
        db_session, window, close_reason=NotificationWindowCloseReason.completed
    )
    assert first is not None

    second = close_and_send(
        db_session, window, close_reason=NotificationWindowCloseReason.timeout
    )
    assert second is None
    assert len(_sent_records(db_session, subscriber.id)) == 1


def test_a_window_closing_with_only_a_resumption_fact_never_claims_a_payment(
    db_session,
):
    """The template must not say a payment happened when no payment fact was
    collected -- e.g. a manual admin resume with no billing event."""
    subscriber = _subscriber(db_session)
    record_restoration_fact(
        db_session,
        subscriber.id,
        _event(EventType.subscription_resumed, subscriber.id),
    )
    record_restoration_fact(
        db_session, subscriber.id, _event(EventType.ont_online, subscriber.id)
    )

    sent = _sent_records(db_session, subscriber.id)
    assert len(sent) == 1
    body = (sent[0].body or "").lower()
    assert "payment" not in body
    assert "resumed" in body
    assert "online" in body
