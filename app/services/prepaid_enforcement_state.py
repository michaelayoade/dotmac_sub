"""Canonical writer for mutable prepaid enforcement timer state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.services.common import coerce_uuid


def _account(db: Session, account_id: object) -> Subscriber | None:
    return db.get(Subscriber, coerce_uuid(account_id))


def arm_prepaid_low_balance_timer(
    db: Session,
    account_id: object,
    *,
    armed_at: datetime,
) -> bool:
    """Arm the first low-balance observation without moving an existing timer."""
    account = _account(db, account_id)
    if account is None or account.prepaid_low_balance_at is not None:
        return False
    account.prepaid_low_balance_at = armed_at
    db.flush()
    return True


def mark_prepaid_deactivated(
    db: Session,
    account_id: object,
    *,
    deactivated_at: datetime,
) -> bool:
    """Record the first successful prepaid suspension without changing policy."""
    account = _account(db, account_id)
    if account is None or account.prepaid_deactivation_at is not None:
        return False
    account.prepaid_deactivation_at = deactivated_at
    db.flush()
    return True


def clear_prepaid_enforcement_timers(db: Session, account_id: object) -> bool:
    """Clear obsolete prepaid timers without committing the caller transaction."""
    account = _account(db, account_id)
    if account is None or (
        account.prepaid_low_balance_at is None
        and account.prepaid_deactivation_at is None
    ):
        return False
    account.prepaid_low_balance_at = None
    account.prepaid_deactivation_at = None
    db.flush()
    return True


__all__ = [
    "arm_prepaid_low_balance_timer",
    "clear_prepaid_enforcement_timers",
    "mark_prepaid_deactivated",
]
