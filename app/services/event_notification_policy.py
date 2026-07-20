"""Policy helpers for event-driven customer notifications."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.services.events.types import Event, EventType
from app.services.settings_spec import resolve_value

_ENABLED_VALUES = {"1", "true", "yes", "on", "enabled"}

BALANCE_NOTIFICATION_EVENTS: set[EventType] = {
    EventType.invoice_created,
    EventType.invoice_sent,
    EventType.invoice_overdue,
    EventType.subscription_suspension_warning,
    EventType.arrangement_defaulted,
}

BILLING_SUSPENSION_REASONS = {"overdue", "dunning", "invoice_overdue"}


def _notification_setting_value(db: Session, key: str) -> str | None:
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.notification)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not setting:
        return None
    if setting.value_text is not None:
        return str(setting.value_text)
    if setting.value_json is not None:
        return str(setting.value_json)
    return None


def event_notifications_enabled(db: Session, template_code: str) -> bool:
    value = _notification_setting_value(
        db, f"notification_event_{template_code}_enabled"
    )
    if value is None:
        return True
    return value.strip().lower() in _ENABLED_VALUES


def customer_balance_notifications_suppressed(db: Session, event: Event) -> bool:
    enabled = resolve_value(
        db, SettingDomain.billing, "customer_balance_notifications_enabled"
    )
    if enabled is not False and str(enabled).lower() not in {
        "0",
        "false",
        "no",
        "off",
        "",
    }:
        return False

    if event.event_type in BALANCE_NOTIFICATION_EVENTS:
        return True
    return is_billing_suspension_event(event)


def is_billing_suspension_event(event: Event) -> bool:
    if event.event_type != EventType.subscription_suspended:
        return False

    reason = str(event.payload.get("reason") or "").strip().lower()
    source = str(event.payload.get("source") or "").strip().lower()
    return (
        reason in BILLING_SUSPENSION_REASONS
        or source in BILLING_SUSPENSION_REASONS
        or source.startswith("invoice:")
    )
