"""Shared test setup for the governed sole-approver exception.

Writes the four governance settings through the settings owner, exactly as an
operator would, so the policy is exercised against real resolution.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services.sole_approver_exception import (
    DECISION_REF_KEY,
    ENABLED_KEY,
    PRINCIPAL_KEY,
    REVIEW_DUE_KEY,
)
from app.timezone import APP_TIMEZONE

DECISION_REF = "governance:decision-test-sole-approver"
JUSTIFICATION = "Sole finance and admin decision-maker; own approval is sufficient."


def future_review_due() -> date:
    return datetime.now(APP_TIMEZONE).date() + timedelta(days=30)


def write_governance_setting(db, key: str, value: object) -> None:
    """Write one governance row directly (test fixture only).

    The generic writers refuse these keys by design (``owner_command_only``);
    production changes go through ``apply_admin_settings_form_updates``.
    """

    row = db.scalars(
        select(DomainSetting).where(
            DomainSetting.domain == SettingDomain.billing, DomainSetting.key == key
        )
    ).first()
    if isinstance(value, bool):
        fields = {
            "value_type": SettingValueType.boolean,
            "value_text": "true" if value else "false",
            "value_json": value,
        }
    else:
        fields = {
            "value_type": SettingValueType.string,
            "value_text": str(value),
            "value_json": None,
        }
    if row is None:
        row = DomainSetting(domain=SettingDomain.billing, key=key, **fields)
        db.add(row)
    else:
        for field, field_value in fields.items():
            setattr(row, field, field_value)
        row.is_active = True
    db.commit()


def configure_sole_approver_exception(
    db,
    *,
    enabled: bool = True,
    principal: UUID | None = None,
    review_due: date | None = None,
    decision_ref: str = DECISION_REF,
) -> None:
    write_governance_setting(db, ENABLED_KEY, enabled)
    for key, value in (
        (PRINCIPAL_KEY, str(principal) if principal else ""),
        (REVIEW_DUE_KEY, review_due.isoformat() if review_due else ""),
        (DECISION_REF_KEY, decision_ref),
    ):
        write_governance_setting(db, key, value)
