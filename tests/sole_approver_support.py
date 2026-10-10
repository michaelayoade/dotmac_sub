"""Shared test setup for the governed sole-approver exception.

Writes the four governance settings through the settings owner, exactly as an
operator would, so the policy is exercised against real resolution.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services.domain_settings import billing_settings
from app.services.sole_approver_exception import (
    DECISION_REF_KEY,
    ENABLED_KEY,
    PRINCIPAL_KEY,
    REVIEW_DUE_KEY,
)

DECISION_REF = "governance:decision-test-sole-approver"
JUSTIFICATION = "Sole finance and admin decision-maker; own approval is sufficient."


def future_review_due() -> date:
    return datetime.now(UTC).date() + timedelta(days=30)


def configure_sole_approver_exception(
    db,
    *,
    enabled: bool = True,
    principal: UUID | None = None,
    review_due: date | None = None,
    decision_ref: str = DECISION_REF,
) -> None:
    billing_settings.upsert_by_key(
        db,
        ENABLED_KEY,
        DomainSettingUpdate(
            value_type=SettingValueType.boolean,
            value_text="true" if enabled else "false",
            value_json=enabled,
        ),
    )
    for key, value in (
        (PRINCIPAL_KEY, str(principal) if principal else ""),
        (REVIEW_DUE_KEY, review_due.isoformat() if review_due else ""),
        (DECISION_REF_KEY, decision_ref),
    ):
        billing_settings.upsert_by_key(
            db,
            key,
            DomainSettingUpdate(value_type=SettingValueType.string, value_text=value),
        )
