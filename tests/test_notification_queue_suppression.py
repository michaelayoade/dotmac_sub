"""The gate must fire in the real delivery path, not just in isolation.

`_deliver_notification_queue` is the only place all four transports are called,
which is why the consent check lives there. A unit test of
`communication_eligibility` proves the *rule*; these prove the rule is actually
*wired in* -- that a suppressed address does not reach `send_email`, and that a
billing notification to the same address still does.
"""

from __future__ import annotations

from unittest.mock import patch

from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
    SuppressionScope,
)
from app.services import communication_eligibility as eligibility
from app.tasks.notifications import _deliver_notification_queue_stats


def _queued(*, category: str, recipient: str = "cust@example.com") -> Notification:
    return Notification(
        channel=NotificationChannel.email,
        recipient=recipient,
        subject="Hello",
        body="Body",
        category=category,
        status=NotificationStatus.queued,
        is_active=True,
    )


def test_a_marketing_send_to_an_unsubscribed_address_never_reaches_the_transport(
    db_session,
):
    eligibility.suppress_committed(
        db_session, channel=NotificationChannel.email, address="cust@example.com"
    )
    note = _queued(category="marketing")
    db_session.add(note)
    db_session.commit()

    sent = []
    with patch(
        "app.tasks.notifications.email_service.send_email",
        side_effect=lambda **kw: sent.append(kw) or True,
    ):
        stats = _deliver_notification_queue_stats(db_session, batch_size=10)

    db_session.refresh(note)
    assert sent == [], "a suppressed address must not reach the transport"
    assert note.status == NotificationStatus.canceled
    assert note.last_error == "suppressed"
    assert stats["suppressed"] == 1
    assert stats["delivered"] == 0


def test_the_same_unsubscribed_address_STILL_gets_its_invoice(db_session):
    """The one that matters. An unsubscribe must not stop billing."""
    eligibility.suppress_committed(
        db_session,
        channel=NotificationChannel.email,
        address="cust@example.com",
        scope=SuppressionScope.marketing,
    )
    invoice = _queued(category="billing")
    db_session.add(invoice)
    db_session.commit()

    sent = []
    with patch(
        "app.tasks.notifications.email_service.send_email",
        side_effect=lambda **kw: sent.append(kw) or True,
    ):
        stats = _deliver_notification_queue_stats(db_session, batch_size=10)

    db_session.refresh(invoice)
    assert len(sent) == 1, "a marketing unsubscribe must not stop the invoice"
    assert sent[0]["to_email"] == "cust@example.com"
    assert invoice.status == NotificationStatus.delivered
    assert stats["delivered"] == 1
    assert stats["suppressed"] == 0


def test_a_hard_bounce_stops_even_the_invoice(db_session):
    eligibility.suppress_committed(
        db_session,
        channel=NotificationChannel.email,
        address="dead@example.com",
        scope=SuppressionScope.all,
    )
    invoice = _queued(category="billing", recipient="dead@example.com")
    db_session.add(invoice)
    db_session.commit()

    sent = []
    with patch(
        "app.tasks.notifications.email_service.send_email",
        side_effect=lambda **kw: sent.append(kw) or True,
    ):
        stats = _deliver_notification_queue_stats(db_session, batch_size=10)

    db_session.refresh(invoice)
    assert sent == []
    assert invoice.status == NotificationStatus.canceled
    assert stats["suppressed"] == 1


def test_an_unsuppressed_address_is_unaffected(db_session):
    note = _queued(category="marketing", recipient="fine@example.com")
    db_session.add(note)
    db_session.commit()

    sent = []
    with patch(
        "app.tasks.notifications.email_service.send_email",
        side_effect=lambda **kw: sent.append(kw) or True,
    ):
        stats = _deliver_notification_queue_stats(db_session, batch_size=10)

    db_session.refresh(note)
    assert len(sent) == 1
    assert note.status == NotificationStatus.delivered
    assert stats["suppressed"] == 0
