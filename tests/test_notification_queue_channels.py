from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.tasks.notifications import _deliver_notification_queue


def _queued_notification(
    *, channel: NotificationChannel, recipient: str, body: str
) -> Notification:
    return Notification(
        channel=channel,
        recipient=recipient,
        subject="Test",
        body=body,
        status=NotificationStatus.queued,
        is_active=True,
    )


def _set_notification_setting(db, key: str, value: str) -> None:
    db.add(
        DomainSetting(
            domain=SettingDomain.notification,
            key=key,
            value_text=value,
            is_active=True,
        )
    )
    db.commit()


def test_deliver_notification_queue_handles_sms_and_whatsapp(db_session, monkeypatch):
    sms = _queued_notification(
        channel=NotificationChannel.sms,
        recipient="+2348000000001",
        body="SMS body",
    )
    wa = _queued_notification(
        channel=NotificationChannel.whatsapp,
        recipient="+2348000000002",
        body="WA body",
    )
    db_session.add_all([sms, wa])
    db_session.commit()

    monkeypatch.setattr(
        "app.tasks.notifications.sms_service.send_sms", lambda **_: True
    )
    monkeypatch.setattr(
        "app.tasks.notifications.whatsapp_service.send_text_message",
        lambda **_: {"ok": True, "sent": True},
    )

    delivered = _deliver_notification_queue(db_session, batch_size=10)

    db_session.refresh(sms)
    db_session.refresh(wa)
    assert delivered == 2
    assert sms.status == NotificationStatus.delivered
    assert wa.status == NotificationStatus.delivered


def test_deliver_notification_queue_marks_failed_on_whatsapp_error(
    db_session, monkeypatch
):
    wa = _queued_notification(
        channel=NotificationChannel.whatsapp,
        recipient="+2348000000002",
        body="WA body",
    )
    db_session.add(wa)
    db_session.commit()

    monkeypatch.setattr(
        "app.tasks.notifications.whatsapp_service.send_text_message",
        lambda **_: {"ok": False, "response": "provider down"},
    )

    delivered = _deliver_notification_queue(db_session, batch_size=10)

    db_session.refresh(wa)
    assert delivered == 0
    assert wa.status == NotificationStatus.failed
    assert wa.last_error == "provider down"


