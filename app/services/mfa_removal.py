"""Shared mechanics for administrative MFA factor removal.

The public owner commands live with the principal owners
(``staff_provisioning.remove_staff_mfa``,
``reseller_onboarding.remove_reseller_user_mfa``) and the subscriber API
(``auth.MFAMethods.delete``). This module only holds the pieces they share:
the removal-reason contract and the factor/recovery-code disable step.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.auth import MFAMethod, MFARecoveryCode

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


@dataclass(frozen=True)
class FactorDisableResult:
    methods_disabled: int
    recovery_codes_deactivated: int

    @property
    def changed(self) -> bool:
        return self.methods_disabled > 0


def disable_locked_principal_factors(
    db: Session, *, principal_column: object, principal_id: object
) -> FactorDisableResult:
    """Disable every live factor of a principal the caller has already locked.

    Lock order is principal (caller), then factor rows, then recovery codes.
    Seeds are wiped so a holder of the old secret cannot re-confirm, and the
    lockout counters are reset.
    """

    methods = list(
        db.scalars(
            select(MFAMethod)
            .where(principal_column == principal_id)  # type: ignore[arg-type]
            .order_by(MFAMethod.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    live = [m for m in methods if m.is_active or m.enabled or m.is_primary]
    codes = 0
    if methods:
        # Also scrub rows that were disabled earlier (legacy bulk disable left
        # seeds and unused recovery codes behind).
        codes = int(
            db.query(MFARecoveryCode)
            .filter(
                MFARecoveryCode.mfa_method_id.in_([m.id for m in methods]),
                MFARecoveryCode.is_active.is_(True),
            )
            .update({"is_active": False}, synchronize_session=False)
            or 0
        )
    for method in methods:
        method.is_active = False
        method.enabled = False
        method.is_primary = False
        method.secret = None
        method.failed_attempts = 0
        method.locked_until = None
    db.flush()
    return FactorDisableResult(len(live), codes)
