"""Behavior contract for the manifest-verified public command boundary."""

from __future__ import annotations

import pytest
from dotmac_kernel.transactions import (
    ConflictSavepointProtocolError,
    conflict_savepoint,
    current_conflict_savepoint,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, SessionTransaction

from app.models.network_monitoring import DeviceProjection
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    OwnerCommandError,
    current_owner_root_transaction,
    execute_owner_command,
    execute_owner_savepoint,
)

_DEFINITION = OwnerCommandDefinition(
    owner="network.device_projection",
    concern="device_projections materialised table",
    name="test_projection_command",
)


def _context() -> CommandContext:
    return CommandContext.system(
        actor="pytest:owner_commands",
        scope="network:test",
        reason="verify owner command transaction semantics",
    )


def _projection(source_id: str) -> DeviceProjection:
    return DeviceProjection(
        device_type="test",
        source_id=source_id,
        operational_status="not_working",
    )


def _count(db_session) -> int:
    return int(
        db_session.scalar(select(func.count()).select_from(DeviceProjection)) or 0
    )


def test_success_commits_before_return(db_session) -> None:
    def operation() -> str:
        db_session.add(_projection("committed"))
        return "complete"

    result = execute_owner_command(
        db_session,
        definition=_DEFINITION,
        context=_context(),
        operation=operation,
    )

    assert result == "complete"
    assert not db_session.in_transaction()
    assert _count(db_session) == 1


def test_public_root_observation_tracks_executor_lifetime(db_session: Session) -> None:
    with pytest.raises(OwnerCommandError, match="No active owner command definition"):
        current_owner_root_transaction(db_session)

    def observe() -> SessionTransaction:
        root = current_owner_root_transaction(db_session)
        assert root is db_session.get_transaction()
        assert root.is_active and root.parent is None and not root.nested
        db_session.add(_projection("root-observation"))
        db_session.flush()
        assert current_owner_root_transaction(db_session) is root
        return root

    root = execute_owner_command(
        db_session, definition=_DEFINITION, context=_context(), operation=observe
    )
    assert not root.is_active
    with pytest.raises(OwnerCommandError, match="No active owner command definition"):
        current_owner_root_transaction(db_session)


def test_public_root_observation_refuses_owner_savepoint(db_session: Session) -> None:
    def operation() -> None:
        def participant() -> None:
            current_owner_root_transaction(db_session)

        with pytest.raises(OwnerCommandError) as captured:
            execute_owner_savepoint(db_session, operation=participant)
        assert captured.value.code.endswith(".nested_transaction_completion")
        assert (
            current_owner_root_transaction(db_session) is db_session.get_transaction()
        )

    execute_owner_command(
        db_session, definition=_DEFINITION, context=_context(), operation=operation
    )
    assert not db_session.in_transaction()


def test_failure_rolls_back_before_raising(db_session) -> None:
    def operation() -> None:
        db_session.add(_projection("rolled-back"))
        db_session.flush()
        raise RuntimeError("test failure")

    with pytest.raises(RuntimeError, match="test failure"):
        execute_owner_command(
            db_session,
            definition=_DEFINITION,
            context=_context(),
            operation=operation,
        )

    assert not db_session.in_transaction()
    assert _count(db_session) == 0


def test_active_caller_transaction_fails_closed_and_is_cleared(db_session) -> None:
    db_session.execute(select(DeviceProjection.id))

    with pytest.raises(OwnerCommandError) as captured:
        execute_owner_command(
            db_session,
            definition=_DEFINITION,
            context=_context(),
            operation=lambda: None,
        )

    assert captured.value.code == "network.device_projection.active_caller_transaction"
    assert not db_session.in_transaction()


def test_nested_public_command_rolls_back_outer_command(db_session) -> None:
    def outer_operation() -> None:
        db_session.add(_projection("nested"))
        execute_owner_command(
            db_session,
            definition=_DEFINITION,
            context=_context(),
            operation=lambda: None,
        )

    with pytest.raises(OwnerCommandError) as captured:
        execute_owner_command(
            db_session,
            definition=_DEFINITION,
            context=_context(),
            operation=outer_operation,
        )

    assert captured.value.code == "network.device_projection.nested_owner_command"
    assert not db_session.in_transaction()
    assert _count(db_session) == 0


