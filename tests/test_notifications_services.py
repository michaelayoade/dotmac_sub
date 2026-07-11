from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.subscription_engine import SettingValueType
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


def _subscriber(db_session, *, status=SubscriberStatus.active, suffix="manual"):
    sub = Subscriber(
        first_name="Manual",
        last_name="Notify",
        email=f"manual-notify-{suffix}@example.com",
        phone="+2348012345678",
        status=status,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def _setting(db_session, key: str, value: str, value_type=SettingValueType.string):
    row = (
        db_session.query(DomainSetting)
        .filter_by(domain=SettingDomain.notification, key=key)
        .one_or_none()
    )
    if row is None:
        row = DomainSetting(
            domain=SettingDomain.notification,
            key=key,
            value_type=value_type,
            value_text=value,
            is_active=True,
        )
        db_session.add(row)
    else:
        row.value_type = value_type
        row.value_text = value
        row.value_json = None
        row.is_active = True
    db_session.commit()


def test_manual_notification_create_uses_status_gate(db_session):
    subscriber = _subscriber(
        db_session, status=SubscriberStatus.canceled, suffix="canceled"
    )

    notification = notification_service.notifications.create(
        db_session,
        NotificationCreate(
            subscriber_id=subscriber.id,
            channel=NotificationChannel.email,
            recipient=subscriber.email,
            subject="Usage update",
            body="Usage update",
            category="usage",
            event_type="manual_usage_notice",
        ),
    )

    assert notification.status == NotificationStatus.canceled
    assert notification.last_error == "Suppressed by account notification status policy"


def test_manual_notification_create_uses_channel_gate(db_session):
    subscriber = _subscriber(db_session, suffix="sms-disabled")
    _setting(
        db_session,
        "sms_enabled",
        "false",
        value_type=SettingValueType.boolean,
    )

    notification = notification_service.notifications.create(
        db_session,
        NotificationCreate(
            subscriber_id=subscriber.id,
            channel=NotificationChannel.sms,
            recipient=subscriber.phone,
            subject="Service update",
            body="Service update",
            category="service",
            event_type="manual_service_notice",
        ),
    )

    assert notification.status == NotificationStatus.canceled
    assert notification.last_error == "Suppressed by notification channel configuration"


def test_manual_notification_create_applies_quiet_hours_and_dedupe(db_session):
    subscriber = _subscriber(db_session, suffix="dedupe")
    _setting(
        db_session,
        "notification_quiet_hours_enabled",
        "true",
        value_type=SettingValueType.boolean,
    )
    _setting(db_session, "notification_quiet_hours_start", "00:00")
    _setting(db_session, "notification_quiet_hours_end", "23:59")
    _setting(
        db_session,
        "notification_dedupe_window_minutes",
        "60",
        value_type=SettingValueType.integer,
    )

    first = notification_service.notifications.create(
        db_session,
        NotificationCreate(
            subscriber_id=subscriber.id,
            channel=NotificationChannel.email,
            recipient=subscriber.email,
            subject="Service update",
            body="Service update",
            category="service",
            event_type="manual_service_notice",
        ),
    )
    second = notification_service.notifications.create(
        db_session,
        NotificationCreate(
            subscriber_id=subscriber.id,
            channel=NotificationChannel.email,
            recipient=subscriber.email,
            subject="Service update",
            body="Service update",
            category="service",
            event_type="manual_service_notice",
        ),
    )

    assert first.status == NotificationStatus.queued
    assert first.send_at is not None
    assert second.status == NotificationStatus.canceled
    assert second.last_error == "Suppressed duplicate customer notification"


def test_event_notification_drops_policy_suppression(db_session):
    subscriber = _subscriber(
        db_session, status=SubscriberStatus.canceled, suffix="event-drop"
    )
    before = db_session.query(Notification).count()

    notification = notification_service.notifications.queue_event_notification(
        db_session,
        NotificationCreate(
            subscriber_id=subscriber.id,
            channel=NotificationChannel.email,
            recipient=subscriber.email,
            subject="Usage update",
            body="Usage update",
            category="usage",
            event_type="usage_warning",
        ),
    )

    assert notification is None
    assert db_session.query(Notification).count() == before


def test_internal_notification_bypasses_customer_identity_and_gates(db_session):
    subscriber = _subscriber(
        db_session, status=SubscriberStatus.canceled, suffix="internal-bypass"
    )
    _setting(
        db_session,
        "sms_enabled",
        "false",
        value_type=SettingValueType.boolean,
    )

    notification = notification_service.notifications.queue_internal_notification(
        db_session,
        NotificationCreate(
            channel=NotificationChannel.sms,
            recipient=subscriber.phone,
            subject="Internal note",
            body="Internal note",
        ),
    )

    assert notification.subscriber_id is None
    assert notification.status == NotificationStatus.queued
    assert notification.last_error is None


def test_record_transport_attempt_creates_sending_row(db_session):
    notification = notification_service.notifications.record_transport_attempt(
        db_session,
        channel=NotificationChannel.email,
        recipient="transport@example.com",
        subject="Sending",
        body="Body",
    )

    assert notification.status == NotificationStatus.sending
    assert notification.recipient == "transport@example.com"

    updated = notification_service.notifications.record_transport_attempt(
        db_session,
        notification_id=notification.id,
        channel=NotificationChannel.sms,
        recipient="+2348011112222",
        body="SMS",
    )

    assert updated.id == notification.id
    assert updated.channel == NotificationChannel.sms
    assert updated.status == NotificationStatus.sending
    assert updated.last_error is None
