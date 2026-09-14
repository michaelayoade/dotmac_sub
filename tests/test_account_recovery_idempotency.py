"""Genuine idempotency for the three ``customer.account_recovery`` owner commands.

Each command reserves a durable row in the shared ``idempotency_keys`` table
(``app/models/idempotency.py``) keyed by (scope, idempotency_key). A retry
presenting the SAME key and the SAME inputs must return the ORIGINAL typed
outcome rather than erroring or re-mutating; a retry presenting the SAME key
with DIFFERENT inputs must fail closed as a typed
``idempotency_input_conflict`` rather than silently replaying the wrong
decision or raising a raw integrity error.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.account_recovery import AccountRecoveryRecord
from app.models.catalog import SubscriptionStatus
from app.services import account_recovery
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from tests.test_account_lifecycle import _make_offer, _make_subscriber, _make_subscription


def _deletion_command(
    account_id, *, idempotency_key: str, reason: str = "administrative deletion"
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
            idempotency_key=idempotency_key,
        ),
        requested_by="admin",
        deleted_by="admin",
    )


def _restore_command(
    account_id,
    *,
    confirmation_fingerprint: str,
    idempotency_key: str,
    actor: str = "admin",
    reason: str = "reviewed restore",
) -> account_recovery.RestoreAccountCommand:
    command_id = uuid.uuid4()
    return account_recovery.RestoreAccountCommand(
        account_id=account_id,
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor=actor,
            scope=account_recovery.ACCOUNT_RECOVERY_WRITE_SCOPE,
            reason=reason,
            idempotency_key=idempotency_key,
        ),
        confirmation_fingerprint=confirmation_fingerprint,
    )


def _rebaseline_command(
    account_id,
    *,
    confirmation_fingerprint: str,
    affected_resource_types: tuple[str, ...],
    idempotency_key: str,
    actor: str = "admin",
    reason: str = "reviewed rebaseline",
) -> account_recovery.RebaselineRecoveryCommand:
    command_id = uuid.uuid4()
    return account_recovery.RebaselineRecoveryCommand(
        account_id=account_id,
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor=actor,
            scope=account_recovery.ACCOUNT_RECOVERY_WRITE_SCOPE,
            reason=reason,
            idempotency_key=idempotency_key,
        ),
        confirmation_fingerprint=confirmation_fingerprint,
        affected_resource_types=affected_resource_types,
    )


def _make_account(db_session):
    subscriber = _make_subscriber(db_session)
    offer = _make_offer(db_session)
    subscription = _make_subscription(
        db_session, subscriber, offer, status=SubscriptionStatus.active
    )
    # Read the ids BEFORE commit (expire_on_commit would otherwise force an
    # implicit refresh read afterward, leaving the session mid-transaction
    # when the owner command below requires transaction-free entry).
    account_id, subscription_id = subscriber.id, subscription.id
    db_session.commit()
    return account_id, subscription_id


def test_duplicate_deletion_request_replays_the_original_tombstone(db_session) -> None:
    account_id, _ = _make_account(db_session)

    first = account_recovery.request_recoverable_deletion(
        db_session, _deletion_command(account_id, idempotency_key="del-key-1")
    )
    assert isinstance(first, account_recovery.DeletionTombstone)

    db_session_adapter.release_read_transaction(db_session)
    second = account_recovery.request_recoverable_deletion(
        db_session, _deletion_command(account_id, idempotency_key="del-key-1")
    )

    assert isinstance(second, account_recovery.DeletionTombstone)
    assert second.record_id == first.record_id
    assert second.confirmation_fingerprint == first.confirmation_fingerprint
    # Exactly one generation was ever created — the retry did not re-mutate.
    assert (
        db_session.query(AccountRecoveryRecord)
        .filter(AccountRecoveryRecord.account_id == account_id)
        .count()
        == 1
    )


def test_duplicate_deletion_key_with_different_reason_is_a_typed_conflict(
    db_session,
) -> None:
    account_id, _ = _make_account(db_session)

    account_recovery.request_recoverable_deletion(
        db_session,
        _deletion_command(account_id, idempotency_key="del-key-2", reason="reason A"),
    )

    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(account_recovery.AccountRecoveryError) as excinfo:
        account_recovery.request_recoverable_deletion(
            db_session,
            _deletion_command(
                account_id, idempotency_key="del-key-2", reason="reason B"
            ),
        )
    assert excinfo.value.code == "customer.account_recovery.idempotency_input_conflict"
    # Still exactly one generation — the conflicting retry made no new row.
    assert (
        db_session.query(AccountRecoveryRecord)
        .filter(AccountRecoveryRecord.account_id == account_id)
        .count()
        == 1
    )


def test_duplicate_restore_replays_the_original_outcome(db_session) -> None:
    account_id, subscription_id = _make_account(db_session)
    tombstone = account_recovery.request_recoverable_deletion(
        db_session, _deletion_command(account_id, idempotency_key="del-for-restore")
    )
    db_session_adapter.release_read_transaction(db_session)

    first = account_recovery.restore_account(
        db_session,
        _restore_command(
            account_id,
            confirmation_fingerprint=tombstone.confirmation_fingerprint,
            idempotency_key="restore-key-1",
        ),
    )
    assert isinstance(first, account_recovery.RecoveryRestored)
    assert first.restored_subscription_ids == (subscription_id,)

    db_session_adapter.release_read_transaction(db_session)
    second = account_recovery.restore_account(
        db_session,
        _restore_command(
            account_id,
            confirmation_fingerprint=tombstone.confirmation_fingerprint,
            idempotency_key="restore-key-1",
        ),
    )

    assert isinstance(second, account_recovery.RecoveryRestored)
    assert second.record_id == first.record_id
    assert second.restored_subscription_ids == first.restored_subscription_ids


def test_duplicate_restore_key_with_different_actor_is_a_typed_conflict(
    db_session,
) -> None:
    account_id, _ = _make_account(db_session)
    tombstone = account_recovery.request_recoverable_deletion(
        db_session, _deletion_command(account_id, idempotency_key="del-for-restore-2")
    )
    db_session_adapter.release_read_transaction(db_session)

    account_recovery.restore_account(
        db_session,
        _restore_command(
            account_id,
            confirmation_fingerprint=tombstone.confirmation_fingerprint,
            idempotency_key="restore-key-2",
            actor="admin-one",
        ),
    )

    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(account_recovery.AccountRecoveryError) as excinfo:
        account_recovery.restore_account(
            db_session,
            _restore_command(
                account_id,
                confirmation_fingerprint=tombstone.confirmation_fingerprint,
                idempotency_key="restore-key-2",
                actor="admin-two",
            ),
        )
    assert excinfo.value.code == "customer.account_recovery.idempotency_input_conflict"


def test_duplicate_rebaseline_replays_the_original_outcome(db_session) -> None:
    account_id, _ = _make_account(db_session)
    tombstone = account_recovery.request_recoverable_deletion(
        db_session, _deletion_command(account_id, idempotency_key="del-for-rebaseline")
    )
    db_session_adapter.release_read_transaction(db_session)

    first = account_recovery.rebaseline_recovery_evidence(
        db_session,
        _rebaseline_command(
            account_id,
            confirmation_fingerprint=tombstone.confirmation_fingerprint,
            affected_resource_types=("subscription",),
            idempotency_key="rebaseline-key-1",
        ),
    )
    assert isinstance(first, account_recovery.RebaselineApplied)

    db_session_adapter.release_read_transaction(db_session)
    second = account_recovery.rebaseline_recovery_evidence(
        db_session,
        _rebaseline_command(
            account_id,
            confirmation_fingerprint=tombstone.confirmation_fingerprint,
            affected_resource_types=("subscription",),
            idempotency_key="rebaseline-key-1",
        ),
    )

    assert isinstance(second, account_recovery.RebaselineApplied)
    assert second.record_id == first.record_id
    assert second.new_confirmation_fingerprint == first.new_confirmation_fingerprint
    assert second.fingerprint_revision == first.fingerprint_revision


def test_duplicate_rebaseline_key_with_different_resource_types_is_a_typed_conflict(
    db_session,
) -> None:
    account_id, _ = _make_account(db_session)
    tombstone = account_recovery.request_recoverable_deletion(
        db_session,
        _deletion_command(account_id, idempotency_key="del-for-rebaseline-2"),
    )
    db_session_adapter.release_read_transaction(db_session)

    account_recovery.rebaseline_recovery_evidence(
        db_session,
        _rebaseline_command(
            account_id,
            confirmation_fingerprint=tombstone.confirmation_fingerprint,
            affected_resource_types=("subscription",),
            idempotency_key="rebaseline-key-2",
        ),
    )

    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(account_recovery.AccountRecoveryError) as excinfo:
        account_recovery.rebaseline_recovery_evidence(
            db_session,
            _rebaseline_command(
                account_id,
                # Same stored fingerprint from the caller's point of view is
                # impossible after a real revision bump, so this simulates a
                # caller that reused the key across a materially different
                # command by presenting a different resource-type set against
                # the SAME (now-stale) confirmation_fingerprint input.
                confirmation_fingerprint=tombstone.confirmation_fingerprint,
                affected_resource_types=("subscription", "enforcement_lock"),
                idempotency_key="rebaseline-key-2",
            ),
        )
    assert excinfo.value.code == "customer.account_recovery.idempotency_input_conflict"
