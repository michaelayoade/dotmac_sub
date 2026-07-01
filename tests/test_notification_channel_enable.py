"""The event→notification fan-out must not create rows for a channel that is
explicitly disabled in config (e.g. SMS with ``sms_enabled=false`` / no
provider). Such rows can only fail at dispatch and accumulate as
``send_failed`` — the failed-SMS backlog this guards against.
"""

from __future__ import annotations

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.notification import Notification, NotificationChannel
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.subscription_engine import SettingValueType
from app.services.events.handlers.notification import (
    NotificationHandler,
    _channel_disabled_in_config,
)
from app.services.events.types import Event, EventType


def _set_sms_enabled(db, value: str) -> None:
    row = (
        db.query(DomainSetting)
        .filter_by(domain=SettingDomain.notification, key="sms_enabled")
        .one_or_none()
    )
    if row is None:
        db.add(
            DomainSetting(
                domain=SettingDomain.notification,
                key="sms_enabled",
                value_type=SettingValueType.boolean,
                value_text=value,
                is_active=True,
            )
        )
    else:
        row.value_text = value
        row.is_active = True
    db.commit()


def _subscriber(db) -> Subscriber:
    sub = Subscriber(
        first_name="Chan",
        last_name="Gate",
        email="chan-gate@example.com",
        phone="+2348012345678",
        status=SubscriberStatus.active,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _handle(db, sub_id) -> None:
    # subscription_suspended fans out to (email, sms); active+service is allowed.
    NotificationHandler().handle(
        db,
        Event(
            event_type=EventType.subscription_suspended,
            payload={},
            account_id=sub_id,
        ),
    )
    # handler db.add()s without flushing; test session has autoflush off.
    db.flush()


def test_channel_disabled_helper(db_session):
    # Email has no enable flag -> never suppressed at creation.
    assert _channel_disabled_in_config(db_session, NotificationChannel.email) is False
    _set_sms_enabled(db_session, "false")
    assert _channel_disabled_in_config(db_session, NotificationChannel.sms) is True
    _set_sms_enabled(db_session, "true")
    assert _channel_disabled_in_config(db_session, NotificationChannel.sms) is False


def test_sms_row_created_when_enabled_skipped_when_disabled(db_session):
    sub = _subscriber(db_session)

    def sms_count() -> int:
        return (
            db_session.query(Notification)
            .filter_by(subscriber_id=sub.id, channel=NotificationChannel.sms)
            .count()
        )

    # Enabled: the sms channel produces a row (recipient resolves from phone).
    _set_sms_enabled(db_session, "true")
    _handle(db_session, sub.id)
    enabled_count = sms_count()
    assert enabled_count >= 1, "SMS row expected when sms_enabled=true"

    # Disabled: same event, same subscriber/phone -> NO new sms row. Only the
    # enable flag differs, so a zero delta proves the config guard (not a
    # missing recipient) is what suppresses the channel.
    _set_sms_enabled(db_session, "false")
    _handle(db_session, sub.id)
    assert sms_count() == enabled_count, "No new SMS row when sms_enabled=false"
