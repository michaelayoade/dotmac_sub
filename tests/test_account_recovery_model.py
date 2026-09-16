"""Fast SQLite parity checks for account-recovery model constraints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.models.account_recovery import AccountRecoveryRecord, AccountRecoveryState
from tests.test_account_lifecycle import _make_subscriber


def _record(
    account_id: uuid.UUID, generation: int, *, restored: bool
) -> AccountRecoveryRecord:
    now = datetime.now(UTC)
    return AccountRecoveryRecord(
        account_id=account_id,
        generation=generation,
        deletion_intent="administrative_recoverable_deletion",
        requested_by="test",
        deleted_by="test",
        requested_at=now,
        deleted_at=now,
        state=AccountRecoveryState.restored if restored else AccountRecoveryState.open,
        affected_resource_types=["subscription"],
        command_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        confirmation_fingerprint="a" * 64,
        restored_at=now if restored else None,
        restored_by="test" if restored else None,
    )


def test_restored_generation_does_not_block_a_new_open_generation(db_session) -> None:
    subscriber = _make_subscriber(db_session)
    db_session.add(_record(subscriber.id, 1, restored=True))
    db_session.commit()

    db_session.add(_record(subscriber.id, 2, restored=False))
    db_session.commit()

    assert (
        db_session.query(AccountRecoveryRecord)
        .filter(AccountRecoveryRecord.account_id == subscriber.id)
        .count()
        == 2
    )