def test_helper_commit_is_rejected_before_state_is_committed(db_session) -> None:
    def operation() -> None:
        db_session.add(_projection("helper-commit"))
        db_session.commit()

    with pytest.raises(OwnerCommandError) as captured:
        execute_owner_command(
            db_session,
            definition=_DEFINITION,
            context=_context(),
            operation=operation,
        )

    assert (
        captured.value.code == "network.device_projection.nested_transaction_completion"
    )
    assert not db_session.in_transaction()
    assert _count(db_session) == 0


def test_helper_rollback_cannot_return_a_successful_outcome(db_session) -> None:
    def operation() -> None:
        db_session.add(_projection("helper-rollback"))
        db_session.flush()
        db_session.rollback()

    with pytest.raises(OwnerCommandError) as captured:
        execute_owner_command(
            db_session,
            definition=_DEFINITION,
            context=_context(),
            operation=operation,
        )

    assert (
        captured.value.code == "network.device_projection.nested_transaction_completion"
    )
    assert not db_session.in_transaction()
    assert _count(db_session) == 0


def test_open_savepoint_rolls_back_the_owner_transaction(db_session) -> None:
    savepoint = None

    def operation() -> None:
        nonlocal savepoint
        db_session.add(_projection("open-savepoint"))
        savepoint = db_session.begin_nested()

    with pytest.raises(OwnerCommandError) as captured:
        execute_owner_command(
            db_session,
            definition=_DEFINITION,
            context=_context(),
            operation=operation,
        )

    assert (
        captured.value.code == "network.device_projection.nested_transaction_completion"
    )
    assert savepoint is not None
    assert not savepoint.is_active
    assert not db_session.in_transaction()
    assert _count(db_session) == 0


def test_helper_savepoint_rollback_cannot_hide_partial_failure(db_session) -> None:
    def operation() -> None:
        savepoint = db_session.begin_nested()
        db_session.add(_projection("savepoint-rollback"))
        db_session.flush()
        savepoint.rollback()

    with pytest.raises(OwnerCommandError) as captured:
        execute_owner_command(
            db_session,
            definition=_DEFINITION,
            context=_context(),
            operation=operation,
        )

    assert (
        captured.value.code == "network.device_projection.nested_transaction_completion"
    )
    assert not db_session.in_transaction()
    assert _count(db_session) == 0


def test_authorized_owner_savepoint_can_commit_an_optional_step(db_session) -> None:
    def operation() -> None:
        execute_owner_savepoint(
            db_session,
            lambda: db_session.add(_projection("isolated-success")),
        )

    execute_owner_command(
        db_session,
        definition=_DEFINITION,
        context=_context(),
        operation=operation,
    )

    assert _count(db_session) == 1


def test_authorized_owner_savepoint_can_stage_failure_evidence(db_session) -> None:
    def fail_optional_step() -> None:
        db_session.add(_projection("discarded-optional-write"))
        db_session.flush()
        raise ValueError("optional step rejected")

    def operation() -> None:
        try:
            execute_owner_savepoint(db_session, fail_optional_step)
        except ValueError:
            db_session.add(_projection("durable-failure-evidence"))

    execute_owner_command(
        db_session,
        definition=_DEFINITION,
        context=_context(),
        operation=operation,
    )

    source_ids = set(db_session.scalars(select(DeviceProjection.source_id)).all())
    assert source_ids == {"durable-failure-evidence"}


def test_uncontracted_owner_cannot_use_runtime_boundary(db_session) -> None:
    uncontracted = OwnerCommandDefinition(
        owner="events.store",
        concern="event persistence",
        name="invalid_event_store_command",
    )

    with pytest.raises(OwnerCommandError) as captured:
        execute_owner_command(
            db_session,
            definition=uncontracted,
            context=_context(),
            operation=lambda: None,
        )

    assert captured.value.code == "events.store.command_contract_violation"
    assert not db_session.in_transaction()


