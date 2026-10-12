"""Fast-lane ordering and stale-identity regression evidence, not PG locks."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

import pytest
from sqlalchemy import event, update
from sqlalchemy.orm import ORMExecuteState, Session

from app.models.auth import AuthenticationBinding, UserCredential
from app.models.party import Party, PartyIdentityStatus, PartyType
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.services import credential_party_binding as projection
from app.services import party as parties
from app.services import staff_party_adoption as staff
from app.services import staff_session_party_adoption as sessions
from app.services import subscriber_party_binding_repair as repair
from app.services.owner_commands import CommandContext
from tests.staff_identity_fixtures import add_bound_staff_user
from tests.test_credential_party_binding import _command, _staff_credential
from tests.test_party_identity_backfill import (
    _EXECUTED_AT,
    _approval,
    _audit,
    _decision,
    _execute,
    _facts,
    _subscriber,
    build_party_backfill_plan,
)
from tests.test_staff_session_party_adoption import _legacy_session


@dataclass(frozen=True)
class LockRead:
    table: str
    refreshed: bool
    ordered: bool


@contextmanager
def trace_locks(db: Session) -> Iterator[list[LockRead]]:
    reads: list[LockRead] = []

    def observe(state: ORMExecuteState) -> None:
        statement = state.statement
        if getattr(statement, "_for_update_arg", None) is not None:
            reads.append(
                LockRead(
                    table=statement.get_final_froms()[0].name,
                    refreshed=bool(
                        state.execution_options.get("populate_existing")
                        or getattr(state.load_options, "_populate_existing", False)
                    ),
                    ordered=bool(tuple(statement._order_by_clauses)),
                )
            )

    event.listen(db, "do_orm_execute", observe)
    try:
        yield reads
    finally:
        event.remove(db, "do_orm_execute", observe)


def test_projection_order_is_principal_credential_party_binding_on_write_and_replay(
    db_session: Session,
) -> None:
    ids = _staff_credential(db_session)
    command = _command(*ids)
    with trace_locks(db_session) as reads:
        first = projection.bind_credential_party(db_session, command)
        replay = projection.bind_credential_party(db_session, command)
    expected = [
        SystemUser.__tablename__,
        UserCredential.__tablename__,
        Party.__tablename__,
        AuthenticationBinding.__tablename__,
    ]
    assert [read.table for read in reads] == expected * 2
    assert all(read.refreshed for read in reads)
    assert first.replayed is False and replay.replayed is True
    assert first.bound_at == replay.bound_at


def test_wrong_typed_principal_refuses_before_any_party_or_binding_lock(
    db_session: Session,
) -> None:
    credential_id, _staff_id, party_id, binding_id = _staff_credential(db_session)
    with (
        trace_locks(db_session) as reads,
        pytest.raises(projection.CredentialBindingError) as refused,
    ):
        projection.bind_credential_party(
            db_session, _command(credential_id, uuid4(), party_id, binding_id)
        )
    assert refused.value.code.endswith(".principal_mismatch")
    assert [read.table for read in reads] == [
        SystemUser.__tablename__,
        UserCredential.__tablename__,
    ]


@pytest.mark.parametrize("changed", ["principal", "credential", "party", "binding"])
def test_projection_refuses_database_changes_hidden_by_identity_map(
    db_session: Session, changed: str
) -> None:
    credential_id, staff_id, party_id, binding_id = _staff_credential(db_session)
    db_session.expire_on_commit = False
    principal = db_session.get(SystemUser, staff_id)
    credential = db_session.get(UserCredential, credential_id)
    person = db_session.get(Party, party_id)
    binding = db_session.get(AuthenticationBinding, binding_id)
    assert (
        principal is not None
        and credential is not None
        and person is not None
        and binding is not None
    )
    if changed == "principal":
        statement = (
            update(SystemUser)
            .where(SystemUser.id == staff_id)
            .values(
                person_party_id=None,
                party_bound_at=None,
                party_binding_source=None,
                party_binding_reason=None,
            )
        )
        expected = ".principal_party_missing"
    elif changed == "credential":
        other, _ = add_bound_staff_user(db_session)
        statement = (
            update(UserCredential)
            .where(UserCredential.id == credential_id)
            .values(system_user_id=other.id)
        )
        expected = ".principal_mismatch"
    elif changed == "party":
        statement = (
            update(Party)
            .where(Party.id == party_id)
            .values(status=PartyIdentityStatus.archived.value)
        )
        expected = ".party_unavailable"
    else:
        statement = (
            update(AuthenticationBinding)
            .where(AuthenticationBinding.id == binding_id)
            .values(is_active=False)
        )
        expected = ".authentication_binding_inactive"
    db_session.execute(statement.execution_options(synchronize_session=False))
    db_session.commit()
    # Cached objects still report the old admitted values before owner entry.
    assert principal.person_party_id == party_id
    assert credential.system_user_id == staff_id
    assert person.status == PartyIdentityStatus.active.value
    assert binding.is_active is True
    with pytest.raises(projection.CredentialBindingError) as refused:
        projection.bind_credential_party(
            db_session, _command(credential_id, staff_id, party_id, binding_id)
        )
    assert refused.value.code.endswith(expected)


def test_staff_adoption_locks_principal_then_party_and_refuses_stale_repoint(
    db_session: Session,
) -> None:
    user, person = add_bound_staff_user(db_session)
    db_session.expire_on_commit = False
    user_id, person_id = user.id, person.id
    source, reason = user.party_binding_source, user.party_binding_reason
    assert source is not None and reason is not None
    command = staff.BindExistingStaffPartyCommand(
        CommandContext.system(
            actor=f"user:{uuid4()}",
            scope=staff.COMMAND_SCOPE,
            reason="reviewed adoption",
        ),
        user_id,
        person_id,
        source,
        reason,
    )
    db_session.commit()
    with trace_locks(db_session) as reads:
        assert staff.bind_existing_staff_party(db_session, command).replayed
    assert [read.table for read in reads] == [
        SystemUser.__tablename__,
        Party.__tablename__,
    ]
    assert all(read.refreshed for read in reads)
    db_session.execute(
        update(SystemUser)
        .where(SystemUser.id == user_id)
        .values(party_binding_reason="changed evidence")
        .execution_options(synchronize_session=False)
    )
    db_session.commit()
    assert user.party_binding_reason == reason
    with pytest.raises(staff.StaffPartyAdoptionError):
        staff.bind_existing_staff_party(db_session, command)


def test_session_projection_locks_principal_party_session_and_refuses_stale_state(
    db_session: Session,
) -> None:
    from app.models.auth import Session as AuthSession
    from app.models.auth import SessionStatus

    user, person = add_bound_staff_user(db_session)
    approver, _ = add_bound_staff_user(db_session)
    auth_session = _legacy_session(db_session, system_user_id=user.id)
    db_session.expire_on_commit = False
    command = sessions.ProjectStaffSessionPartyCommand(
        CommandContext.system(
            actor=f"user:{approver.id}",
            scope=sessions.COMMAND_SCOPE,
            reason="reviewed session projection",
        ),
        auth_session.id,
        user.id,
        person.id,
        uuid4(),
        "1" * 64,
        "2" * 64,
        uuid4(),
        "3" * 64,
    )
    db_session.commit()
    db_session.execute(
        update(AuthSession)
        .where(AuthSession.id == command.session_id)
        .values(status=SessionStatus.revoked)
        .execution_options(synchronize_session=False)
    )
    db_session.commit()
    assert auth_session.status is SessionStatus.active
    with (
        trace_locks(db_session) as reads,
        pytest.raises(sessions.StaffSessionPartyAdoptionError) as refused,
    ):
        sessions.project_staff_session_party(db_session, command)
    assert refused.value.code.endswith(".session_ineligible")
    assert [read.table for read in reads] == [
        SystemUser.__tablename__,
        Party.__tablename__,
        AuthSession.__tablename__,
    ]
    assert all(read.refreshed for read in reads)


def test_subscriber_repair_locks_subscriber_then_party_and_refuses_stale_evidence(
    db_session: Session, subscriber: Subscriber
) -> None:
    person = parties.create_party(
        db_session, party_type=PartyType.person, display_name="Reviewed customer"
    )
    db_session.expire_on_commit = False
    command = repair.BindSubscriberToExistingPartyCommand(
        CommandContext.system(
            actor=f"user:{uuid4()}",
            scope=repair.COMMAND_SCOPE,
            reason="reviewed customer binding",
        ),
        subscriber.id,
        person.id,
        "Reviewed authoritative identity evidence.",
    )
    db_session.commit()
    with trace_locks(db_session) as reads:
        first = repair.bind_subscriber_to_existing_party(db_session, command)
        replay = repair.bind_subscriber_to_existing_party(db_session, command)
    assert not first.replayed and replay.replayed
    assert [read.table for read in reads] == [
        Subscriber.__tablename__,
        Party.__tablename__,
    ] * 2
    assert all(read.refreshed for read in reads)
    db_session.execute(
        update(Subscriber)
        .where(Subscriber.id == command.subscriber_id)
        .values(party_binding_reason="changed review evidence")
        .execution_options(synchronize_session=False)
    )
    db_session.commit()
    assert subscriber.party_binding_reason == command.binding_reason
    with pytest.raises(repair.SubscriberPartyBindingRepairError):
        repair.bind_subscriber_to_existing_party(db_session, command)


def test_backfill_replay_refreshes_and_locks_sorted_subscribers_before_parties(
    db_session: Session,
) -> None:
    from app.models.party import PartyIdentityBackfillReceipt
    from app.services.party_identity_backfill import PartyIdentityBackfillError

    accounts = [
        _subscriber(f"lock-backfill-{uuid4().hex}@example.test") for _ in range(2)
    ]
    db_session.add_all(accounts)
    db_session.flush()
    audit = _audit(*(_facts(account) for account in accounts))
    decisions = tuple(_decision(audit, account.id, uuid4()) for account in accounts)
    plan = build_party_backfill_plan(audit, decisions, planned_at=_EXECUTED_AT)
    approval = _approval(plan)
    _execute(db_session, audit, decisions, approval)
    with trace_locks(db_session) as reads:
        assert _execute(db_session, audit, decisions, approval).replayed
    assert [read.table for read in reads] == [
        PartyIdentityBackfillReceipt.__tablename__,
        Subscriber.__tablename__,
        Party.__tablename__,
    ]
    assert all(read.refreshed for read in reads)
    assert all(read.ordered for read in reads[1:])
    person = db_session.get(Party, decisions[0].planned_party_id)
    assert person is not None
    db_session.execute(
        update(Party)
        .where(Party.id == person.id)
        .values(metadata_={})
        .execution_options(synchronize_session=False)
    )
    assert person.metadata_
    with pytest.raises(PartyIdentityBackfillError, match="provenance drifted"):
        _execute(db_session, audit, decisions, approval)