def test_deliver_notification_queue_brands_plain_text_email(db_session, monkeypatch):
    email = _queued_notification(
        channel=NotificationChannel.email,
        recipient="cust@example.com",
        body="Dear Customer,\n\nYour invoice is overdue.\nPlease pay soon.",
    )
    db_session.add(email)
    db_session.commit()

    captured: dict = {}

    def _fake_send_email(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(
        "app.tasks.notifications.email_service.send_email", _fake_send_email
    )

    delivered = _deliver_notification_queue(db_session, batch_size=10)

    db_session.refresh(email)
    assert delivered == 1
    assert email.status == NotificationStatus.delivered
    # Plain text is wrapped in the branded template with a text/plain part.
    assert "<!DOCTYPE html>" in captured["body_html"]
    assert "#FF0000" in captured["body_html"]
    assert "#008000" in captured["body_html"]
    assert "/static/branding/favicon/icon-192.png" in captured["body_html"]
    assert "static/illustrations/email-header.png" not in captured["body_html"]
    assert "Your invoice is overdue.<br>Please pay soon." in captured["body_html"]
    assert captured["body_text"] == email.body


def test_deliver_notification_queue_sends_html_email_with_text_part(
    db_session, monkeypatch
):
    email = _queued_notification(
        channel=NotificationChannel.email,
        recipient="cust@example.com",
        body="<p>Your <strong>invoice</strong> is ready.</p>",
    )
    db_session.add(email)
    db_session.commit()

    captured: dict = {}

    def _fake_send_email(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(
        "app.tasks.notifications.email_service.send_email", _fake_send_email
    )

    delivered = _deliver_notification_queue(db_session, batch_size=10)

    db_session.refresh(email)
    assert delivered == 1
    assert email.status == NotificationStatus.delivered
    assert "<p>Your <strong>invoice</strong> is ready.</p>" in captured["body_html"]
    assert captured["body_text"] == "Your invoice is ready."


def test_deliver_notification_queue_processes_push_channel(db_session, monkeypatch):
    push = _queued_notification(
        channel=NotificationChannel.push,
        recipient="subscriber",
        body="Usage alert",
    )
    db_session.add(push)
    db_session.commit()

    monkeypatch.setattr(
        "app.tasks.notifications.push_service.send_push", lambda **_: True
    )

    delivered = _deliver_notification_queue(db_session, batch_size=10)

    db_session.refresh(push)
    assert delivered == 1
    assert push.status == NotificationStatus.delivered


def test_deliver_notification_queue_expires_stale_notifications(
    db_session, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from app.tasks.notifications import _deliver_notification_queue_stats

    stale = _queued_notification(
        channel=NotificationChannel.email,
        recipient="stale@example.com",
        body="Old dunning notice",
    )
    fresh = _queued_notification(
        channel=NotificationChannel.email,
        recipient="fresh@example.com",
        body="New notice",
    )
    db_session.add_all([stale, fresh])
    db_session.commit()
    # Backdate past the default 72h cutoff.
    stale.created_at = datetime.now(UTC) - timedelta(hours=100)
    db_session.commit()

    monkeypatch.setattr(
        "app.tasks.notifications.email_service.send_email", lambda **_: True
    )

    stats = _deliver_notification_queue_stats(db_session, batch_size=10)

    db_session.refresh(stale)
    db_session.refresh(fresh)
    assert stats["expired"] == 1
    assert stats["delivered"] == 1
    assert stale.status == NotificationStatus.canceled
    assert stale.last_error == "expired_in_queue"
    assert fresh.status == NotificationStatus.delivered


def test_deliver_notification_queue_applies_per_channel_rate_limit(
    db_session, monkeypatch
):
    _set_notification_setting(db_session, "notification_per_channel_rate_limit", "1")
    first = _queued_notification(
        channel=NotificationChannel.sms,
        recipient="+2348000000001",
        body="SMS one",
    )
    second = _queued_notification(
        channel=NotificationChannel.sms,
        recipient="+2348000000002",
        body="SMS two",
    )
    db_session.add_all([first, second])
    db_session.commit()

    monkeypatch.setattr(
        "app.tasks.notifications.sms_service.send_sms", lambda **_: True
    )

    from app.tasks.notifications import _deliver_notification_queue_stats

    stats = _deliver_notification_queue_stats(db_session, batch_size=10)

    db_session.refresh(first)
    db_session.refresh(second)
    assert stats["delivered"] == 1
    assert stats["rate_limited"] == 1
    assert first.status == NotificationStatus.delivered
    assert second.status == NotificationStatus.queued


def test_deliver_notification_queue_schedules_failed_retry_with_backoff(
    db_session, monkeypatch
):
    _set_notification_setting(db_session, "notification_max_retries", "3")
    _set_notification_setting(db_session, "notification_retry_backoff_minutes", "7")
    sms = _queued_notification(
        channel=NotificationChannel.sms,
        recipient="+2348000000001",
        body="SMS body",
    )
    db_session.add(sms)
    db_session.commit()

    monkeypatch.setattr(
        "app.tasks.notifications.sms_service.send_sms", lambda **_: False
    )

    from app.tasks.notifications import _deliver_notification_queue_stats

    before_run = datetime.now(UTC)
    stats = _deliver_notification_queue_stats(db_session, batch_size=10)

    db_session.refresh(sms)
    assert stats["retried"] == 1
    assert sms.status == NotificationStatus.failed
    assert sms.retry_count == 1
    assert sms.send_at is not None
    assert sms.send_at.replace(tzinfo=UTC) >= before_run + timedelta(minutes=6)
