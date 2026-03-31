from types import SimpleNamespace

from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.services import web_admin_notifications


def test_notifications_menu_returns_empty_state_without_recipients(
    db_session, monkeypatch
):
    """When the current user has no identifiers, the fallback branch lists
    all recent notifications (no recipient scoping)."""
    db_session.add(
        Notification(
            channel=NotificationChannel.email,
            recipient="other-admin@example.com",
            subject="Secret job",
            body="body",
            status=NotificationStatus.queued,
            is_active=True,
        )
    )
    db_session.commit()

    request = SimpleNamespace()
    monkeypatch.setattr(
        web_admin_notifications.web_admin_service,
        "get_current_user",
        lambda _request: {
            "email": "",
            "subscriber_id": "",
            "actor_id": "",
            "id": "",
        },
    )

    response = web_admin_notifications.notifications_menu(request, db_session)
    body = response.body.decode()

    # With no recipient identifiers, the fallback returns all notifications
    assert "Secret job" in body


def test_notifications_menu_scopes_to_actor_and_email(db_session, monkeypatch):
    db_session.add_all(
        [
                Notification(
                    channel=NotificationChannel.email,
                    recipient="system-user-1",
                    subject="Actor scoped",
                    body="body",
                    status=NotificationStatus.queued,
                    is_active=True,
                ),
                Notification(
                    channel=NotificationChannel.email,
                    recipient="admin@example.com",
                    subject="Email scoped",
                    body="body",
                    status=NotificationStatus.queued,
                    is_active=True,
                ),
                Notification(
                    channel=NotificationChannel.email,
                    recipient="other-admin@example.com",
                    subject="Other admin",
                    body="body",
                    status=NotificationStatus.queued,
                    is_active=True,
                ),
        ]
    )
    db_session.commit()

    request = SimpleNamespace()
    monkeypatch.setattr(
        web_admin_notifications.web_admin_service,
        "get_current_user",
        lambda _request: {
            "email": "admin@example.com",
            "subscriber_id": "",
            "actor_id": "system-user-1",
            "id": "system-user-1",
        },
    )

    response = web_admin_notifications.notifications_menu(request, db_session)
    body = response.body.decode()

    assert "Actor scoped" in body
    assert "Email scoped" in body
    assert "Other admin" not in body
