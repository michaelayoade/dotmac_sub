"""Policy helpers for event-driven access enforcement."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services import settings_spec


def _setting_bool(db: Session, domain: SettingDomain, key: str, default: bool) -> bool:
    value = settings_spec.resolve_value(db, domain, key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def group_routing_enabled(db: Session) -> bool:
    return _setting_bool(db, SettingDomain.radius, "group_routing_enabled", False)


def refresh_sessions_on_profile_change_enabled(db: Session) -> bool:
    return _setting_bool(
        db, SettingDomain.radius, "refresh_sessions_on_profile_change", True
    )


def fup_action(db: Session, payload_action: object | None = None) -> str:
    action = payload_action or settings_spec.resolve_value(
        db, SettingDomain.usage, "fup_action"
    )
    if action == "reduce_speed":
        action = "throttle"
    action = str(action or "throttle").strip().lower()
    if action not in {"throttle", "suspend", "block", "none"}:
        return "throttle"
    return action


def fup_throttle_radius_profile_id(db: Session) -> str | None:
    value = settings_spec.resolve_value(
        db, SettingDomain.usage, "fup_throttle_radius_profile_id"
    )
    text = str(value or "").strip()
    return text or None


def auto_suspend_on_overdue_enabled(db: Session) -> bool:
    return _setting_bool(db, SettingDomain.billing, "auto_suspend_on_overdue", False)


def suspension_grace_hours(db: Session) -> int:
    value = settings_spec.resolve_value(
        db, SettingDomain.billing, "suspension_grace_hours"
    )
    try:
        return int(str(value or 48))
    except (TypeError, ValueError):
        return 48
