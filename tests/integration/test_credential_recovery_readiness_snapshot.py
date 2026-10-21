"""Hosted PostgreSQL tests; local collection alone is not execution evidence."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db import begin_read_only_snapshot
from app.models.auth import AuthProvider, UserCredential
from app.models.system_user import SystemUser
from app.services.credential_recovery import RecoveryReadinessReason
from app.services.credential_security_inventory import (
    CredentialInventoryQuery,
    RecoveryReadinessQuery,
    RecoveryReadinessResult,
    read_credential_inventory,
    read_recovery_readiness,
)
from app.services.domain_errors import DomainError

pytestmark = pytest.mark.integration
OBSERVED = datetime(2026, 10, 12, tzinfo=UTC)
QUERY = RecoveryReadinessQuery(observed_at=OBSERVED)


def _session(engine: Engine, *, snapshot: bool = False) -> Session:
    assert engine.dialect.name == "postgresql", "Hosted PostgreSQL database required"
    db = Session(bind=engine, expire_on_commit=False)
    if snapshot:
        begin_read_only_snapshot(db)
    return db


def test_postgresql_repeatable_snapshot_conserves_reversible_counts(
    engine: Engine,
) -> None:
    principal_id, credential_id = uuid4(), uuid4()
    with _session(engine) as setup:
        setup.add(
            SystemUser(
                id=principal_id,
                first_name="Readiness",
                last_name="Test",
                email=f"{principal_id}@example.test",
                is_active=True,
            )
        )
        setup.flush()
        setup.add(
            UserCredential(
                id=credential_id,
                system_user_id=principal_id,
                provider=AuthProvider.local,
                username=f"readiness-{credential_id}",
                password_hash="plain:synthetic-test-only",
                is_active=True,
            )
        )
        setup.commit()
    try:
        with _session(engine, snapshot=True) as snapshot:
            assert snapshot.scalar(text("SHOW transaction_read_only")) == "on"
            assert (
                snapshot.scalar(text("SHOW transaction_isolation")) == "repeatable read"
            )
            before = read_recovery_readiness(db=snapshot, query=QUERY)
            inventory = read_credential_inventory(
                db=snapshot, query=CredentialInventoryQuery(OBSERVED)
            )
            assert before.reversible_count == inventory.reversible_count
            with _session(engine) as writer:
                principal = writer.get(SystemUser, principal_id)
                assert principal is not None
                principal.email = "\u2003" + "".join(
                    " " if digit in "01234567" else "\t" for digit in uuid4().hex
                )
                writer.commit()
            assert read_recovery_readiness(db=snapshot, query=QUERY) == before
        with _session(engine, snapshot=True) as fresh:
            after = read_recovery_readiness(db=fresh, query=QUERY)

            def count(
                reason: RecoveryReadinessReason, result: RecoveryReadinessResult
            ) -> int:
                return sum(
                    row.count
                    for row in result.cohorts
                    if row.reason == reason and row.principal.value == "staff"
                )

            assert after.reversible_count == before.reversible_count
            assert (
                count(RecoveryReadinessReason.MISSING_EMAIL, after)
                == count(RecoveryReadinessReason.MISSING_EMAIL, before) + 1
            )
            assert (
                count(RecoveryReadinessReason.EXACT_LOCAL_EMAIL_ELIGIBLE, after)
                == count(RecoveryReadinessReason.EXACT_LOCAL_EMAIL_ELIGIBLE, before) - 1
            )
    finally:
        with _session(engine) as cleanup:
            cleanup.execute(
                delete(UserCredential).where(UserCredential.id == credential_id)
            )
            cleanup.execute(delete(SystemUser).where(SystemUser.id == principal_id))
            cleanup.commit()


def test_postgresql_write_snapshot_and_pending_objects_are_refused(
    engine: Engine,
) -> None:
    with _session(engine) as db:
        with pytest.raises(DomainError, match="read-only snapshot"):
            read_recovery_readiness(db=db, query=QUERY)
    with _session(engine, snapshot=True) as db:
        db.add(
            SystemUser(
                first_name="Pending", last_name="Test", email=f"{uuid4()}@example.test"
            )
        )
        with pytest.raises(DomainError, match="read-only snapshot"):
            read_recovery_readiness(db=db, query=QUERY)
    with _session(engine, snapshot=True) as db:
        read_recovery_readiness(db=db, query=QUERY)
        with pytest.raises(DBAPIError):
            db.execute(
                text("UPDATE system_users SET is_active = is_active WHERE false")
            )