def test_owner_savepoint_recovers_real_flush_constraint_failure(
    db_session: Session,
) -> None:
    def insert_duplicate() -> None:
        db_session.add(_projection("duplicate-source"))
        db_session.flush()

    def operation() -> None:
        db_session.add(_projection("duplicate-source"))
        db_session.flush()
        with pytest.raises(IntegrityError):
            execute_owner_savepoint(db_session, insert_duplicate)
        assert db_session.is_active
        assert not db_session.in_nested_transaction()
        db_session.add(_projection("after-conflict"))

    execute_owner_command(
        db_session, definition=_DEFINITION, context=_context(), operation=operation
    )

    assert not db_session.in_transaction()
    assert set(db_session.scalars(select(DeviceProjection.source_id))) == {
        "duplicate-source",
        "after-conflict",
    }


@pytest.mark.parametrize(
    "action", ["commit", "rollback", "savepoint_commit", "savepoint_rollback"]
)
def test_owner_savepoint_still_rejects_participant_transaction_completion(
    db_session: Session,
    action: str,
) -> None:
    def participant() -> None:
        db_session.add(_projection("illegal-participant"))
        db_session.flush()
        if action == "commit":
            db_session.commit()
        elif action == "rollback":
            db_session.rollback()
        else:
            nested = db_session.get_nested_transaction()
            assert nested is not None
            if action == "savepoint_commit":
                nested.commit()
            else:
                nested.rollback()

    with pytest.raises(OwnerCommandError):
        execute_owner_command(
            db_session,
            definition=_DEFINITION,
            context=_context(),
            operation=lambda: execute_owner_savepoint(db_session, participant),
        )

    assert not db_session.in_transaction()
    assert _count(db_session) == 0


def test_swallowed_optional_commit_attempt_still_aborts_owner(
    db_session: Session,
) -> None:
    def participant() -> None:
        db_session.add(_projection("swallowed-illegal-commit"))
        with pytest.raises(OwnerCommandError):
            db_session.commit()

    def operation() -> None:
        with pytest.raises(OwnerCommandError):
            execute_owner_savepoint(db_session, participant)
        db_session.add(_projection("cannot-hide-boundary-violation"))

    with pytest.raises(OwnerCommandError):
        execute_owner_command(
            db_session, definition=_DEFINITION, context=_context(), operation=operation
        )
    assert not db_session.in_transaction()
    assert not any(key.startswith("_dotmac_") for key in db_session.info)
    assert _count(db_session) == 0


def test_optional_failure_preserves_exception_identity_and_cause(
    db_session: Session,
) -> None:
    first = ValueError("optional business failure")
    cause = LookupError("original failure cause")

    def participant() -> None:
        db_session.add(_projection("discarded-with-diagnostic"))
        db_session.flush()
        raise first from cause

    def operation() -> None:
        with pytest.raises(ValueError) as captured:
            execute_owner_savepoint(db_session, participant)
        assert captured.value is first
        assert captured.value.__cause__ is cause
        db_session.add(_projection("retained-diagnostic-evidence"))

    execute_owner_command(
        db_session, definition=_DEFINITION, context=_context(), operation=operation
    )
    assert not db_session.in_transaction()
    assert not any(key.startswith("_dotmac_") for key in db_session.info)
    assert set(db_session.scalars(select(DeviceProjection.source_id))) == {
        "retained-diagnostic-evidence"
    }


@pytest.mark.parametrize("flush_in_body", [False, True])
def test_kernel_conflict_savepoint_preserves_owner_after_unique_failure(
    db_session: Session, flush_in_body: bool
) -> None:
    def operation() -> None:
        root = current_owner_root_transaction(db_session)
        db_session.add(_projection("kernel-conflict"))
        db_session.flush()
        with pytest.raises(IntegrityError):
            with conflict_savepoint(db_session):
                db_session.add(_projection("kernel-conflict"))
                if flush_in_body:
                    db_session.flush()
        assert current_owner_root_transaction(db_session) is root
        assert current_conflict_savepoint(db_session) is None
        db_session.add(_projection("kernel-recovery"))

    execute_owner_command(
        db_session, definition=_DEFINITION, context=_context(), operation=operation
    )
    assert not db_session.in_transaction()
    assert set(db_session.scalars(select(DeviceProjection.source_id))) == {
        "kernel-conflict",
        "kernel-recovery",
    }


