"""Collect locally only. Hosted migrated PostgreSQL is the FK/lock authority."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.auth import ApiKey, MFAMethod, MFAMethodType, MFARecoveryCode
from app.models.dispatch import TechnicianProfile
from app.models.party import Party
from app.models.subscriber import Reseller, Subscriber
from app.models.system_user import SystemUser
from app.schemas.auth import ApiKeyUpdate
from app.services import api_key_administration as keys
from app.services import staff_provisioning as owner
from tests.api_key_update_fixtures import update_command
from tests.integration import test_oidc_atomic_session_concurrency as races
from tests.integration.test_oidc_atomic_session_concurrency import Worker, _blocked_by
from tests.staff_deletion_fixtures import (
    bind_party_fixture,
    bulk_command,
    command,
    staff_fixture,
)
from tests.test_api_key_administration import command as revoke_command
from tests.web_api_key_fixtures import mutate_command

engine = races.engine


@dataclass(frozen=True)
class StaffScenario:
    user_id: UUID
    key_id: UUID
    subscriber_id: UUID
    party_id: UUID
    audit_ids: frozenset[UUID]


def _audit_ids(db: Session) -> frozenset[UUID]:
    # Real-Alembic clones retain migration evidence (for example revision 667).
    # Compare every audit ID, without filtering action/target, so a rollback
    # cannot leave ANY partial scenario evidence or remove preexisting evidence.
    return frozenset(db.scalars(select(AuditEvent.id)))


@pytest.fixture
def scenario(engine: Engine) -> StaffScenario:
    with Session(engine) as db:
        user_id, _ = staff_fixture(db, auth_children=False)
        party = Party(party_type="person", display_name="retained PG identity")
        subscriber = Subscriber(
            first_name="Inert",
            last_name="Subscriber",
            email=f"{uuid4()}@test.invalid",
            reseller=Reseller(name=f"Staff deletion fixture {uuid4()}"),
        )
        db.add_all([party, subscriber])
        db.flush()
        key = ApiKey(
            system_user_id=user_id,
            subscriber_id=subscriber.id,
            key_hash=f"inert-pg-key-{uuid4()}",
        )
        db.add(key)
        bind_party_fixture(db, user_id=user_id, party_id=party.id)
        db.flush()
        result = StaffScenario(user_id, key.id, subscriber.id, party.id, _audit_ids(db))
        db.commit()
        return result


def delete_worker(
    engine: Engine,
    user_ids: tuple[UUID, ...],
    worker: Worker,
    pause: bool = False,
    *,
    deletion_command: owner.BulkDeleteStaffAccountsCommand | None = None,
):
    with engine.connect() as connection, Session(connection) as db:
        worker.identify(db)
        if pause:
            event.listen(db, "before_commit", worker.pause_before_commit)
        else:
            worker.ready.set()
        return owner.bulk_delete_staff_accounts(
            db, command=deletion_command or bulk_command(user_ids)
        )


def key_worker(
    engine: Engine,
    scenario: StaffScenario,
    action: str,
    worker: Worker,
    pause: bool = False,
):
    with engine.connect() as connection, Session(connection) as db:
        worker.identify(db)
        if pause:
            event.listen(db, "before_commit", worker.pause_before_commit)
        else:
            worker.ready.set()
        if action == "patch":
            return keys.update(
                db,
                command=update_command(
                    scenario.key_id,
                    ApiKeyUpdate(label="inert-updated", subscriber_id=None),
                ),
            )
        if action == "revoke":
            return keys.revoke(db, command=revoke_command(scenario.key_id))
        return keys.web_mutate(
            db,
            command=mutate_command(
                scenario.key_id, keys.WebApiKeyAction.ROTATE, scenario.subscriber_id
            ),
        )


@pytest.mark.parametrize("action", ["patch", "revoke", "rotate"])
@pytest.mark.parametrize("delete_first", [False, True])
def test_staff_delete_and_existing_key_commands_serialize(
    engine: Engine, scenario: StaffScenario, action: str, delete_first: bool
) -> None:
    blocker, waiter = Worker(), Worker()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = (
            pool.submit(delete_worker, engine, (scenario.user_id,), blocker, True)
            if delete_first
            else pool.submit(key_worker, engine, scenario, action, blocker, True)
        )
        try:
            assert blocker.ready.wait(3) and blocker.backend_pid is not None
            second = (
                pool.submit(key_worker, engine, scenario, action, waiter)
                if delete_first
                else pool.submit(delete_worker, engine, (scenario.user_id,), waiter)
            )
            _blocked_by(engine, waiter=waiter, blocker=blocker.backend_pid)
        finally:
            blocker.release.set()
        first.result(timeout=15)
        if delete_first and action != "rotate":
            with pytest.raises(
                (keys.ApiKeyUpdateError, keys.ApiKeyRevocationError)
            ) as error:
                second.result(timeout=15)
            assert error.value.code.endswith(".not_found")
        else:
            result = second.result(timeout=15)
            if delete_first:
                assert result.key_id is None
    with Session(engine) as db:
        assert (
            db.get(SystemUser, scenario.user_id) is None
            and db.get(ApiKey, scenario.key_id) is None
        )
        assert (
            db.get(Subscriber, scenario.subscriber_id) is not None
            and db.get(Party, scenario.party_id) is not None
        )
        actions = tuple(
            db.scalars(
                select(AuditEvent.action)
                .where(AuditEvent.entity_id == str(scenario.key_id))
                .order_by(AuditEvent.occurred_at)
            )
        )
        expected = (
            ()
            if delete_first
            else (
                {
                    "patch": "auth.api_key.updated",
                    "revoke": "auth.api_key.revoked",
                    "rotate": "auth.api_key.rotated",
                }[action],
            )
        )
        assert actions == (*expected, "auth.api_key.deleted")


def test_overlapping_reverse_bulk_batches_refuse_busy_principal(
    engine: Engine, scenario: StaffScenario
) -> None:
    with Session(engine) as db:
        second_id, _ = staff_fixture(db, auth_children=False)
    blocker, waiter = Worker(), Worker()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            delete_worker, engine, (scenario.user_id, second_id), blocker, True
        )
        try:
            assert blocker.ready.wait(3)
            second = pool.submit(
                delete_worker, engine, (second_id, scenario.user_id), waiter
            )
            with pytest.raises(owner.StaffProvisioningError) as error:
                second.result(timeout=3)
            assert error.value.code.endswith(".deletion_busy")
        finally:
            blocker.release.set()
        assert len(first.result(timeout=15).deleted_user_ids) == 2
    result = delete_worker(engine, (second_id, scenario.user_id), Worker())
    assert result.deleted_user_ids == () and result.skipped_count == 2


def test_activation_contention_refuses_then_retry_rechecks_principal(
    engine: Engine, scenario: StaffScenario
) -> None:
    with Session(engine) as blocker:
        user = blocker.scalar(
            select(SystemUser)
            .where(SystemUser.id == scenario.user_id)
            .with_for_update()
        )
        user.is_active = True
        blocker.flush()
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(delete_worker, engine, (scenario.user_id,), Worker())
            with pytest.raises(owner.StaffProvisioningError) as error:
                result.result(timeout=3)
            assert error.value.code.endswith(".deletion_busy")
        blocker.commit()
    assert delete_worker(engine, (scenario.user_id,), Worker()).skipped_count == 1
    with Session(engine) as db:
        assert db.get(SystemUser, scenario.user_id).is_active
        assert db.get(ApiKey, scenario.key_id) is not None
        assert _audit_ids(db) == scenario.audit_ids


@pytest.mark.parametrize("locked", ["principal", "key"])
def test_lock_busy_is_sanitized_and_rolls_back(
    engine: Engine, scenario: StaffScenario, locked: str
) -> None:
    with Session(engine) as blocker:
        blocker.scalar(
            (
                select(SystemUser).where(SystemUser.id == scenario.user_id)
                if locked == "principal"
                else select(ApiKey).where(ApiKey.id == scenario.key_id)
            ).with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(delete_worker, engine, (scenario.user_id,), Worker())
            with pytest.raises(owner.StaffProvisioningError) as error:
                result.result(timeout=10)
        assert (
            error.value.code.endswith(".deletion_busy")
            and error.value.__suppress_context__
        )
        blocker.rollback()
    with Session(engine) as db:
        assert db.get(ApiKey, scenario.key_id) is not None
        assert _audit_ids(db) == scenario.audit_ids


@pytest.mark.parametrize("blocker_kind", ["mfa_recovery", "technician"])
def test_late_bulk_fk_blocker_rolls_back_all_rows_and_audits(
    engine: Engine, scenario: StaffScenario, blocker_kind: str
) -> None:
    with Session(engine) as db:
        second_id, _ = staff_fixture(db, auth_children=False)
        if blocker_kind == "mfa_recovery":
            method = MFAMethod(
                system_user_id=second_id,
                method_type=MFAMethodType.totp,
                secret="inert-seed",
            )
            db.add(method)
            db.flush()
            db.add(
                MFARecoveryCode(
                    mfa_method_id=method.id, code_hash="inert-pg-recovery-hash"
                )
            )
        else:
            db.add(TechnicianProfile(system_user_id=second_id, person_id=second_id))
        db.commit()
        with pytest.raises(owner.StaffProvisioningError) as error:
            owner.bulk_delete_staff_accounts(
                db, command=bulk_command((scenario.user_id, second_id))
            )
        assert error.value.code.endswith(".linked_records")
        assert (
            db.get(SystemUser, scenario.user_id) is not None
            and db.get(SystemUser, second_id) is not None
        )
        assert db.get(ApiKey, scenario.key_id) is not None
        assert _audit_ids(db) == scenario.audit_ids


def test_parent_audit_failure_rolls_back_successful_key_participant(
    engine: Engine, scenario: StaffScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("inert parent audit failure")

    monkeypatch.setattr(owner, "stage_audit_event", fail)
    with Session(engine) as db:
        with pytest.raises(RuntimeError):
            owner.delete_staff_account(db, command=command(scenario.user_id))
        assert (
            db.get(SystemUser, scenario.user_id) is not None
            and db.get(ApiKey, scenario.key_id) is not None
        )
        assert _audit_ids(db) == scenario.audit_ids


def grantor_scenario(engine: Engine, scenario: StaffScenario) -> tuple[UUID, UUID]:
    from app.models.rbac import Permission, Role

    with Session(engine) as db:
        # Deterministic A < B so bulk deletion necessarily locks A before busy B.
        target_id = UUID(int=scenario.user_id.int + 1)
        db.add(
            SystemUser(
                id=target_id,
                first_name="Inert",
                last_name="Target",
                email=f"{uuid4()}@test.invalid",
                is_active=False,
            )
        )
        db.add(Role(name="admin", is_active=True))
        permission = Permission(
            key=f"test:grant:{uuid4()}", is_active=True, is_ui_assignable=True
        )
        db.add(permission)
        db.flush()
        permission_id = permission.id
        db.commit()
        return target_id, permission_id


def assignment_worker(
    engine: Engine,
    scenario: StaffScenario,
    target_id: UUID,
    permission_id: UUID,
    worker: Worker,
):
    from app.services import system_user_assignments as assignments
    from app.services.owner_commands import CommandContext

    with engine.connect() as connection, Session(connection) as db:
        worker.identify(db)
        context = CommandContext.system(
            actor=f"user:{scenario.user_id}",
            scope="rbac:assign",
            reason="in-flight grantor FK race",
        )
        return assignments.replace_system_user_assignments(
            db,
            assignments.ReplaceSystemUserAssignmentsCommand(
                context, target_id, (), (permission_id,)
            ),
        )


@pytest.mark.parametrize("bulk_cycle", [False, True])
def test_deletion_nowait_breaks_inflight_rbac_grantor_fk_cycle(
    engine: Engine,
    scenario: StaffScenario,
    monkeypatch: pytest.MonkeyPatch,
    bulk_cycle: bool,
) -> None:
    from threading import Event

    from app.models.rbac import SystemUserPermission
    from app.services import system_user_assignments as assignments

    target_id, permission_id = grantor_scenario(engine, scenario)
    writer, deletion = Worker(), Worker()
    deletion_has_a, attempt_role = Event(), Event()
    replace_permissions = assignments._replace_direct_permissions
    key_participant = keys.stage_staff_key_deletion

    def paused_permissions(db, **kwargs):
        # Real RBAC owner already holds B and the shared admin Role here.
        writer.ready.set()
        assert writer.release.wait(8)
        return replace_permissions(db, **kwargs)

    def paused_key_participant(db, *, command):
        result = key_participant(db, command=command)
        if not bulk_cycle:
            deletion_has_a.set()
            assert attempt_role.wait(8)
        return result

    monkeypatch.setattr(assignments, "_replace_direct_permissions", paused_permissions)
    monkeypatch.setattr(keys, "stage_staff_key_deletion", paused_key_participant)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(
            assignment_worker, engine, scenario, target_id, permission_id, writer
        )
        try:
            assert writer.ready.wait(3)
            targets = (
                (scenario.user_id, target_id) if bulk_cycle else (scenario.user_id,)
            )
            deletion_command = bulk_command(targets)
            deleting = pool.submit(
                delete_worker,
                engine,
                targets,
                deletion,
                deletion_command=deletion_command,
            )
            if not bulk_cycle:
                assert deletion_has_a.wait(3) and deletion.backend_pid is not None
                writer.release.set()
                # The actual granted_by FK KEY SHARE is blocked by deletion's A lock.
                _blocked_by(engine, waiter=writer, blocker=deletion.backend_pid)
                attempt_role.set()
            with pytest.raises(owner.StaffProvisioningError) as error:
                deleting.result(timeout=3)
            assert error.value.code.endswith(".deletion_busy")
        finally:
            writer.release.set()
            attempt_role.set()
        assert writing.result(timeout=15).changed
    with Session(engine) as db:
        assert db.get(SystemUser, scenario.user_id) is not None
        assert db.get(ApiKey, scenario.key_id) is not None
        grant = db.scalar(
            select(SystemUserPermission).where(
                SystemUserPermission.system_user_id == target_id
            )
        )
        assert grant is not None and grant.granted_by_system_user_id == scenario.user_id
        # The successful RBAC writer owns separate evidence. Refused deletion
        # must leave no evidence for its exact command or correlation, whatever
        # action/target a participant staged; migration evidence is unrelated.
        assert not db.scalars(
            select(AuditEvent).where(
                or_(
                    AuditEvent.details["command_id"].as_string()
                    == str(deletion_command.context.command_id),
                    AuditEvent.details["correlation_id"].as_string()
                    == str(deletion_command.context.correlation_id),
                )
            )
        ).all()
