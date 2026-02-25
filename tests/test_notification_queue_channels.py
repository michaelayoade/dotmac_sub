from __future__ import annotations

from app.models.notification import Notification, NotificationChannel, NotificationStatus
from app.tasks.notifications import _deliver_notification_queue


def _queued_notification(*, channel: NotificationChannel, recipient: str, body: str) -> Notification:
    return Notification(
        channel=channel,
        recipient=recipient,
        subject="Test",
        body=body,
        status=NotificationStatus.queued,
        is_active=True,
    )


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

    monkeypatch.setattr("app.tasks.notifications.sms_service.send_sms", lambda **_: True)
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


def test_deliver_notification_queue_marks_failed_on_whatsapp_error(db_session, monkeypatch):
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
