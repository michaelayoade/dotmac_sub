"""Network participants on a real heads-migrated private PostgreSQL clone."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.network import (
    CPEDevice,
    DeviceStatus,
    IPAssignment,
    IPv4Address,
    IPv6Address,
    IPVersion,
)
from app.models.subscriber import Reseller, Subscriber
from app.services.audit_adapter import AuditActor
from app.services.cpe_subscriber_retirement import (
    RetireSubscriberCPECommand,
    stage_subscriber_cpe_retirement,
)
from app.services.ip_assignment_lifecycle import (
    ReleaseSubscriberIPAssignmentsCommand,
    stage_subscriber_ip_assignment_release,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

DEFINITION = OwnerCommandDefinition(
    "customer.account_deletion",
    "customer account hard deletion",
    "delete_customer_account",
)


@pytest.fixture
def network_engine(cloned_database: Callable[[str], URL]) -> Iterator[Engine]:
    engine = create_engine(cloned_database("heads"))
    try:
        yield engine
    finally:
        engine.dispose()


def _seed(db: Session) -> tuple[UUID, UUID, UUID, UUID, UUID]:
    subscriber = Subscriber(
        first_name="Network",
        last_name="Participant",
        email=f"{uuid4().hex}@example.test",
        is_active=False,
        reseller=Reseller(name=f"Network {uuid4().hex}"),
    )
    db.add(subscriber)
    db.flush()
    v4 = IPv4Address(address="192.0.2.71")
    v6 = IPv6Address(address="2001:db8::71")
    db.add_all([v4, v6])
    db.flush()
    cpe = CPEDevice(subscriber_id=subscriber.id, status=DeviceStatus.active)
    history = CPEDevice(subscriber_id=subscriber.id, status=DeviceStatus.maintenance)
    a4 = IPAssignment(
        subscriber_id=subscriber.id,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=v4.id,
        is_active=True,
    )
    a6 = IPAssignment(
        subscriber_id=subscriber.id,
        ip_version=IPVersion.ipv6,
        ipv6_address_id=v6.id,
        is_active=True,
    )
    db.add_all([cpe, history, a4, a6])
    db.flush()
    result = (subscriber.id, cpe.id, history.id, a4.id, a6.id)
    db.commit()
    return result


def _stage(db: Session, subscriber_id: UUID, context: CommandContext) -> None:
    assert (
        db.scalar(
            select(Subscriber)
            .where(Subscriber.id == subscriber_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        is not None
    )
    actor = AuditActor.user("network-participant-test")
    stage_subscriber_ip_assignment_release(
        db, command=ReleaseSubscriberIPAssignmentsCommand(context, subscriber_id, actor)
    )
    stage_subscriber_cpe_retirement(
        db, command=RetireSubscriberCPECommand(context, subscriber_id, actor)
    )


def _context() -> CommandContext:
    return CommandContext.system(
        actor="user:network-participant-test",
        scope="customers:delete",
        reason="isolated participant test",
    )


@pytest.mark.parametrize("fail_after_stage", [False, True])
@pytest.mark.parametrize("inactive_version", [None, IPVersion.ipv4, IPVersion.ipv6])
def test_release_retirement_and_retained_history_are_one_root(
    network_engine: Engine,
    fail_after_stage: bool,
    inactive_version: IPVersion | None,
) -> None:
    with Session(network_engine) as db:
        subscriber_id, cpe_id, history_id, a4_id, a6_id = _seed(db)
        inactive_id = None
        history_time = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
        if inactive_version is not None:
            inactive_id = a4_id if inactive_version is IPVersion.ipv4 else a6_id
            db.execute(
                update(IPAssignment)
                .where(IPAssignment.id == inactive_id)
                .values(
                    is_active=False, created_at=history_time, updated_at=history_time
                )
            )
            db.commit()
        context = _context()

        def assert_inactive_history() -> None:
            if inactive_id is None:
                return
            history = db.get(IPAssignment, inactive_id)
            db.refresh(history)
            assert history.is_active is False
            assert history.ip_version is inactive_version
            assert history.created_at == history_time
            assert history.updated_at == history_time
            assert history.subscriber_id == subscriber_id

        def operation() -> None:
            _stage(db, subscriber_id, context)
            assert_inactive_history()
            # A repeat stage must preserve the target state and history.
            _stage(db, subscriber_id, context)
            assert_inactive_history()
            if fail_after_stage:
                raise RuntimeError("forced later coordinator failure")
            db.execute(delete(Subscriber).where(Subscriber.id == subscriber_id))
            db.flush()

        if fail_after_stage:
            with pytest.raises(RuntimeError, match="forced later coordinator failure"):
                execute_owner_command(
                    db, definition=DEFINITION, context=context, operation=operation
                )
        else:
            execute_owner_command(
                db, definition=DEFINITION, context=context, operation=operation
            )
        db.expire_all()
        assert (db.get(Subscriber, subscriber_id) is not None) is fail_after_stage
        assert db.get(CPEDevice, cpe_id).status is (
            DeviceStatus.active if fail_after_stage else DeviceStatus.retired
        )
        assert db.get(CPEDevice, history_id).status is DeviceStatus.maintenance
        for row in [
            db.get(CPEDevice, cpe_id),
            db.get(CPEDevice, history_id),
            db.get(IPAssignment, a4_id),
            db.get(IPAssignment, a6_id),
        ]:
            assert row.subscriber_id == (subscriber_id if fail_after_stage else None)
        for assignment_id in (a4_id, a6_id):
            assignment = db.get(IPAssignment, assignment_id)
            assert assignment.is_active is (
                fail_after_stage and assignment_id != inactive_id
            )
        if inactive_id is not None:
            history = db.get(IPAssignment, inactive_id)
            assert history.ip_version is inactive_version
            assert history.created_at == history_time
            assert history.updated_at == history_time
            assert history.subscriber_id == (
                subscriber_id if fail_after_stage else None
            )


@pytest.mark.parametrize("child", ["ip", "cpe", "inactive_ip", "inactive_cpe"])
def test_physical_child_nowait_refuses_and_rolls_back_entire_root(
    network_engine: Engine, child: str
) -> None:
    with Session(network_engine) as db:
        subscriber_id, cpe_id, history_id, a4_id, _ = _seed(db)
        inactive_id = None
        if child == "inactive_ip":
            assignment = db.get(IPAssignment, a4_id)
            historical = IPAssignment(
                subscriber_id=subscriber_id,
                ip_version=IPVersion.ipv4,
                ipv4_address_id=assignment.ipv4_address_id,
                is_active=False,
            )
            db.add(historical)
            db.flush()
            inactive_id = historical.id
            db.commit()
        with Session(network_engine) as blocker:
            if child == "inactive_ip":
                model, row_id = IPAssignment, inactive_id
            elif child == "inactive_cpe":
                model, row_id = CPEDevice, history_id
            else:
                model, row_id = (
                    (IPAssignment, a4_id) if child == "ip" else (CPEDevice, cpe_id)
                )
            blocker.scalar(select(model).where(model.id == row_id).with_for_update())
            context = _context()
            with pytest.raises(OperationalError) as error:
                execute_owner_command(
                    db,
                    definition=DEFINITION,
                    context=context,
                    operation=lambda: _stage(db, subscriber_id, context),
                )
            assert error.value.orig.sqlstate == "55P03"
            assert not db.in_transaction()
            blocker.rollback()
        assert db.get(IPAssignment, a4_id).is_active is True
        assert db.get(CPEDevice, cpe_id).status is DeviceStatus.active
        assert db.get(Subscriber, subscriber_id) is not None


def test_authoritative_refresh_overrides_stale_identity_map(
    network_engine: Engine,
) -> None:
    with Session(network_engine, expire_on_commit=False) as db:
        subscriber_id, cpe_id, _, a4_id, _ = _seed(db)
        stale_ip = db.get(IPAssignment, a4_id)
        stale_cpe = db.get(CPEDevice, cpe_id)
        db.commit()
        with Session(network_engine) as writer:
            writer.execute(
                update(IPAssignment)
                .where(IPAssignment.id == a4_id)
                .values(is_active=False)
            )
            writer.execute(
                update(CPEDevice)
                .where(CPEDevice.id == cpe_id)
                .values(status=DeviceStatus.maintenance)
            )
            writer.commit()
        assert stale_ip.is_active is True
        assert stale_cpe.status is DeviceStatus.active
        context = _context()
        execute_owner_command(
            db,
            definition=DEFINITION,
            context=context,
            operation=lambda: _stage(db, subscriber_id, context),
        )
        assert stale_ip.is_active is False
        assert stale_cpe.status is DeviceStatus.maintenance
