"""Admin web presentation for the prepaid activation funding guard.

Adapter only: every decision (quarantine, reason, runbook, override state)
comes from ``financial.prepaid_activation_funding_guard``. This module shapes
that assessment for the staff banner and builds command contexts for the
override routes.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.services.owner_commands import CommandContext
from app.services.prepaid_activation_funding_guard import (
    MIN_OVERRIDE_REASON_LENGTH,
    OVERRIDE_PERMISSION,
    PrepaidActivationFundingError,
    account_has_prepaid_exposure,
    assess_prepaid_funding_quarantine,
)

RUNBOOK_REPOSITORY_BASE_URL = "https://github.com/michaelayoade/dotmac_sub/blob/main/"


@dataclass(frozen=True, slots=True)
class PrepaidFundingQuarantineBanner:
    """Template-ready banner for one funding-quarantined account."""

    account_id: UUID
    reason_code: str
    reason_summary: str
    runbook_path: str
    runbook_url: str
    prepaid_exposure: bool
    override_active: bool
    override_granted_by: str | None
    override_granted_at: str | None
    override_reason: str | None
    can_override: bool
    override_permission: str
    min_reason_length: int
    grant_url: str
    revoke_url: str


def prepaid_funding_quarantine_banner(
    db: Session,
    account_id: UUID | str | None,
    *,
    can_override: bool,
) -> PrepaidFundingQuarantineBanner | None:
    """Return the banner when the account is funding-quarantined, else None."""

    if account_id is None:
        return None
    try:
        assessment = assess_prepaid_funding_quarantine(db, account_id)
    except PrepaidActivationFundingError:
        return None
    if (
        not assessment.quarantined
        or assessment.reason is None
        or assessment.runbook is None
    ):
        return None
    override = assessment.override
    base = (
        f"/admin/customers/accounts/{assessment.account_id}/prepaid-activation-override"
    )
    return PrepaidFundingQuarantineBanner(
        account_id=assessment.account_id,
        reason_code=assessment.reason.value,
        reason_summary=assessment.reason_summary or "",
        runbook_path=assessment.runbook.value,
        runbook_url=RUNBOOK_REPOSITORY_BASE_URL + assessment.runbook.value,
        prepaid_exposure=account_has_prepaid_exposure(db, assessment.account_id),
        override_active=override is not None,
        override_granted_by=override.granted_by if override else None,
        override_granted_at=(
            override.granted_at.strftime("%Y-%m-%d %H:%M UTC") if override else None
        ),
        override_reason=override.reason if override else None,
        can_override=can_override,
        override_permission=OVERRIDE_PERMISSION,
        min_reason_length=MIN_OVERRIDE_REASON_LENGTH,
        grant_url=base,
        revoke_url=f"{base}/revoke",
    )


def override_command_context(
    *,
    actor_system_user_id: UUID,
    account_id: UUID,
    action: str,
    reason: str,
    idempotency_key: str | None = None,
) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"user:{actor_system_user_id}",
        scope=OVERRIDE_PERMISSION,
        reason=reason,
        idempotency_key=(
            idempotency_key
            or f"prepaid-activation-override:{action}:{account_id}:{command_id}"
        ),
    )


def safe_return_path(value: str | None, *, fallback: str) -> str:
    """Accept only same-site admin paths as the post-action destination."""

    candidate = str(value or "").strip()
    if (
        candidate.startswith("/admin/")
        and "//" not in candidate
        and "\\" not in candidate
    ):
        return candidate
    return fallback


__all__ = [
    "PrepaidFundingQuarantineBanner",
    "override_command_context",
    "prepaid_funding_quarantine_banner",
    "safe_return_path",
]