def test_kernel_helper_inside_optional_step_restores_optional_child(
    db_session: Session,
) -> None:
    def participant() -> None:
        optional = db_session.get_nested_transaction()
        assert optional is not None
        with conflict_savepoint(db_session):
            db_session.add(_projection("kernel-optional"))
        assert db_session.get_nested_transaction() is optional
        assert optional.is_active
        assert current_conflict_savepoint(db_session) is None

    execute_owner_command(
        db_session,
        definition=_DEFINITION,
        context=_context(),
        operation=lambda: execute_owner_savepoint(db_session, participant),
    )
    assert not db_session.in_transaction()
    assert _count(db_session) == 1


def test_kernel_conflict_inside_optional_step_retains_failure_evidence(
    db_session: Session,
) -> None:
    def participant() -> None:
        optional = db_session.get_nested_transaction()
        with pytest.raises(IntegrityError):
            with conflict_savepoint(db_session):
                db_session.add(_projection("kernel-duplicate"))
                db_session.flush()
        assert db_session.get_nested_transaction() is optional
        raise ValueError("optional domain consequence refused")

    def operation() -> None:
        db_session.add(_projection("kernel-duplicate"))
        db_session.flush()
        with pytest.raises(ValueError, match="optional domain consequence refused"):
            execute_owner_savepoint(db_session, participant)
        db_session.add(_projection("kernel-failure-evidence"))

    execute_owner_command(
        db_session, definition=_DEFINITION, context=_context(), operation=operation
    )
    assert not db_session.in_transaction()
    assert set(db_session.scalars(select(DeviceProjection.source_id))) == {
        "kernel-duplicate",
        "kernel-failure-evidence",
    }


def test_nested_kernel_helpers_restore_observation_without_granting_admission(
    db_session: Session,
) -> None:
    def operation() -> None:
        root = current_owner_root_transaction(db_session)
        with conflict_savepoint(db_session):
            outer = current_conflict_savepoint(db_session)
            assert outer is not None and outer.root is root
            with conflict_savepoint(db_session):
                inner = current_conflict_savepoint(db_session)
                assert inner is not None and inner.root is root
                assert inner.savepoint is not outer.savepoint
                with pytest.raises(OwnerCommandError):
                    current_owner_root_transaction(db_session)
                with pytest.raises(OwnerCommandError, match="cannot be nested"):
                    execute_owner_savepoint(db_session, lambda: None)
                db_session.add(_projection("kernel-nested"))
            assert current_conflict_savepoint(db_session) is outer
        assert current_owner_root_transaction(db_session) is root
        assert current_conflict_savepoint(db_session) is None

    execute_owner_command(
        db_session, definition=_DEFINITION, context=_context(), operation=operation
    )
    assert not db_session.in_transaction()
    assert _count(db_session) == 1


@pytest.mark.parametrize("completion", ["commit", "rollback"])
def test_kernel_body_completion_violation_cannot_be_swallowed(
    db_session: Session, completion: str
) -> None:
    def operation() -> None:
        try:
            with conflict_savepoint(db_session):
                db_session.add(_projection("kernel-illegal"))
                db_session.flush()
                child = db_session.get_nested_transaction()
                assert child is not None
                try:
                    if completion == "commit":
                        child.commit()
                    else:
                        child.rollback()
                except OwnerCommandError:
                    pass
        except (OwnerCommandError, ConflictSavepointProtocolError):
            pass

    with pytest.raises(OwnerCommandError):
        execute_owner_command(
            db_session, definition=_DEFINITION, context=_context(), operation=operation
        )
    assert not db_session.in_transaction()
    assert not any(key.startswith("_dotmac_") for key in db_session.info)
    assert _count(db_session) == 0
