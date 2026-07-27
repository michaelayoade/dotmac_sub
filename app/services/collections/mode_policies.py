"""Mode-specific collections planners (ADR 0007 Phase 5, section 8).

Read-only policies. Each evaluates exact typed financial facts — never an
account-wide scan — and returns a typed proposal for `collections.lifecycle`
to act on. Neither policy mutates anything, and neither decides access.

- `collections.postpaid_policy` evaluates an exact overdue collectible
  receivable obligation.
- `collections.prepaid_policy` evaluates an exact uncovered/underfunded
  obligation against available typed prepaid funding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.billing_contract import (
    AccountingTreatment,
    BillingObligation,
    BillingRecordAuthority,
    ObligationState,
)
from app.models.collections_case import CollectionsReason
from app.services.billing.customer_subledger import resolve_position
from app.services.domain_errors import DomainError

_ACTIONABLE_STATES = (ObligationState.open, ObligationState.partially_resolved)


class CollectionsPolicyError(DomainError):
    """Fail-closed collections-policy error."""


@dataclass(frozen=True)
class CollectionsProposal:
    """Typed decision one mode policy hands to `collections.lifecycle`."""

    reason: CollectionsReason
    account_id: UUID
    subscription_id: UUID
    obligation_id: UUID
    currency: str
    outstanding_amount: Decimal
    due_at: datetime | None


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None:
        raise CollectionsPolicyError(
            code="collections.policy.naive_instant",
            message="Policy evaluation requires a timezone-aware instant.",
        )


def _aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive instants in tests; PostgreSQL keeps the offset."""

    from datetime import UTC

    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def plan_postpaid_consequence(
    db: Session,
    *,
    obligation: BillingObligation,
    now: datetime,
) -> CollectionsProposal | None:
    """Propose a case for one exact overdue collectible receivable.

    Returns ``None`` when the obligation is not an actionable overdue
    receivable — not due yet, already resolved, or not a receivable at all.
    """

    _require_aware(now)
    if obligation.accounting_treatment is not AccountingTreatment.receivable:
        return None
    if obligation.state not in _ACTIONABLE_STATES:
        return None
    due_at = _aware(obligation.due_at)
    if due_at is None or due_at > now:
        return None

    outstanding = Decimal(obligation.gross_amount) - Decimal(obligation.resolved_amount)
    if outstanding <= 0:
        return None

    return CollectionsProposal(
        reason=CollectionsReason.postpaid_overdue,
        account_id=obligation.account_id,
        subscription_id=obligation.subscription_id,
        obligation_id=obligation.id,
        currency=obligation.currency,
        outstanding_amount=outstanding,
        due_at=due_at,
    )


def plan_prepaid_consequence(
    db: Session,
    *,
    obligation: BillingObligation,
    now: datetime,
    authority: BillingRecordAuthority | None = None,
) -> CollectionsProposal | None:
    """Propose a case for one exact uncovered prepaid obligation.

    The obligation is underfunded when its outstanding amount exceeds the
    account's typed prepaid funding plus unapplied credit in the same
    currency. No receivable is created merely to support enforcement.
    """

    _require_aware(now)
    if obligation.accounting_treatment is not AccountingTreatment.prepaid_consumption:
        return None
    if obligation.state not in _ACTIONABLE_STATES:
        return None
    period_start = _aware(obligation.period_start)
    if period_start is None or period_start > now:
        # The service period has not started; nothing is uncovered yet.
        return None

    outstanding = Decimal(obligation.gross_amount) - Decimal(obligation.resolved_amount)
    if outstanding <= 0:
        return None

    position = resolve_position(
        db,
        account_id=obligation.account_id,
        currency=obligation.currency,
        authority=authority,
    )
    available = position.prepaid_funding_reserved + position.unapplied_customer_credit
    if available >= outstanding:
        return None

    return CollectionsProposal(
        reason=CollectionsReason.prepaid_underfunded,
        account_id=obligation.account_id,
        subscription_id=obligation.subscription_id,
        obligation_id=obligation.id,
        currency=obligation.currency,
        outstanding_amount=outstanding,
        due_at=_aware(obligation.due_at),
    )


__all__ = [
    "CollectionsPolicyError",
    "CollectionsProposal",
    "plan_postpaid_consequence",
    "plan_prepaid_consequence",
]
