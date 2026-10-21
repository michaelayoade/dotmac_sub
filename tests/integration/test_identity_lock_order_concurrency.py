"""Hosted migrated-PG proofs for principal/Party and collision lock cycles.

Use the repository clone-of-real-Alembic-all-heads fixture. These tests deliberately
coordinate the competing lock graph; SQLite is never concurrency evidence.
"""

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker

from app.models.auth import (
    AuthenticationBinding,
    AuthProvider,
    SessionStatus,
    UserCredential,
)
from app.models.auth import Session as AuthSession
from app.models.party import Party
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.services import credential_party_binding as projection
from app.services import party as party_registry
from app.services import staff_party_adoption as staff
from app.services import staff_session_party_adoption as sessions
from app.services.operator_tenant import OPERATOR_TENANT_ID, provision_operator_tenant
from app.services.owner_commands import CommandContext
from app.services.subscriber import _default_reseller_id
from tests.staff_identity_fixtures import add_bound_staff_user


@pytest.fixture
def engine(cloned_database: Callable[[str], URL]) -> Iterator[Engine]:
    value = create_engine(cloned_database("heads"))

    @event.listens_for(value, "begin")
    def deadlines(connection: Connection) -> None:
        connection.exec_driver_sql("SET LOCAL lock_timeout = '3s'")
        connection.exec_driver_sql("SET LOCAL statement_timeout = '10s'")

    try:
        yield value
    finally:
        value.dispose()


@dataclass(frozen=True)
class Identities:
    staff_id: UUID
    party_id: UUID
    approver_id: UUID
    session_id: UUID
    binding_id: UUID
    staff_credential_id: UUID
    subscriber_id: UUID
    subscriber_credential_id: UUID
    source: str
    reason: str


@pytest.fixture
def identities(engine: Engine) -> Identities:
    with Session(engine) as db:
        provision_operator_tenant(db)
        user, person = add_bound_staff_user(db)
        approver, _ = add_bound_staff_user(db)
        subscriber = Subscriber(
            first_name="Lock",
            last_name="Graph",
            email=f"locks-{uuid4().hex}@example.test",
            reseller_id=_default_reseller_id(db),
        )
        db.add(subscriber)
        db.flush()
        party_registry.bind_subscriber_account(
            db,
            subscriber_id=subscriber.id,
            party_id=person.id,
            source="test-lock-graph",
            reason="Reviewed same person, separate account",
        )
        binding = AuthenticationBinding(
            binding_key=f"lock-test-{uuid4().hex}",
            mechanism_code="local",
            name="Lock graph verifier",
            is_active=True,
        )
        credential = UserCredential(
            system_user_id=user.id,
            provider=AuthProvider.local,
            username=f"staff-lock-{uuid4().hex}",
            password_hash="not-a-real-hash",
            is_active=True,
        )
        other = UserCredential(
            subscriber_id=subscriber.id,
            provider=AuthProvider.local,
            username=f"customer-lock-{uuid4().hex}",
            password_hash="not-a-real-hash",
            is_active=True,
        )
        auth_session = AuthSession(
            system_user_id=user.id,
            party_id=person.id,
            token_hash=f"legacy-{uuid4().hex}",
            status=SessionStatus.active,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add_all([binding, credential, other, auth_session])
        db.flush()
        assert (
            user.party_binding_source is not None
            and user.party_binding_reason is not None
        )
        result = Identities(
            user.id,
            person.id,
            approver.id,
            auth_session.id,
            binding.id,
            credential.id,
            subscriber.id,
            other.id,
            user.party_binding_source,
            user.party_binding_reason,
        )
        db.commit()
        return result


def _projection(
    ids: Identities, *, subscriber: bool
) -> projection.CredentialPartyBinding:
    return projection.CredentialPartyBinding(
        CommandContext.system(
            actor="pytest:identity-locks",
            scope="party:credential_authentication_projection",
            reason="reviewed exact lock graph",
        ),
        ids.subscriber_credential_id if subscriber else ids.staff_credential_id,
        projection.CredentialPrincipalKind.subscriber
        if subscriber
        else projection.CredentialPrincipalKind.system_user,
        ids.subscriber_id if subscriber else ids.staff_id,
        ids.party_id,
        ids.binding_id,
        OPERATOR_TENANT_ID,
        "test-lock-graph",
        "Reviewed exact projection",
    )


@pytest.mark.parametrize("owner", ["staff_adoption", "session_projection"])
def test_principal_lock_then_session_fk_does_not_cycle_with_adoption(
    engine: Engine, identities: Identities, owner: str
) -> None:
    ids = identities
    at_principal = Event()
    factory = sessionmaker(engine, expire_on_commit=False)

    def adopt() -> bool:
        with factory() as db:

            @event.listens_for(db, "do_orm_execute")
            def reached_principal(state: ORMExecuteState) -> None:
                if (
                    getattr(state.statement, "_for_update_arg", None) is not None
                    and state.statement.get_final_froms()[0].name
                    == SystemUser.__tablename__
                ):
                    at_principal.set()

            if owner == "staff_adoption":
                return staff.bind_existing_staff_party(
                    db,
                    staff.BindExistingStaffPartyCommand(
                        CommandContext.system(
                            actor=f"user:{ids.approver_id}",
                            scope=staff.COMMAND_SCOPE,
                            reason="approved staff identity",
                        ),
                        ids.staff_id,
                        ids.party_id,
                        ids.source,
                        ids.reason,
                    ),
                ).replayed
            return sessions.project_staff_session_party(
                db,
                sessions.ProjectStaffSessionPartyCommand(
                    CommandContext.system(
                        actor=f"user:{ids.approver_id}",
                        scope=sessions.COMMAND_SCOPE,
                        reason="approved staff session",
                    ),
                    ids.session_id,
                    ids.staff_id,
                    ids.party_id,
                    uuid4(),
                    "1" * 64,
                    "2" * 64,
                    uuid4(),
                    "3" * 64,
                ),
            ).replayed

    with ThreadPoolExecutor(max_workers=1) as pool:
        with factory() as holder:
            holder.scalar(
                select(SystemUser)
                .where(SystemUser.id == ids.staff_id)
                .with_for_update()
            )
            future = pool.submit(adopt)
            try:
                assert at_principal.wait(5), "owner did not reach the principal lock"
                # Real FK enforcement obtains Party KEY SHARE here. An owner
                # holding Party while waiting for our principal recreates the cycle.
                holder.add(
                    AuthSession(
                        system_user_id=ids.staff_id,
                        party_id=ids.party_id,
                        token_hash=f"new-{uuid4().hex}",
                        status=SessionStatus.active,
                        expires_at=datetime.now(UTC) + timedelta(hours=1),
                    )
                )
                holder.flush()
                holder.commit()
            finally:
                holder.rollback()
        # The migration-541 ratchet makes every usable staff session
        # Party-bound, so both post-cutover owner paths are exact replays.
        assert future.result(timeout=15) is True


def test_projection_collision_never_locks_incumbent_after_binding(
    engine: Engine, identities: Identities
) -> None:
    ids = identities
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as setup:
        projection.bind_credential_party(setup, _projection(ids, subscriber=False))
    at_collision = Event()

    def collide() -> str:
        with factory() as db:

            @event.listens_for(db, "do_orm_execute")
            def reached_collision(state: ORMExecuteState) -> None:
                descriptions = getattr(state.statement, "column_descriptions", ())
                if (
                    descriptions
                    and descriptions[0].get("name") == "id"
                    and descriptions[0].get("entity") is UserCredential
                ):
                    at_collision.set()

            try:
                projection.bind_credential_party(db, _projection(ids, subscriber=True))
            except projection.CredentialBindingError as refused:
                return refused.code
            raise AssertionError("collision was admitted")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with factory() as holder:
            holder.scalar(
                select(SystemUser)
                .where(SystemUser.id == ids.staff_id)
                .with_for_update()
            )
            holder.scalar(
                select(UserCredential)
                .where(UserCredential.id == ids.staff_credential_id)
                .with_for_update()
            )
            future = pool.submit(collide)
            try:
                assert at_collision.wait(5), "contender did not reach tuple arbitration"
                # If the contender tries to lock our incumbent after Party/binding,
                # this ordinary principal->credential->Party acquisition cycles.
                holder.scalar(
                    select(Party).where(Party.id == ids.party_id).with_for_update()
                )
                holder.commit()
            finally:
                holder.rollback()
        assert future.result(timeout=15).endswith(".projection_collision")


def test_concurrent_projection_competitors_admit_exactly_one_tuple(
    engine: Engine, identities: Identities
) -> None:
    factory = sessionmaker(engine, expire_on_commit=False)
    barrier = Barrier(2)

    def project(subscriber: bool) -> str:
        with factory() as db:
            barrier.wait(timeout=5)
            try:
                projection.bind_credential_party(
                    db, _projection(identities, subscriber=subscriber)
                )
                return "bound"
            except projection.CredentialBindingError as refused:
                assert refused.code.endswith(".projection_collision")
                return "collision"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(project, subscriber) for subscriber in (False, True)]
        assert sorted(future.result(timeout=15) for future in futures) == [
            "bound",
            "collision",
        ]
    with factory() as verify:
        assert (
            verify.scalar(
                select(func.count())
                .select_from(UserCredential)
                .where(
                    UserCredential.party_id == identities.party_id,
                    UserCredential.authentication_binding_id == identities.binding_id,
                    UserCredential.tenant_id == OPERATOR_TENANT_ID,
                )
            )
            == 1
        )


