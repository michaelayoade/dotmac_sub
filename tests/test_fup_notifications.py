"""Customer FUP notifications emitted by the periodic FUP evaluation task."""

from app.models.notification import Notification, NotificationChannel
from app.tasks.usage import _build_fup_notification, _emit_fup_notifications


def test_build_fup_notification_messages():
    subj, body = _build_fup_notification("approaching", "Monthly 100GB cap", 100, 85)
    assert subj == "Approaching your data limit"
    assert "85%" in body and "Monthly 100GB cap" in body

    subj, body = _build_fup_notification("throttled", "Monthly 100GB cap", 100, 105)
    assert subj == "Speed reduced"
    assert "top up" in body.lower()

    subj, body = _build_fup_notification("blocked", None, None, None)
    assert subj == "Service paused"
    # Falls back gracefully with no rule name / threshold.
    assert "your plan" in body


def test_emit_throttled_creates_push_and_email(db_session, subscriber):
    subscriber.email = "fup.customer@example.com"
    db_session.commit()

    sent = _emit_fup_notifications(
        db_session,
        [
            {
                "subscriber_id": subscriber.id,
                "kind": "throttled",
                "rule_name": "Monthly 100GB cap",
                "threshold_gb": 100,
                "used_gb": 120,
            }
        ],
    )
    assert sent == 1

    notes = (
        db_session.query(Notification)
        .filter(Notification.subscriber_id == subscriber.id)
        .filter(Notification.event_type == "fup_throttled")
        .all()
    )
    # Enforcement notifications go out on both push and email.
    channels = {n.channel for n in notes}
    assert channels == {NotificationChannel.push, NotificationChannel.email}
    assert all(n.category == "fup" and n.subject == "Speed reduced" for n in notes)


def test_emit_approaching_is_push_only(db_session, subscriber):
    subscriber.email = "fup.customer2@example.com"
    db_session.commit()

    _emit_fup_notifications(
        db_session,
        [
            {
                "subscriber_id": subscriber.id,
                "kind": "approaching",
                "rule_name": "Monthly 100GB cap",
                "threshold_gb": 100,
                "used_gb": 85,
            }
        ],
    )
    notes = (
        db_session.query(Notification)
        .filter(Notification.subscriber_id == subscriber.id)
        .filter(Notification.event_type == "fup_approaching")
        .all()
    )
    assert [n.channel for n in notes] == [NotificationChannel.push]


def test_emit_fup_notifications_skips_unknown_subscriber(db_session):
    import uuid

    # No subscriber row → no recipient to deliver to → skipped, not raised.
    sent = _emit_fup_notifications(
        db_session, [{"subscriber_id": uuid.uuid4(), "kind": "blocked"}]
    )
    assert sent == 0


def test_emit_fup_notifications_empty_is_noop(db_session):
    assert _emit_fup_notifications(db_session, []) == 0
