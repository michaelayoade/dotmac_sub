"""Helpers for resolving billing settings with legacy fallbacks."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain


def _coerce_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _setting_value(db: Session, key: str) -> object | None:
    stmt = (
        select(DomainSetting)
        .where(DomainSetting.domain == SettingDomain.billing)
        .where(DomainSetting.key == key)
        .where(DomainSetting.is_active.is_(True))
    )
    setting = db.scalars(stmt).first()
    if not setting:
        return None
    return setting.value_json if setting.value_json is not None else setting.value_text


def resolve_payment_due_days(
    db: Session,
    default: int = 14,
    subscriber: object | None = None,
) -> int:
    """Resolve payment due days: subscriber override > global setting > legacy keys.

    Args:
        db: Database session.
        default: Fallback if no setting is found.
        subscriber: Optional subscriber — if they have ``payment_due_days``
            set, that value takes priority over the global setting.
    """
    # Subscriber-level override takes priority
    sub_due_days = getattr(subscriber, "payment_due_days", None)
    if sub_due_days is not None:
        return max(_coerce_int(sub_due_days, default), 0)

    canonical = _setting_value(db, "payment_due_days")
    if canonical is not None:
        return max(_coerce_int(canonical, default), 0)

    legacy_invoice = _setting_value(db, "invoice_due_days")
    if legacy_invoice is not None:
        return max(_coerce_int(legacy_invoice, default), 0)

    legacy_terms = _setting_value(db, "default_payment_terms_days")
    if legacy_terms is not None:
        return max(_coerce_int(legacy_terms, default), 0)

    return default