def test_changed_credential_identity_is_refused_after_principal_wait(
    engine: Engine, identities: Identities
) -> None:
    ids = identities
    factory = sessionmaker(engine, expire_on_commit=False)
    at_principal = Event()

    def project_cached() -> tuple[str, list[str]]:
        with factory() as db:
            cached = db.get(UserCredential, ids.staff_credential_id)
            assert cached is not None and cached.system_user_id == ids.staff_id
            db.commit()
            locks: list[str] = []

            @event.listens_for(db, "do_orm_execute")
            def observe(state: ORMExecuteState) -> None:
                if getattr(state.statement, "_for_update_arg", None) is not None:
                    table = state.statement.get_final_froms()[0].name
                    locks.append(table)
                    if table == SystemUser.__tablename__:
                        at_principal.set()

            try:
                projection.bind_credential_party(db, _projection(ids, subscriber=False))
            except projection.CredentialBindingError as refused:
                return refused.code, locks
            raise AssertionError("changed principal identity was admitted")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with factory() as holder:
            holder.scalar(
                select(SystemUser)
                .where(SystemUser.id == ids.staff_id)
                .with_for_update()
            )
            future = pool.submit(project_cached)
            try:
                assert at_principal.wait(5)
                holder.execute(
                    update(UserCredential)
                    .where(UserCredential.id == ids.staff_credential_id)
                    .values(system_user_id=None, subscriber_id=ids.subscriber_id)
                )
                holder.commit()
            finally:
                holder.rollback()
        code, locks = future.result(timeout=15)
    assert code.endswith(".principal_mismatch")
    assert locks == [SystemUser.__tablename__, UserCredential.__tablename__]
    with factory() as verify:
        row = verify.get(UserCredential, ids.staff_credential_id)
        assert row is not None and row.party_id is None
        assert row.subscriber_id == ids.subscriber_id and row.system_user_id is None
