"""Typed read/adapter layer over ``customer.account_recovery`` for the admin UI.

This module no longer owns deletion, tombstoning, or restoration — those are
``app/services/account_recovery.py``'s job, and it never mutates
invoice/payment/service-order/credential/RADIUS/IP/ONT/splitter/CPE state
directly (that cascade code was removed; account_recovery.py only reverses
the ``subscription`` participant, and any account whose evidence names a
non-subscription resource type fails closed as
``blocked_missing_participants``). HTTP status-code mapping and error
translation belong to the caller (``app/web/admin/system.py``), not here —
every function here raises a typed
:class:`app.services.account_recovery.AccountRecoveryError` or returns a
typed dataclass, never an ``HTTPException``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.account_recovery import AccountRecoveryRecord, AccountRecoveryState
from app.models.subscriber import Subscriber, UserType
from app.services import account_recovery


@dataclass(frozen=True, slots=True)
class DeletedSubscriberRow:
    subscriber: Subscriber
    record: AccountRecoveryRecord


def _matches_query(subscriber: Subscriber, query_text: str) -> bool:
    needle = query_text.strip().lower()
    if not needle:
        return True

    fields = [
        str(subscriber.id),
        subscriber.subscriber_number or "",
        subscriber.account_number or "",
        subscriber.display_name or "",
        subscriber.first_name or "",
        subscriber.last_name or "",
        subscriber.email or "",
        subscriber.phone or "",
    ]
    for item in subscriber.subscriptions:
        fields.append(item.login or "")
    return any(needle in value.lower() for value in fields if value)


def list_deletion_recoverable_subscribers(
    db: Session,
    *,
    query: str | None,
    limit: int = 100,
) -> list[DeletedSubscriberRow]:
    """Every account with an OPEN or BLOCKED recovery generation.

    Read-only: no locking, no mutation, no purge.
    """
    rows = db.execute(
        select(AccountRecoveryRecord, Subscriber)
        .join(Subscriber, Subscriber.id == AccountRecoveryRecord.account_id)
        .options(selectinload(Subscriber.subscriptions))
        .where(
            AccountRecoveryRecord.state.in_(
                (AccountRecoveryState.open, AccountRecoveryState.blocked)
            ),
            Subscriber.user_type != UserType.system_user,
        )
        .order_by(AccountRecoveryRecord.deleted_at.desc())
        .limit(max(50, limit * 5))
    ).all()

    results = [
        DeletedSubscriberRow(subscriber=subscriber, record=record)
        for record, subscriber in rows
        if _matches_query(subscriber, query or "")
    ]
    return results[: max(1, limit)]


def list_recently_deleted(
    db: Session, *, limit: int = 20
) -> list[DeletedSubscriberRow]:
    return list_deletion_recoverable_subscribers(db, query=None, limit=limit)


def describe_recovery(
    db: Session, *, subscriber_id: str
) -> account_recovery.RecoveryEligibility:
    """Read-only eligibility description for one account. Never mutates."""
    return account_recovery.describe_recovery_eligibility(db, UUID(subscriber_id))


def build_page_state(
    db: Session, *, query: str | None, selected_id: str | None
) -> dict[str, Any]:
    """Read-only page state: no mutation, no flush, no commit, no purge.

    The former automatic GET-driven purge (``purge_expired_from_recovery_queue``
    called on every page render) is REMOVED, not replaced. Scheduled
    retention/legal-hold/disposition/purge is explicit Records-owned debt —
    see ``docs/designs/SUBSCRIBER_ACCOUNT_LIFECYCLE_SOURCES.md``. This page
    no longer reports a ``purged_count`` or any "Auto-purged now" result.
    """
    selected_eligibility: account_recovery.RecoveryEligibility | None = None
    selected = (selected_id or "").strip()
    if selected:
        try:
            selected_eligibility = describe_recovery(db, subscriber_id=selected)
        except (ValueError, LookupError):
            selected_eligibility = None

    deleted_rows = list_deletion_recoverable_subscribers(
        db, query=query, limit=100 if (query or "").strip() else 20
    )
    recent_rows = list_recently_deleted(db, limit=20)

    return {
        "query": query or "",
        "deleted_rows": deleted_rows,
        "recent_rows": recent_rows,
        "selected_eligibility": selected_eligibility,
        "selected_id": selected,
    }


def restore_via_recovery(
    db: Session,
    *,
    subscriber_id: str,
    confirmation_fingerprint: str,
    actor_id: str,
    reason: str,
) -> account_recovery.RecoveryOutcome:
    """Thin typed adapter over :func:`account_recovery.restore_account`."""
    command = account_recovery.RestoreAccountCommand(
        account_id=UUID(subscriber_id),
        confirmation_fingerprint=confirmation_fingerprint,
        actor=actor_id,
        reason=reason,
    )
    outcome = account_recovery.restore_account(db, command)
    db.commit()
    return outcome


def rebaseline_via_recovery(
    db: Session,
    *,
    subscriber_id: str,
    confirmation_fingerprint: str,
    affected_resource_types: tuple[str, ...],
    actor_id: str,
    reason: str,
) -> account_recovery.RebaselineApplied:
    """Thin typed adapter over :func:`account_recovery.rebaseline_recovery_evidence`."""
    command = account_recovery.RebaselineRecoveryCommand(
        account_id=UUID(subscriber_id),
        confirmation_fingerprint=confirmation_fingerprint,
        affected_resource_types=affected_resource_types,
        actor=actor_id,
        reason=reason,
    )
    outcome = account_recovery.rebaseline_recovery_evidence(db, command)
    db.commit()
    return outcome
