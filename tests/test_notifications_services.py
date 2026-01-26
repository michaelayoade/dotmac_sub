from app.models.notification import NotificationChannel, NotificationStatus
from app.schemas.notification import (
    NotificationCreate,
    NotificationTemplateCreate,
)
from app.services import notification as notification_service


def test_notification_template_and_delivery(db_session):
    template = notification_service.templates.create(
        db_session,
        NotificationTemplateCreate(
            name="Welcome",
            code="welcome_email",
            channel=NotificationChannel.email,
            subject="Hello",
            body="Welcome!",
        ),
    )
    notification = notification_service.notifications.create(
        db_session,
        NotificationCreate(
            template_id=template.id,
            channel=NotificationChannel.email,
            recipient="user@example.com",
            status=NotificationStatus.queued,
            payload={"name": "User"},
        ),
    )
    items = notification_service.notifications.list(
        db_session,
        channel=NotificationChannel.email.value,
        status=NotificationStatus.queued.value,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert len(items) == 1
    assert items[0].id == notification.id
