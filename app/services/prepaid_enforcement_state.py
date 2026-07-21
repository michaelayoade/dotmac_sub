"""Canonical writer for mutable prepaid enforcement timer state."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event


class PrepaidEnforcementStateError(DomainError):
    """Stable failures at the prepaid enforcement state boundary."""


def _error(suffix: str, message: str) -> PrepaidEnforcementStateError:
    return PrepaidEnforcementStateError(
        code=f"financial.prepaid_enforcement_state.{suffix}",
        message=message,
    )


def _account(db: Session, account_id: object) -> Subscriber:
    try:
        resolved_id = UUID(str(account_id))
    except (TypeError, ValueError) as exc:
        raise _error("invalid_account_id", "Account identifier is invalid.") from exc
    account = db.scalar(
        select(Subscriber).where(Subscriber.id == resolved_id).with_for_update()
    )
    if account is None:
        raise _error("account_not_found", "Account was not found.")
    return account


def _emit_transition(
    db: Session,
    account: Subscriber,
    *,
    transition: str,
) -> None:
    emit_event(
        db,
        EventType.prepaid_enforcement_timer_changed,
        {
            "schema_version": 1,
            "account_id": str(account.id),
            "transition": transition,
        },
        subscriber_id=account.id,
        account_id=account.id,
    )


def arm_prepaid_low_balance_timer(
    db: Session,
    account_id: object,
    *,
    armed_at: datetime,
) -> bool:
    """Arm the first low-balance observation without moving an existing timer."""
    account = _account(db, account_id)
    if account.prepaid_low_balance_at is not None:
        return False
    account.prepaid_low_balance_at = armed_at
    _emit_transition(db, account, transition="low_balance_armed")
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
    if account.prepaid_deactivation_at is not None:
        return False
    account.prepaid_deactivation_at = deactivated_at
    _emit_transition(db, account, transition="deactivation_recorded")
    db.flush()
    return True


def clear_prepaid_enforcement_timers(db: Session, account_id: object) -> bool:
    """Clear obsolete prepaid timers without committing the caller transaction."""
    account = _account(db, account_id)
    if (
        account.prepaid_low_balance_at is None
        and account.prepaid_deactivation_at is None
    ):
        return False
    account.prepaid_low_balance_at = None
    account.prepaid_deactivation_at = None
    _emit_transition(db, account, transition="timers_cleared")
    db.flush()
    return True


__all__ = [
    "arm_prepaid_low_balance_timer",
    "clear_prepaid_enforcement_timers",
    "mark_prepaid_deactivated",
    "PrepaidEnforcementStateError",
]
