"""One customer email per restoration episode, not up to four.

A single payment-resumption event today correctly and independently triggers
up to FOUR customer emails: ``payment_received``, ``invoice_paid``,
``subscription_resumed`` and ``ont_online``. Each is individually correct —
the bug is that nothing correlates them, because
``communication_intents.submit``'s dedupe key
(``event-notification:{event_id}:{template_code}:{channel}``) is unique per
EVENT, and these are four different event types.

This module is the single owner of that correlation. It sits between "one of
the four events fired" and "an email actually goes out": ``NotificationHandler``
routes those four event types here instead of calling ``submit`` directly, and
this module opens/extends a short per-subscriber debounce window
(:class:`app.models.notification.SubscriberNotificationWindow`), then closes
it — either immediately on ``ont_online`` (the natural "episode complete"
signal) or, on timeout, from the sweep in ``app.tasks.notifications`` — and
sends exactly one consolidated email via the existing, unmodified
``communication_intents.submit``.

Nothing about *emission* of the four underlying events changes; this is a
purely additive consolidation layer over how those emissions currently reach
a customer's inbox.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.notification import (
    NotificationWindowCloseReason,
    SubscriberNotificationWindow,
)
from app.models.subscriber import Subscriber
from app.services.communication_intents import (
    CommunicationIntent,
    CommunicationIntentResult,
    submit,
)
from app.services.events.types import Event, EventType
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

#: The four event types a restoration episode can independently emit. Owned
#: here so ``NotificationHandler`` has one place to ask "does this event go
#: through consolidation instead of a direct send?".
CONSOLIDATED_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.payment_received,
        EventType.invoice_paid,
        EventType.subscription_resumed,
        EventType.ont_online,
    }
)

#: Default debounce window length, overridable via the
#: ``notification_restoration_debounce_minutes`` setting (see
#: ``app.tasks.notifications``'s ``_sending_timeout_minutes`` etc. for the
#: identical small-settings-lookup-helper pattern this follows).
DEFAULT_DEBOUNCE_MINUTES = 15

#: The one new template/notification type a window close sends. Content
#: adapts to whichever facts were actually collected (see
#: ``_build_consolidated_message``) — it never claims a payment happened if
#: no payment fact was collected.
RESTORATION_SUMMARY_TEMPLATE_CODE = "service_restored_summary"
RESTORATION_SUMMARY_CATEGORY = "service"


def _debounce_minutes(db: Session) -> int:
    value = resolve_value(
        db, SettingDomain.notification, "notification_restoration_debounce_minutes"
    )
    try:
        return max(1, int(str(value)))
    except (TypeError, ValueError):
        return DEFAULT_DEBOUNCE_MINUTES


def _event_fact(event: Event) -> dict[str, str]:
    """Summarize one collected event into the fact stored on the window.

    Minimal by design: enough for the consolidated template to say something
    true about what happened, never a copy of the full event payload.
    """

    fact: dict[str, str] = {
        "event_type": event.event_type.value,
        "event_id": str(event.event_id),
        "occurred_at": event.occurred_at.isoformat(),
    }
    payload = event.payload or {}
    if event.event_type == EventType.payment_received:
        for key in ("amount", "receipt_number"):
            value = payload.get(key)
            if value is not None:
                fact[key] = str(value)
    elif event.event_type == EventType.invoice_paid:
        for key in ("invoice_number", "amount"):
            value = payload.get(key)
            if value is not None:
                fact[key] = str(value)
    return fact


def _get_open_window(
    db: Session, subscriber_id: UUID
) -> SubscriberNotificationWindow | None:
    return (
        db.query(SubscriberNotificationWindow)
        .filter(SubscriberNotificationWindow.subscriber_id == subscriber_id)
        .filter(SubscriberNotificationWindow.closed_at.is_(None))
        .with_for_update()
        .one_or_none()
    )


def _open_window(
    db: Session, subscriber_id: UUID, now: datetime
) -> SubscriberNotificationWindow:
    """Open a new window, tolerating a concurrent opener.

    The partial unique index (``subscriber_id`` WHERE ``closed_at IS NULL``)
    is the actual guarantee; a savepoint keeps a lost race from poisoning the
    caller's outer transaction (feature code never calls ``db.rollback()`` —
    see ``account_lifecycle.cancel_subscription``'s credit-note savepoint for
    the identical shape).
    """

    window = SubscriberNotificationWindow(
        subscriber_id=subscriber_id,
        opened_at=now,
        window_closes_at=now + timedelta(minutes=_debounce_minutes(db)),
        collected_events=[],
    )
    try:
        with db.begin_nested():
            db.add(window)
            db.flush()
    except IntegrityError:
        existing = _get_open_window(db, subscriber_id)
        if existing is None:
            raise
        return existing
    return window


def record_restoration_fact(db: Session, subscriber_id: UUID | None, event: Event) -> None:
    """Coalesce one of the four restoration-adjacent events into a window.

    Called from ``NotificationHandler`` INSTEAD OF a direct
    ``communication_intents.submit`` for exactly the four
    ``CONSOLIDATED_EVENT_TYPES``. No-ops when there is no subscriber to
    notify. ``ont_online`` closes and sends immediately; every other event
    type only appends its fact and lets the window run (or a later
    ``ont_online``, or the sweep on timeout) decide when to send.

    A late ``ont_online`` arriving after its window already closed is a
    deliberate no-op: the timeout fallback already told the customer, and
    reopening a closed window would recreate the exact multi-email problem
    this module exists to remove.
    """

    if subscriber_id is None:
        logger.debug(
            "Skipped restoration-window fact for event %s: no subscriber",
            event.event_type.value,
        )
        return

    window = _get_open_window(db, subscriber_id)
    if window is None:
        window = _open_window(db, subscriber_id, datetime.now(UTC))

    window.collected_events = [*window.collected_events, _event_fact(event)]
    db.flush()

    if event.event_type == EventType.ont_online:
        close_and_send(
            db, window, close_reason=NotificationWindowCloseReason.completed
        )


def _build_consolidated_message(
    window: SubscriberNotificationWindow, subscriber_name: str
) -> tuple[str, str]:
    facts_by_type = {
        fact.get("event_type"): fact for fact in window.collected_events
    }
    payment_fact = facts_by_type.get(EventType.payment_received.value) or (
        facts_by_type.get(EventType.invoice_paid.value)
    )
    resumed = EventType.subscription_resumed.value in facts_by_type
    online = EventType.ont_online.value in facts_by_type

    lines: list[str] = []
    if payment_fact is not None:
        amount = payment_fact.get("amount")
        if amount:
            lines.append(f"We received your payment of {amount}. Thank you.")
        else:
            lines.append("We received your payment. Thank you.")
    if resumed:
        lines.append("Your service has been resumed.")
    if online:
        lines.append("Your connection is back online.")
    if not lines:
        # A window can close on timeout having collected nothing recognizable
        # (defensive only — record_restoration_fact always appends a fact
        # before a window can exist). Never leave the customer with silence.
        lines.append("Your service has been restored.")

    subject = "Your service has been restored"
    body = (
        f"Dear {subscriber_name},\n\n"
        + "\n\n".join(lines)
        + "\n\nIf you continue to experience any issues, please contact our "
        "support team."
    )
    return subject, body


def _send_consolidated_email(
    db: Session, window: SubscriberNotificationWindow
) -> CommunicationIntentResult:
    subscriber = db.get(Subscriber, window.subscriber_id)
    subscriber_name = (subscriber.name if subscriber and subscriber.name else None) or (
        "Valued Customer"
    )
    subject, body = _build_consolidated_message(window, subscriber_name)
    return submit(
        db,
        CommunicationIntent(
            subscriber_id=window.subscriber_id,
            event_type=RESTORATION_SUMMARY_TEMPLATE_CODE,
            category=RESTORATION_SUMMARY_CATEGORY,
            template_code=RESTORATION_SUMMARY_TEMPLATE_CODE,
            subject=subject,
            body=body,
            persist_policy_suppressions=False,
            # Dedupe on the WINDOW's own id, not any one event's — this is
            # what makes closing an already-closed window (a retried sweep, a
            # sweep racing an ont_online arrival) safe to call twice. See
            # ``communication_intents.submit``'s dedupe-key check.
            dedupe_key=f"restoration-window:{window.id}",
        ),
    )


def close_and_send(
    db: Session,
    window: SubscriberNotificationWindow,
    *,
    close_reason: NotificationWindowCloseReason,
    now: datetime | None = None,
) -> CommunicationIntentResult | None:
    """Close a window and send its one consolidated email.

    A no-op if the window is already closed — the caller (either
    ``record_restoration_fact``'s ``ont_online`` path, holding the window's
    row lock from ``_get_open_window``, or the sweep task, holding it via its
    own ``with_for_update(skip_locked=True)`` claim) is expected to have
    already established that it alone may act on this row within the current
    transaction. This check is the second, cheap half of that guarantee — it
    is what makes a retried sweep call safe.
    """

    if window.closed_at is not None:
        return None

    now = now or datetime.now(UTC)
    window.closed_at = now
    window.close_reason = close_reason
    db.flush()
    result = _send_consolidated_email(db, window)
    window.sent_at = now
    db.flush()
    return result
