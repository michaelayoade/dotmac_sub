"""Per-channel reclaim policy for stuck-'sending' notifications (#48a).

A notification stuck in 'sending' past the timeout means the worker likely
crashed mid-send — possibly AFTER the provider was already called. Without a
provider idempotency key, the policy is content-driven: noisy/bulk is
at-most-once (don't re-send a possible duplicate), everything else is
at-least-once (re-send, bounded by MAX_RETRIES — a duplicate beats losing a
billing/auth notice).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.tasks.notifications import (
    _deliver_notification_queue_stats,
    _reclaim_policy,
)


def _stuck_sending(
    *,
    recipient: str,
    category: str | None = None,
    event_type: str | None = None,
    retry_count: int = 0,
    age_min: int = 15,
) -> Notification:
    old = datetime.now(UTC) - timedelta(minutes=age_min)
    return Notification(
        channel=NotificationChannel.sms,
        recipient=recipient,
        subject="T",
        body="body",
        status=NotificationStatus.sending,
        is_active=True,
        category=category,
        event_type=event_type,
        retry_count=retry_count,
        created_at=old,
        updated_at=old,
    )


def test_reclaim_policy_classification():
    assert _reclaim_policy(Notification(category="general")) == "at_most_once"
    assert (
        _reclaim_policy(Notification(event_type="service_bulk_message"))
        == "at_most_once"
    )
    assert _reclaim_policy(Notification(category="billing")) == "at_least_once"
    assert _reclaim_policy(Notification(category="service")) == "at_least_once"
    # Untyped/legacy defaults to at-least-once (never silently lose one).
    assert _reclaim_policy(Notification()) == "at_least_once"


def test_at_most_once_stuck_is_not_resent(db_session, monkeypatch):
    sent: list = []
    monkeypatch.setattr(
        "app.tasks.notifications.sms_service.send_sms",
        lambda **k: sent.append(k) or True,
    )
    n = _stuck_sending(recipient="+2348000000001", category="general")
    db_session.add(n)
    db_session.commit()

    stats = _deliver_notification_queue_stats(db_session, batch_size=10)

    db_session.refresh(n)
    assert n.status == NotificationStatus.failed
    assert "at-most-once" in (n.last_error or "")
    assert sent == []  # provider NOT called — no duplicate blast
    assert stats["stuck_dropped"] == 1


def test_at_least_once_stuck_is_resent(db_session, monkeypatch):
    sent: list = []
    monkeypatch.setattr(
        "app.tasks.notifications.sms_service.send_sms",
        lambda **k: sent.append(k) or True,
    )
    n = _stuck_sending(recipient="+2348000000002", category="billing")
    db_session.add(n)
    db_session.commit()

    stats = _deliver_notification_queue_stats(db_session, batch_size=10)

    db_session.refresh(n)
    assert n.status == NotificationStatus.delivered
    assert len(sent) == 1  # re-sent (accepting a rare duplicate)
    assert stats["reclaimed"] == 1


def test_at_least_once_stuck_gives_up_after_max(db_session, monkeypatch):
    sent: list = []
    monkeypatch.setattr(
        "app.tasks.notifications.sms_service.send_sms",
        lambda **k: sent.append(k) or True,
    )
    # retry_count already at MAX → reclaim bumps it past MAX → give up, no send.
    n = _stuck_sending(recipient="+2348000000003", category="billing", retry_count=3)
    db_session.add(n)
    db_session.commit()

    stats = _deliver_notification_queue_stats(db_session, batch_size=10)

    db_session.refresh(n)
    assert n.status == NotificationStatus.failed
    assert n.last_error == "stuck_sending_reclaim_exhausted"
    assert sent == []  # not re-sent
    assert stats["failed"] == 1
