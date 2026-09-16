"""Preflight participant gate: refuse a recoverable deletion before mutation.

`cancel_subscription` has consequences beyond a bare status write (ends
active add-ons, resolves active enforcement locks, releases active service
IP assignments). None of those is a registered recovery participant, so
`request_recoverable_deletion` must refuse the whole request — zero
mutation — when any pending subscription currently carries one, rather than
proceeding and only discovering the gap at restore time.

Each guard below is planted (a genuine unsupported consequence blocks the
request and names it) and near-missed (the same setup with the consequence
already cleared does NOT block) so the check is proven sensitive, not just
present.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.account_recovery import AccountRecoveryRecord
from app.models.catalog import AddOn, AddOnType, SubscriptionAddOn, SubscriptionStatus
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.idempotency import IdempotencyKey
from app.services import account_recovery
from app.services.audit_adapter import AuditActor
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from tests.test_account_lifecycle import (
    _make_offer,
    _make_subscriber,
    _make_subscription,
)


def _add_on(db: Session) -> AddOn:
    add_on = AddOn(
        name=f"Test Add-on {uuid.uuid4().hex[:6]}", addon_type=AddOnType.custom
    )
    db.add(add_on)
    db.flush()
    return add_on


def _command(
    account_id, *, idempotency_key: str | None = None, reason: str = "test"
) -> account_recovery.RequestRecoverableDeletionCommand:
    command_id = uuid.uuid4()
    return account_recovery.RequestRecoverableDeletionCommand(
        account_id=account_id,
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="admin",
            scope=account_recovery.ACCOUNT_RECOVERY_WRITE_SCOPE,
            reason=reason,
            idempotency_key=idempotency_key or f"request-deletion:{command_id}",
        ),
        requested_by="admin",
        deleted_by="admin",
        audit_actor=AuditActor.user("admin"),
    )


def test_active_add_on_blocks_deletion_with_zero_mutation(db_session) -> None:
    subscriber = _make_subscriber(db_session)
    offer = _make_offer(db_session)
    subscription = _make_subscription(
        db_session, subscriber, offer, status=SubscriptionStatus.active
    )
    add_on = _add_on(db_session)
    db_session.add(
        SubscriptionAddOn(
            subscription_id=subscription.id, add_on_id=add_on.id, end_at=None
        )
    )
    db_session.commit()

    command = _command(subscriber.id)
    db_session_adapter.release_read_transaction(db_session)
    outcome = account_recovery.request_recoverable_deletion(db_session, command)

    assert isinstance(outcome, account_recovery.DeletionPreflightBlocked)
    assert outcome.unsupported_consequences == ("add_on",)
    assert outcome.blocked_subscription_ids == (subscription.id,)

    db_session.rollback()
    db_session.refresh(subscriber)
    db_session.refresh(subscription)
    assert subscriber.is_active is True
    assert subscription.status == SubscriptionStatus.active
    assert (
        db_session.query(AccountRecoveryRecord)
        .filter(AccountRecoveryRecord.account_id == subscriber.id)
        .count()
        == 0
    )


def test_multi_subscription_preflight_refusal_replays_with_bounded_reference(
    db_session,
) -> None:
    subscriber = _make_subscriber(db_session)
    offer = _make_offer(db_session)
    first = _make_subscription(db_session, subscriber, offer)
    second = _make_subscription(db_session, subscriber, offer)
    account_id = subscriber.id
    blocked_ids = {first.id, second.id}
    add_on = _add_on(db_session)
    db_session.add(
        SubscriptionAddOn(subscription_id=first.id, add_on_id=add_on.id, end_at=None)
    )
    db_session.commit()

    key = f"preflight:{uuid.uuid4()}"
    command = _command(account_id, idempotency_key=key)
    db_session_adapter.release_read_transaction(db_session)
    blocked = account_recovery.request_recoverable_deletion(db_session, command)
    assert isinstance(blocked, account_recovery.DeletionPreflightBlocked)
    assert set(blocked.blocked_subscription_ids) == blocked_ids
    reservation = (
        db_session.query(IdempotencyKey)
        .filter(
            IdempotencyKey.scope == "account_recovery:request_deletion",
            IdempotencyKey.key == key,
        )
        .one()
    )
    assert reservation.ref_id is not None
    assert len(reservation.ref_id) <= 120

    # A later retry with the same key is the original refusal, even if the
    # unsupported add-on has since ended. A new review needs a new key.
    db_session.query(SubscriptionAddOn).filter(
        SubscriptionAddOn.subscription_id == first.id
    ).update({"end_at": datetime.now(UTC)})
    db_session.commit()
    db_session_adapter.release_read_transaction(db_session)
    replayed = account_recovery.request_recoverable_deletion(
        db_session, _command(account_id, idempotency_key=key)
    )
    assert replayed == blocked


def test_ended_add_on_does_not_block_deletion(db_session) -> None:
    """Near-miss: the same add-on, but already ended, must not block."""
    subscriber = _make_subscriber(db_session)
    offer = _make_offer(db_session)
    subscription = _make_subscription(
        db_session, subscriber, offer, status=SubscriptionStatus.active
    )
    add_on = _add_on(db_session)
    db_session.add(
        SubscriptionAddOn(
            subscription_id=subscription.id,
            add_on_id=add_on.id,
            end_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    command = _command(subscriber.id)
    db_session_adapter.release_read_transaction(db_session)
    outcome = account_recovery.request_recoverable_deletion(db_session, command)

    assert isinstance(outcome, account_recovery.DeletionTombstone)


def test_active_enforcement_lock_blocks_deletion_with_zero_mutation(db_session) -> None:
    subscriber = _make_subscriber(db_session)
    offer = _make_offer(db_session)
    subscription = _make_subscription(
        db_session, subscriber, offer, status=SubscriptionStatus.active
    )
    db_session.add(
        EnforcementLock(
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            reason=EnforcementReason.overdue,
            source="test:account_recovery_preflight",
            is_active=True,
        )
    )
    db_session.commit()

    command = _command(subscriber.id)
    db_session_adapter.release_read_transaction(db_session)
    outcome = account_recovery.request_recoverable_deletion(db_session, command)

    assert isinstance(outcome, account_recovery.DeletionPreflightBlocked)
    assert outcome.unsupported_consequences == ("enforcement_lock",)

    db_session.rollback()
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active
    assert (
        db_session.query(AccountRecoveryRecord)
        .filter(AccountRecoveryRecord.account_id == subscriber.id)
        .count()
        == 0
    )


def test_resolved_enforcement_lock_does_not_block_deletion(db_session) -> None:
    """Near-miss: the same lock, but already resolved, must not block."""
    subscriber = _make_subscriber(db_session)
    offer = _make_offer(db_session)
    subscription = _make_subscription(
        db_session, subscriber, offer, status=SubscriptionStatus.active
    )
    db_session.add(
        EnforcementLock(
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            reason=EnforcementReason.overdue,
            source="test:account_recovery_preflight",
            is_active=False,
            resolved_at=datetime.now(UTC),
            resolved_by="test",
        )
    )
    db_session.commit()

    command = _command(subscriber.id)
    db_session_adapter.release_read_transaction(db_session)
    outcome = account_recovery.request_recoverable_deletion(db_session, command)

    assert isinstance(outcome, account_recovery.DeletionTombstone)


def test_already_canceled_subscription_is_never_preflight_checked(db_session) -> None:
    """A subscription this call would not touch (already canceled) must not
    be examined by the preflight — only PENDING cancellations matter."""
    subscriber = _make_subscriber(db_session)
    offer = _make_offer(db_session)
    subscription = _make_subscription(
        db_session, subscriber, offer, status=SubscriptionStatus.canceled
    )
    add_on = _add_on(db_session)
    # An add-on left dangling on an already-canceled subscription must not
    # block a deletion that touches nothing new on it.
    db_session.add(
        SubscriptionAddOn(
            subscription_id=subscription.id, add_on_id=add_on.id, end_at=None
        )
    )
    db_session.commit()

    command = _command(subscriber.id)
    db_session_adapter.release_read_transaction(db_session)
    outcome = account_recovery.request_recoverable_deletion(db_session, command)

    assert isinstance(outcome, account_recovery.DeletionTombstone)
