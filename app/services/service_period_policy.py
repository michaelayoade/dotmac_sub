"""Typed readers for prepaid-period purchase and outage-compensation policy."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services.settings_spec import resolve_value


@dataclass(frozen=True, slots=True)
class PrepaidPeriodPurchasePolicy:
    enabled: bool
    max_months: int


@dataclass(frozen=True, slots=True)
class OutageCompensationPolicy:
    enabled: bool
    minimum_seconds: int


def resolve_prepaid_period_purchase_policy(
    db: Session,
) -> PrepaidPeriodPurchasePolicy:
    enabled = (
        resolve_value(db, SettingDomain.billing, "prepaid_period_purchase_enabled")
        is True
    )
    raw_maximum = resolve_value(
        db, SettingDomain.billing, "prepaid_period_purchase_max_months"
    )
    if isinstance(raw_maximum, bool):
        raise ValueError("Prepaid period-purchase maximum is invalid")
    maximum = int(raw_maximum)
    if maximum < 1 or maximum > 12:
        raise ValueError("Prepaid period-purchase maximum must be 1–12 months")
    return PrepaidPeriodPurchasePolicy(enabled=enabled, max_months=maximum)


def resolve_outage_compensation_policy(db: Session) -> OutageCompensationPolicy:
    enabled = (
        resolve_value(db, SettingDomain.billing, "outage_compensation_enabled") is True
    )
    raw_hours = resolve_value(
        db, SettingDomain.billing, "outage_compensation_min_hours"
    )
    if isinstance(raw_hours, bool):
        raise ValueError("Outage compensation threshold is invalid")
    hours = int(raw_hours)
    if hours < 1 or hours > 168:
        raise ValueError("Outage compensation threshold must be 1–168 hours")
    return OutageCompensationPolicy(
        enabled=enabled,
        minimum_seconds=hours * 3600,
    )


__all__ = [
    "OutageCompensationPolicy",
    "PrepaidPeriodPurchasePolicy",
    "resolve_outage_compensation_policy",
    "resolve_prepaid_period_purchase_policy",
]
