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

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.account_recovery import AccountRecoveryRecord, AccountRecoveryState
from app.models.subscriber import Subscriber, UserType
from app.services import account_recovery
from app.services.owner_commands import CommandContext


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


def _replay_safe_idempotency_key(*parts: str) -> str:
    """Deterministic key for a form re-submission of the SAME review step.

    Each admin form here always carries the record's current
    ``confirmation_fingerprint`` (obtained from the review step this action
    confirms). That fingerprint changes on every generation/revision, so a
    key derived from it collapses a genuine double-submit (same fingerprint)
    into one replayed outcome while a later, distinct review (new
    fingerprint) always gets its own key — never a stale cross-generation
    replay.
    """
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def restore_via_recovery(
    db: Session,
    *,
    subscriber_id: str,
    confirmation_fingerprint: str,
    actor_id: str,
    reason: str,
) -> account_recovery.RecoveryOutcome:
    """Thin typed adapter over :func:`account_recovery.restore_account`.

    Routes through the owner-command boundary
    (:func:`app.services.owner_commands.execute_owner_command`, entered by
    ``restore_account`` itself) instead of self-committing — the command
    boundary owns the transaction, so this adapter never calls
    ``db.commit()``.
    """
    context = CommandContext(
        command_id=uuid4(),
        correlation_id=uuid4(),
        actor=actor_id,
        scope=account_recovery.ACCOUNT_RECOVERY_WRITE_SCOPE,
        reason=reason,
        idempotency_key=_replay_safe_idempotency_key(
            "account-recovery:restore", subscriber_id, confirmation_fingerprint
        ),
    )
    command = account_recovery.RestoreAccountCommand(
        account_id=UUID(subscriber_id),
        context=context,
        confirmation_fingerprint=confirmation_fingerprint,
    )
    return account_recovery.restore_account(db, command)


def rebaseline_via_recovery(
    db: Session,
    *,
    subscriber_id: str,
    confirmation_fingerprint: str,
    affected_resource_types: tuple[str, ...],
    actor_id: str,
    reason: str,
) -> account_recovery.RebaselineApplied:
    """Thin typed adapter over :func:`account_recovery.rebaseline_recovery_evidence`.

    Routes through the owner-command boundary; see `restore_via_recovery`.
    """
    context = CommandContext(
        command_id=uuid4(),
        correlation_id=uuid4(),
        actor=actor_id,
        scope=account_recovery.ACCOUNT_RECOVERY_WRITE_SCOPE,
        reason=reason,
        idempotency_key=_replay_safe_idempotency_key(
            "account-recovery:rebaseline",
            subscriber_id,
            confirmation_fingerprint,
            ",".join(sorted(affected_resource_types)),
        ),
    )
    command = account_recovery.RebaselineRecoveryCommand(
        account_id=UUID(subscriber_id),
        context=context,
        confirmation_fingerprint=confirmation_fingerprint,
        affected_resource_types=affected_resource_types,
    )
    return account_recovery.rebaseline_recovery_evidence(db, command)
