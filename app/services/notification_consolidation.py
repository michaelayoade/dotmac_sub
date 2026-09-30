"""A generic per-subscriber debounce/coalescing facility for related events.

A single payment-resumption event today correctly and independently triggers
up to FOUR customer emails: ``payment_received``, ``invoice_paid``,
``subscription_resumed`` and ``ont_online``. Each is individually correct —
the bug is that nothing correlates them, because
``communication_intents.submit``'s dedupe key
(``event-notification:{event_id}:{template_code}:{channel}``) is unique per
EVENT, and these are four different event types.

This module is the single owner of that correlation, and it is a FACILITY,
not a point fix for the restoration case alone: a **consolidation group** is a
declared registry entry (:data:`CONSOLIDATION_GROUPS`, keyed by a plain string
id — a new vocabulary is a declaration registry, never a bare enum/frozenset,
per ADR-0008) naming which event types are its members, which of those are
"closing" signals, and how to render the one consolidated message. Today
exactly one group exists (``service_restoration``); a future unrelated set of
correlated events (e.g. an installation flow) registers its OWN group here
rather than either overloading this one or duplicating this module.

``NotificationHandler`` routes any event whose type belongs to a registered
group here instead of calling ``submit`` directly. This module opens/extends a
short per-subscriber, per-group debounce window
(:class:`app.models.notification.SubscriberNotificationWindow`), then closes
it — either immediately on one of the group's closing event types (the
natural "episode complete" signal) or, on timeout, from the sweep in
``app.tasks.notifications`` — and sends exactly one consolidated email via the
existing, unmodified ``communication_intents.submit``.

Nothing about *emission* of the underlying events changes; this is a purely
additive consolidation layer over how those emissions currently reach a
customer's inbox.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ConsolidationGroupSpec:
    """One registered consolidation group.

    ``member_event_types`` are the events this group collects facts from.
    ``closing_event_types`` (a subset of members) are the signals that close
    the window and send immediately, rather than waiting for the sweep's
    timeout fallback — the group's "this episode is definitely over" facts.
    """

    member_event_types: frozenset[EventType]
    closing_event_types: frozenset[EventType]
    template_code: str
    category: str
    debounce_setting_key: str
    default_debounce_minutes: int
    build_message: Callable[[SubscriberNotificationWindow, str], tuple[str, str]]


def _build_restoration_message(
    window: SubscriberNotificationWindow, subscriber_name: str
) -> tuple[str, str]:
    facts_by_type = {fact.get("event_type"): fact for fact in window.collected_events}
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
        # (defensive only — record_consolidated_fact always appends a fact
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


#: The template/notification-category constants for the one group registered
#: today. Kept as named constants (not just inline strings in the registry
#: entry below) because ``communication_intents`` callers and tests reference
#: them directly.
RESTORATION_SUMMARY_TEMPLATE_CODE = "service_restored_summary"
RESTORATION_SUMMARY_CATEGORY = "service"

#: Default debounce window length for the restoration group, overridable via
#: the ``notification_restoration_debounce_minutes`` setting (see
#: ``app.tasks.notifications``'s ``_sending_timeout_minutes`` etc. for the
#: identical small-settings-lookup-helper pattern this follows).
DEFAULT_DEBOUNCE_MINUTES = 15

#: The declared registry of consolidation groups. Adding a future, unrelated
#: correlated-event set means adding a new entry here (with its own event
#: types, template and message builder) — never widening this one group's
#: member set or copy-pasting this module.
CONSOLIDATION_GROUPS: dict[str, ConsolidationGroupSpec] = {
    "service_restoration": ConsolidationGroupSpec(
        member_event_types=frozenset(
            {
                EventType.payment_received,
                EventType.invoice_paid,
                EventType.subscription_resumed,
                EventType.ont_online,
            }
        ),
        closing_event_types=frozenset({EventType.ont_online}),
        template_code=RESTORATION_SUMMARY_TEMPLATE_CODE,
        category=RESTORATION_SUMMARY_CATEGORY,
        debounce_setting_key="notification_restoration_debounce_minutes",
        default_debounce_minutes=DEFAULT_DEBOUNCE_MINUTES,
        build_message=_build_restoration_message,
    ),
}

#: Reverse index built once at import time: which group (if any) owns a given
#: event type. Asserted disjoint below — no event type may belong to two
#: groups, which would make "which window does this fact go in" ambiguous.
_EVENT_TYPE_TO_GROUP: dict[EventType, str] = {}
for _group_id, _spec in CONSOLIDATION_GROUPS.items():
    for _event_type in _spec.member_event_types:
        if _event_type in _EVENT_TYPE_TO_GROUP:
            raise AssertionError(
                f"{_event_type} claimed by both {_EVENT_TYPE_TO_GROUP[_event_type]!r} "
                f"and {_group_id!r} consolidation groups"
            )
        _EVENT_TYPE_TO_GROUP[_event_type] = _group_id
del _group_id, _spec, _event_type


def consolidation_group_for(event_type: EventType) -> str | None:
    """The registered group id this event type belongs to, or ``None``.

    ``NotificationHandler`` calls this to decide whether an event routes
    through consolidation at all; a ``None`` result means "send directly, as
    before" for every event type not declared into a group.
    """

    return _EVENT_TYPE_TO_GROUP.get(event_type)


def _debounce_minutes(db: Session, spec: ConsolidationGroupSpec) -> int:
    value = resolve_value(db, SettingDomain.notification, spec.debounce_setting_key)
    try:
        return max(1, int(str(value)))
    except (TypeError, ValueError):
        return spec.default_debounce_minutes


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
    db: Session, subscriber_id: UUID, group: str
) -> SubscriberNotificationWindow | None:
    return (
        db.query(SubscriberNotificationWindow)
        .filter(SubscriberNotificationWindow.subscriber_id == subscriber_id)
        .filter(SubscriberNotificationWindow.group == group)
        .filter(SubscriberNotificationWindow.closed_at.is_(None))
        .with_for_update()
        .one_or_none()
    )


def _open_window(
    db: Session, subscriber_id: UUID, group: str, now: datetime
) -> SubscriberNotificationWindow:
    """Open a new window, tolerating a concurrent opener.

    The partial unique index (``subscriber_id, group`` WHERE ``closed_at IS
    NULL``) is the actual guarantee; a savepoint keeps a lost race from
    poisoning the caller's outer transaction (feature code never calls
    ``db.rollback()`` — see ``account_lifecycle.cancel_subscription``'s
    credit-note savepoint for the identical shape). Scoping the uniqueness to
    ``(subscriber_id, group)`` rather than just ``subscriber_id`` is what lets
    a subscriber have independent open windows in two different groups at
    once without one group's episode blocking another's.
    """

    spec = CONSOLIDATION_GROUPS[group]
    window = SubscriberNotificationWindow(
        subscriber_id=subscriber_id,
        group=group,
        opened_at=now,
        window_closes_at=now + timedelta(minutes=_debounce_minutes(db, spec)),
        collected_events=[],
    )
    try:
        with db.begin_nested():
            db.add(window)
            db.flush()
    except IntegrityError:
        existing = _get_open_window(db, subscriber_id, group)
        if existing is None:
            raise
        return existing
    return window


def record_consolidated_fact(
    db: Session, subscriber_id: UUID | None, event: Event
) -> None:
    """Coalesce one member event of a registered group into its window.

    Called from ``NotificationHandler`` INSTEAD OF a direct
    ``communication_intents.submit`` for any event type
    :func:`consolidation_group_for` resolves to a group. No-ops when there is
    no subscriber to notify. A closing event type (the group's declared
    ``closing_event_types``) closes and sends immediately; every other member
    event only appends its fact and lets the window run (or a later closing
    event, or the sweep on timeout) decide when to send.

    A late closing event arriving after its window already closed is a
    deliberate no-op: the timeout fallback already told the customer, and
    reopening a closed window would recreate the exact multi-email problem
    this module exists to remove.
    """

    group = consolidation_group_for(event.event_type)
    if group is None:
        logger.warning(
            "record_consolidated_fact called for %s, which is not a member of "
            "any registered consolidation group; ignoring",
            event.event_type.value,
        )
        return

    if subscriber_id is None:
        logger.debug(
            "Skipped consolidation-window fact for event %s: no subscriber",
            event.event_type.value,
        )
        return

    spec = CONSOLIDATION_GROUPS[group]
    is_closing = event.event_type in spec.closing_event_types

    window = _get_open_window(db, subscriber_id, group)
    if window is None:
        if is_closing:
            # A late completion signal with no open window — either this
            # subscriber's episode already timed out and was sent (the
            # window is closed, not missing), or there was never an episode
            # in progress. Either way, do not create a fresh window just to
            # immediately close it: that would recreate the exact
            # multi-email problem this module exists to remove.
            logger.info(
                "Ignored closing event %s for subscriber %s: no open %r "
                "window (already closed or never opened)",
                event.event_type.value,
                subscriber_id,
                group,
            )
            return
        window = _open_window(db, subscriber_id, group, datetime.now(UTC))

    window.collected_events = [*window.collected_events, _event_fact(event)]
    db.flush()

    if is_closing:
        close_and_send(db, window, close_reason=NotificationWindowCloseReason.completed)


def _send_consolidated_email(
    db: Session, window: SubscriberNotificationWindow
) -> CommunicationIntentResult:
    spec = CONSOLIDATION_GROUPS[window.group]
    subscriber = db.get(Subscriber, window.subscriber_id)
    subscriber_name = (subscriber.name if subscriber and subscriber.name else None) or (
        "Valued Customer"
    )
    subject, body = spec.build_message(window, subscriber_name)
    return submit(
        db,
        CommunicationIntent(
            subscriber_id=window.subscriber_id,
            event_type=spec.template_code,
            category=spec.category,
            template_code=spec.template_code,
            subject=subject,
            body=body,
            persist_policy_suppressions=False,
            # Dedupe on the WINDOW's own id, not any one event's — this is
            # what makes closing an already-closed window (a retried sweep, a
            # sweep racing a closing-event arrival) safe to call twice. See
            # ``communication_intents.submit``'s dedupe-key check.
            dedupe_key=f"consolidation-window:{window.id}",
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
    ``record_consolidated_fact``'s closing-event path, holding the window's
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
