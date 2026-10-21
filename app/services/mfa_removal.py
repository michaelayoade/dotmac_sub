"""Shared read-only contract for administrative MFA factor removal.

The public owner commands live with the principal owners
(``staff_provisioning.remove_staff_mfa``,
``reseller_onboarding.remove_reseller_user_mfa``) and the subscriber API
(``auth.MFAMethods.delete``). This module only holds the pieces they share:
the removal-reason contract and lock-timeout helpers. The factor write step
lives in ``credential_administration_mutation`` (flush-only participant).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

REASON_MIN_LENGTH = 3
REASON_MAX_LENGTH = 500
REASON_REQUIRED_MESSAGE = (
    f"A removal reason of {REASON_MIN_LENGTH} to {REASON_MAX_LENGTH} characters "
    "is required."
)


LOCK_TIMEOUT_MESSAGE = "Account is busy; retry shortly."


def set_lock_timeout(db: Session) -> None:
    """Bound principal-lock waits (PostgreSQL only), as other owner commands do."""

    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT set_config('lock_timeout', '5s', true)"))


def is_lock_timeout(exc: OperationalError) -> bool:
    state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    return state == "55P03"


def normalize_reason(reason: str | None) -> str | None:
    """Return the stripped reason, or None when it violates the contract."""

    if reason is None:
        return None
    cleaned = reason.strip()
    if not REASON_MIN_LENGTH <= len(cleaned) <= REASON_MAX_LENGTH:
        return None
    return cleaned
