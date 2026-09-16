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
from types import SimpleNamespace

import pytest

from app.models.account_recovery import AccountRecoveryRecord
from app.models.audit import AuditActorType, AuditEvent
from app.models.catalog import SubscriptionStatus
from app.services import account_recovery
from app.services.audit_adapter import AuditActor
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from tests.test_account_lifecycle import (
    _make_offer,
    _make_subscriber,
    _make_subscription,
)


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
        audit_actor=AuditActor.user("admin"),
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


def test_api_key_deletion_retains_typed_audit_principal(db_session) -> None:
    account_id, _ = _make_account(db_session)
    command_id = uuid.uuid4()
    account_recovery.request_recoverable_deletion(
        db_session,
        account_recovery.RequestRecoverableDeletionCommand(
            account_id=account_id,
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor="api_key:key-123",
                scope=account_recovery.ACCOUNT_RECOVERY_WRITE_SCOPE,
                reason="API-key administrative deletion",
                idempotency_key="api-key-deletion-test",
            ),
            requested_by="api_key:key-123",
            deleted_by="api_key:key-123",
            audit_actor=AuditActor.api_key("key-123"),
        ),
    )
    audit = (
        db_session.query(AuditEvent)
        .filter(
            AuditEvent.action == "customer.account_recovery.deletion_tombstoned",
            AuditEvent.entity_id == str(account_id),
        )
        .one()
    )
    assert audit.actor_type is AuditActorType.api_key
    assert audit.actor_id == "key-123"


def test_deletion_replay_keeps_original_fingerprint_after_rebaseline(
    db_session,
) -> None:
    account_id, subscription_id = _make_account(db_session)
    original = account_recovery.request_recoverable_deletion(
        db_session,
        _deletion_command(account_id, idempotency_key="delete-then-rebaseline"),
    )
    assert isinstance(original, account_recovery.DeletionTombstone)

    db_session_adapter.release_read_transaction(db_session)
    revised = account_recovery.rebaseline_recovery_evidence(
        db_session,
        _rebaseline_command(
            account_id,
            confirmation_fingerprint=original.confirmation_fingerprint,
            affected_resource_types=("subscription", "enforcement_lock"),
            idempotency_key="review-after-delete",
        ),
    )
    assert revised.new_confirmation_fingerprint != original.confirmation_fingerprint

    db_session_adapter.release_read_transaction(db_session)
    replay = account_recovery.request_recoverable_deletion(
        db_session,
        _deletion_command(account_id, idempotency_key="delete-then-rebaseline"),
    )
    assert replay == original
    assert replay.affected_subscription_ids == (subscription_id,)


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


def test_partial_restore_replays_original_after_fresh_key_succeeds(
    db_session, monkeypatch
) -> None:
    account_id, subscription_id = _make_account(db_session)
    tombstone = account_recovery.request_recoverable_deletion(
        db_session, _deletion_command(account_id, idempotency_key="del-partial-replay")
    )
    db_session_adapter.release_read_transaction(db_session)

    with monkeypatch.context() as patch:
        patch.setattr(
            account_recovery,
            "restore_subscription_detailed",
            lambda *_args, **_kwargs: SimpleNamespace(subscription_reactivated=False),
        )
        first = account_recovery.restore_account(
            db_session,
            _restore_command(
                account_id,
                confirmation_fingerprint=tombstone.confirmation_fingerprint,
                idempotency_key="partial-restore-key",
            ),
        )
    assert isinstance(first, account_recovery.RecoveryPartiallyRestored)
    assert first.unrestored_subscription_ids == (subscription_id,)

    db_session_adapter.release_read_transaction(db_session)
    completed = account_recovery.restore_account(
        db_session,
        _restore_command(
            account_id,
            confirmation_fingerprint=tombstone.confirmation_fingerprint,
            idempotency_key="fresh-restore-key",
        ),
    )
    assert isinstance(completed, account_recovery.RecoveryRestored)

    db_session_adapter.release_read_transaction(db_session)
    replay = account_recovery.restore_account(
        db_session,
        _restore_command(
            account_id,
            confirmation_fingerprint=tombstone.confirmation_fingerprint,
            idempotency_key="partial-restore-key",
        ),
    )
    assert replay == first


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


def test_rebaseline_replays_original_revision_after_later_review(db_session) -> None:
    account_id, _ = _make_account(db_session)
    tombstone = account_recovery.request_recoverable_deletion(
        db_session,
        _deletion_command(account_id, idempotency_key="del-rebaseline-replay"),
    )
    db_session_adapter.release_read_transaction(db_session)
    first = account_recovery.rebaseline_recovery_evidence(
        db_session,
        _rebaseline_command(
            account_id,
            confirmation_fingerprint=tombstone.confirmation_fingerprint,
            affected_resource_types=("subscription",),
            idempotency_key="first-rebaseline-key",
        ),
    )
    db_session_adapter.release_read_transaction(db_session)
    later = account_recovery.rebaseline_recovery_evidence(
        db_session,
        _rebaseline_command(
            account_id,
            confirmation_fingerprint=first.new_confirmation_fingerprint,
            affected_resource_types=("subscription", "enforcement_lock"),
            idempotency_key="later-rebaseline-key",
        ),
    )
    assert later.fingerprint_revision > first.fingerprint_revision

    db_session_adapter.release_read_transaction(db_session)
    replay = account_recovery.rebaseline_recovery_evidence(
        db_session,
        _rebaseline_command(
            account_id,
            confirmation_fingerprint=tombstone.confirmation_fingerprint,
            affected_resource_types=("subscription",),
            idempotency_key="first-rebaseline-key",
        ),
    )
    assert replay == first


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
