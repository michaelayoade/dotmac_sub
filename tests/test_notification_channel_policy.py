from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationTemplate,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.subscription_engine import SettingValueType
from app.services.events.handlers.notification import NotificationHandler
from app.services.events.types import Event, EventType
from app.services.notification_channel_policy import resolve_notification_channels
from app.services.settings_cache import SettingsCache


def _setting(db_session, key: str, *, value_text=None, value_json=None) -> None:
    row = (
        db_session.query(DomainSetting)
        .filter_by(domain=SettingDomain.notification, key=key)
        .one_or_none()
    )
    if row is None:
        row = DomainSetting(
            domain=SettingDomain.notification,
            key=key,
            value_type=SettingValueType.json
            if value_json is not None
            else SettingValueType.string,
            value_text=value_text,
            value_json=value_json,
            is_active=True,
        )
        db_session.add(row)
    else:
        row.value_type = (
            SettingValueType.json if value_json is not None else SettingValueType.string
        )
        row.value_text = value_text
        row.value_json = value_json
        row.is_active = True
    db_session.commit()
    SettingsCache.invalidate(SettingDomain.notification.value, key)


def test_channel_policy_falls_back_to_caller_defaults(db_session):
    channels = resolve_notification_channels(
        db_session,
        template_code="subscription_activated",
        category="service",
        default_channels=(NotificationChannel.email, NotificationChannel.sms),
    )

    assert channels == (NotificationChannel.email, NotificationChannel.sms)


def test_channel_policy_uses_category_policy(db_session):
    _setting(
        db_session,
        "notification_channel_policy",
        value_json={
            "categories": {
                "service": ["whatsapp", "push", "email", "email"],
            }
        },
    )

    channels = resolve_notification_channels(
        db_session,
        template_code="subscription_activated",
        category="service",
        default_channels=(NotificationChannel.email,),
    )

    assert channels == (
        NotificationChannel.whatsapp,
        NotificationChannel.push,
        NotificationChannel.email,
    )


def test_legacy_event_channels_take_precedence_over_policy(db_session):
    _setting(
        db_session,
        "notification_channel_policy",
        value_json={"categories": {"billing": ["whatsapp"]}},
    )
    _setting(
        db_session,
        "notification_event_invoice_overdue_channels",
        value_text="sms,email",
    )

    channels = resolve_notification_channels(
        db_session,
        template_code="invoice_overdue",
        category="billing",
        default_channels=(NotificationChannel.email,),
    )

    assert channels == (NotificationChannel.sms, NotificationChannel.email)


def test_event_handler_uses_category_channel_policy(db_session):
    subscriber = Subscriber(
        first_name="Policy",
        last_name="Channels",
        email="policy-channels@example.com",
        phone="+2348012348888",
        status=SubscriberStatus.active,
    )
    db_session.add(subscriber)
    db_session.add_all(
        [
            NotificationTemplate(
                code="subscription_suspended",
                name="Suspended WhatsApp",
                channel=NotificationChannel.whatsapp,
                subject=None,
                body="WhatsApp suspended",
                is_active=True,
            ),
            NotificationTemplate(
                code="subscription_suspended",
                name="Suspended Push",
                channel=NotificationChannel.push,
                subject="Push suspended",
                body="Push suspended",
                is_active=True,
            ),
        ]
    )
    db_session.commit()
    _setting(
        db_session,
        "notification_channel_policy",
        value_json={"categories": {"service": ["whatsapp", "push"]}},
    )

    NotificationHandler().handle(
        db_session,
        Event(
            event_type=EventType.subscription_suspended,
            payload={},
            account_id=subscriber.id,
        ),
    )
    db_session.commit()

    notifications = db_session.query(Notification).all()
    assert {row.channel for row in notifications} == {
        NotificationChannel.whatsapp,
        NotificationChannel.push,
    }
    assert {row.recipient for row in notifications} == {
        subscriber.phone,
        subscriber.email,
    }
