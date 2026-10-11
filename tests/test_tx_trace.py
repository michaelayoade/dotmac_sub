"""Trace counterexamples use real SQLAlchemy sessions and auth mapped tables."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.auth import UserCredential
from app.models.event_store import EventStore
from app.models.subscriber import Subscriber
from tests.support.tx_trace import (
    FactKind,
    assert_protected_tx,
    trace_transactions,
)
from tests.test_password_authentication import _credential


def _setup(db_session: Session, person: Subscriber) -> UUID:
    credential = _credential(db_session, person)
    identifier = credential.id
    db_session.commit()
    return identifier


def _write(db_session: Session, identifier: UUID) -> None:
    db_session.execute(
        update(UserCredential)
        .where(UserCredential.id == identifier)
        .values(failed_login_attempts=1)
    )


def _audit(db_session: Session) -> None:
    db_session.add(AuditEvent(action="test.trace", entity_type="credential"))
    db_session.flush()


def test_core_gate_update_and_audit_share_root(
    db_session: Session, person: Subscriber
) -> None:
    identifier = _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        _write(db_session, identifier)
        _audit(db_session)
        db_session.commit()
    assert_protected_tx(trace.result)
    assert any(
        f.kind == FactKind.SQL_WRITE
        and f.table == "user_credentials"
        and f.operation == "update"
        for f in trace.result.facts
    )
    assert "user@example.com" not in repr(trace.result)
    with pytest.raises(FrozenInstanceError):
        trace.result.facts[0].order = 99


@pytest.mark.parametrize("helper_audit", [False, True])
def test_missing_or_other_session_audit_is_rejected(
    db_session: Session, person: Subscriber, helper_audit: bool
) -> None:
    identifier = _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        _write(db_session, identifier)
        db_session.commit()
        if helper_audit:
            with Session(
                bind=db_session.get_bind(), join_transaction_mode="create_savepoint"
            ) as helper:
                _audit(helper)
                helper.commit()
    with pytest.raises(
        AssertionError, match=("exactly one" if helper_audit else "same-root")
    ):
        assert_protected_tx(trace.result)
    if helper_audit:
        roots = {f.root for f in trace.result.facts if f.kind == FactKind.COMMIT}
        assert len(roots) == 2


def test_helper_state_commit_before_owner_audit_cannot_be_attributed_to_owner(
    db_session: Session, person: Subscriber
) -> None:
    identifier = _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        with Session(
            bind=db_session.get_bind(), join_transaction_mode="create_savepoint"
        ) as helper:
            _write(helper, identifier)
            helper.commit()
        _audit(db_session)
        db_session.commit()
    with pytest.raises(AssertionError, match="exactly one"):
        assert_protected_tx(trace.result)
    writes = [f for f in trace.result.facts if f.kind == FactKind.SQL_WRITE]
    assert next(f.root for f in writes if f.table == "user_credentials") != next(
        f.root for f in writes if f.table == "audit_events"
    )


def test_two_fully_audited_root_commits_still_rejected(
    db_session: Session, person: Subscriber
) -> None:
    identifier = _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        for _ in range(2):
            _write(db_session, identifier)
            _audit(db_session)
            db_session.commit()
    with pytest.raises(AssertionError, match="exactly one"):
        assert_protected_tx(trace.result)


def test_rollback_has_no_committed_partial_record(
    db_session: Session, person: Subscriber
) -> None:
    identifier = _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        _write(db_session, identifier)
        _audit(db_session)
        db_session.rollback()
    assert_protected_tx(trace.result, success=False)
    assert db_session.get(UserCredential, identifier).failed_login_attempts == 0
    assert db_session.query(AuditEvent).filter_by(action="test.trace").count() == 0


def test_nested_commit_is_not_a_root_commit(
    db_session: Session, person: Subscriber
) -> None:
    identifier = _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        with db_session.begin_nested():
            _write(db_session, identifier)
            _audit(db_session)
        db_session.commit()
    assert_protected_tx(trace.result)
    commits = [f for f in trace.result.facts if f.kind == FactKind.COMMIT]
    assert len(commits) == 2
    assert len({f.root for f in commits}) == 1


def test_audit_rolled_back_in_savepoint_cannot_cover_root_write(
    db_session: Session, person: Subscriber
) -> None:
    identifier = _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        _write(db_session, identifier)
        savepoint = db_session.begin_nested()
        _audit(db_session)
        savepoint.rollback()
        db_session.commit()
    with pytest.raises(AssertionError, match="same-root"):
        assert_protected_tx(trace.result)


def test_external_effect_requires_its_own_intent_root_commit(
    db_session: Session, person: Subscriber
) -> None:
    identifier = _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        _write(db_session, identifier)
        _audit(db_session)
        root = trace.result.transactions[0].root
        trace.record_external_effect(intent_root=root)
        db_session.commit()
    with pytest.raises(AssertionError, match="inside protected transaction"):
        assert_protected_tx(trace.result)


@pytest.mark.parametrize("wrong_root", [False, True])
def test_postcommit_bounded_transport_spy(
    db_session: Session,
    person: Subscriber,
    monkeypatch: pytest.MonkeyPatch,
    wrong_root: bool,
) -> None:
    identifier = _setup(db_session, person)

    # One explicit seam, no broad patching of third-party networking libraries.
    class Transport:
        def send(self) -> None:
            raise AssertionError("test must bind its transport")

    transport = Transport()
    with trace_transactions(db_session.get_bind().engine) as trace:
        _write(db_session, identifier)
        _audit(db_session)
        db_session.add(
            EventStore(event_id=uuid4(), event_type="test.delivery_intent", payload={})
        )
        db_session.flush()
        root = trace.result.transactions[0].root
        monkeypatch.setattr(
            transport,
            "send",
            lambda: trace.record_external_effect(
                intent_root=root + 100 if wrong_root else root
            ),
        )
        db_session.commit()
        transport.send()
    if wrong_root:
        with pytest.raises(AssertionError, match="before intent commit"):
            assert_protected_tx(trace.result)
    else:
        assert_protected_tx(trace.result)


def test_harmless_sql_ignored_and_hooks_removed_on_exception(
    db_session: Session, person: Subscriber
) -> None:
    identifier = _setup(db_session, person)
    with pytest.raises(RuntimeError, match="stop"):
        with trace_transactions(db_session.get_bind().engine) as trace:
            db_session.execute(select(UserCredential.id))
            db_session.execute(text("SELECT 1"))
            raise RuntimeError("stop")
    frozen = trace.result
    _write(db_session, identifier)
    _audit(db_session)
    db_session.commit()
    assert trace.result == frozen
    assert not any(
        f.kind in (FactKind.SQL_WRITE, FactKind.MAPPER_WRITE) for f in frozen.facts
    )
    with pytest.raises(RuntimeError, match="outside trace"):
        trace.record_external_effect(intent_root=1)


def test_event_store_alone_is_not_canonical_audit(
    db_session: Session, person: Subscriber
) -> None:
    identifier = _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        _write(db_session, identifier)
        db_session.add(
            EventStore(event_id=uuid4(), event_type="test.trace", payload={})
        )
        db_session.commit()
    with pytest.raises(AssertionError, match="same-root"):
        assert_protected_tx(trace.result)


def test_raw_sql_update_captured_without_parameters(
    db_session: Session, person: Subscriber
) -> None:
    _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        db_session.execute(
            text("UPDATE user_credentials SET failed_login_attempts=:n"), {"n": 2}
        )
        _audit(db_session)
        db_session.commit()
    assert_protected_tx(trace.result)
    assert any(
        f.table == "user_credentials" and f.operation == "update"
        for f in trace.result.facts
    )


def test_effect_before_intent_transaction_begins_is_rejected(
    db_session: Session, person: Subscriber
) -> None:
    identifier = _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        trace.record_external_effect(intent_root=1)
        _write(db_session, identifier)
        _audit(db_session)
        db_session.commit()
    with pytest.raises(AssertionError, match="before intent commit"):
        assert_protected_tx(trace.result)


def test_engine_dml_without_session_is_not_given_owner_root(
    db_session: Session, person: Subscriber
) -> None:
    _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        db_session.get_bind().exec_driver_sql(
            "UPDATE user_credentials SET failed_login_attempts=2"
        )
    with pytest.raises(AssertionError, match="unattributed SQL write"):
        assert_protected_tx(trace.result)


def test_committed_root_without_intent_cannot_authorize_external_effect(
    db_session: Session,
    person: Subscriber,
) -> None:
    identifier = _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        _write(db_session, identifier)
        _audit(db_session)
        root = trace.result.transactions[0].root
        db_session.commit()
        trace.record_external_effect(intent_root=root)
    with pytest.raises(AssertionError, match="lacks committed transactional intent"):
        assert_protected_tx(trace.result)


def test_caught_failed_audit_insert_does_not_prove_audit(
    db_session: Session,
    person: Subscriber,
) -> None:
    identifier = _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        _write(db_session, identifier)
        with pytest.raises(IntegrityError):
            db_session.execute(text("INSERT INTO audit_events (action) VALUES (NULL)"))
        db_session.commit()
    assert db_session.get(UserCredential, identifier).failed_login_attempts == 1
    with pytest.raises(AssertionError, match="same-root"):
        assert_protected_tx(trace.result)


@pytest.mark.parametrize("conflict_ignore", [False, True])
def test_noop_audit_insert_does_not_prove_audit(
    db_session: Session,
    person: Subscriber,
    conflict_ignore: bool,
) -> None:
    identifier = _setup(db_session, person)
    _audit(db_session)
    existing_id = db_session.query(AuditEvent.id).one()[0]
    db_session.commit()
    with trace_transactions(db_session.get_bind().engine) as trace:
        _write(db_session, identifier)
        statement = sqlite_insert(AuditEvent).values(
            id=existing_id, action="test.trace", entity_type="credential"
        )
        if conflict_ignore:
            statement = statement.prefix_with("OR IGNORE")
        else:
            statement = statement.on_conflict_do_nothing(index_elements=[AuditEvent.id])
        result = db_session.execute(statement)
        assert result.rowcount == 0
        db_session.commit()
    with pytest.raises(AssertionError, match="same-root"):
        assert_protected_tx(trace.result)


@pytest.mark.parametrize("operation", ["delete", "update"])
def test_audit_mutation_in_evidence_root_rejected(
    db_session: Session,
    person: Subscriber,
    operation: str,
) -> None:
    identifier = _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        _write(db_session, identifier)
        _audit(db_session)
        if operation == "delete":
            db_session.execute(delete(AuditEvent))
        else:
            db_session.execute(update(AuditEvent).values(action="test.changed"))
        db_session.commit()
    with pytest.raises(AssertionError, match="same-root"):
        assert_protected_tx(trace.result)


@pytest.mark.parametrize("operation", ["delete", "update"])
def test_intent_mutation_in_evidence_root_rejected(
    db_session: Session,
    person: Subscriber,
    operation: str,
) -> None:
    identifier = _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        _write(db_session, identifier)
        _audit(db_session)
        db_session.add(
            EventStore(event_id=uuid4(), event_type="test.intent", payload={})
        )
        db_session.flush()
        if operation == "delete":
            db_session.execute(delete(EventStore))
        else:
            db_session.execute(update(EventStore).values(event_type="test.changed"))
        root = trace.result.transactions[0].root
        db_session.commit()
        trace.record_external_effect(intent_root=root)
    with pytest.raises(AssertionError, match="lacks committed transactional intent"):
        assert_protected_tx(trace.result)


def test_ordinary_insert_returning_proves_audit(
    db_session: Session,
    person: Subscriber,
) -> None:
    identifier = _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        _write(db_session, identifier)
        assert db_session.execute(
            insert(AuditEvent)
            .values(action="test.trace", entity_type="credential")
            .returning(AuditEvent.id)
        ).one()
        db_session.commit()
    assert_protected_tx(trace.result)


def test_noop_intent_insert_does_not_authorize_external_effect(
    db_session: Session,
    person: Subscriber,
) -> None:
    identifier = _setup(db_session, person)
    event_id = uuid4()
    db_session.add(EventStore(event_id=event_id, event_type="test.intent", payload={}))
    db_session.commit()
    with trace_transactions(db_session.get_bind().engine) as trace:
        _write(db_session, identifier)
        _audit(db_session)
        result = db_session.execute(
            sqlite_insert(EventStore)
            .values(event_id=event_id, event_type="test.intent", payload={})
            .on_conflict_do_nothing(index_elements=[EventStore.event_id])
        )
        assert result.rowcount == 0
        root = trace.result.transactions[0].root
        db_session.commit()
        trace.record_external_effect(intent_root=root)
    with pytest.raises(AssertionError, match="lacks committed transactional intent"):
        assert_protected_tx(trace.result)


def test_zero_row_insert_select_does_not_prove_audit(
    db_session: Session,
    person: Subscriber,
) -> None:
    identifier = _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        _write(db_session, identifier)
        result = db_session.execute(
            text(
                "INSERT INTO audit_events (action, entity_type) SELECT 'test.trace', 'credential' WHERE 0"
            )
        )
        assert result.rowcount == 0
        db_session.commit()
    with pytest.raises(AssertionError, match="same-root"):
        assert_protected_tx(trace.result)


def test_raw_sql_literal_tokens_cannot_spoof_returning_evidence(
    db_session: Session,
    person: Subscriber,
) -> None:
    identifier = _setup(db_session, person)
    with trace_transactions(db_session.get_bind().engine) as trace:
        _write(db_session, identifier)
        result = db_session.execute(
            text(
                "INSERT INTO audit_events(action,entity_type) SELECT 'VALUES(', 'RETURNING' WHERE 0"
            )
        )
        assert result.rowcount == 0
        db_session.commit()
    with pytest.raises(AssertionError, match="same-root"):
        assert_protected_tx(trace.result)
