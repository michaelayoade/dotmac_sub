"""Canonical currency policy for prepaid funding and access decisions."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services import settings_spec
from app.services.domain_errors import DomainError


class PrepaidCurrencyError(DomainError):
    """Stable failure for missing or invalid prepaid currency evidence."""


def normalize_prepaid_currency(value: object) -> str:
    """Normalize one explicit ISO-style currency or fail closed."""

    currency = str(value).strip().upper() if isinstance(value, str) else ""
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise PrepaidCurrencyError(
            code="financial.prepaid_currency.invalid_currency",
            message="The prepaid enforcement currency must be a three-letter code.",
        )
    return currency


def resolve_prepaid_enforcement_currency(db: Session) -> str:
    """Resolve and validate the sole prepaid enforcement currency setting."""

    return normalize_prepaid_currency(
        settings_spec.resolve_value(
            db,
            SettingDomain.billing,
            "prepaid_enforcement_currency",
        )
    )
