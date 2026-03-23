"""Tests for the network operation tracking service.

Covers:
- Operation creation and field defaults
- Status lifecycle transitions (pending → running → succeeded/failed)
- Correlation key dedup (409 on active duplicate)
- list_for_device query and ordering
- Parent/child status derivation
- tracked_operation context manager
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services.network_operations import (
    network_operations,
    run_tracked_action,
    tracked_operation,
)
from app.tasks.network_operations import cleanup_old_operations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_target_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Creation tests
# ---------------------------------------------------------------------------


class TestNetworkOperationCreate:
    def test_create_operation_defaults(self, db_session):
        """Creating an operation sets expected defaults."""
        target_id = _make_target_id()
        op = network_operations.start(
            db_session,
            NetworkOperationType.olt_ont_sync,
            NetworkOperationTargetType.olt,
            target_id,
        )
        assert op.id is not None
        assert op.status == NetworkOperationStatus.pending
        assert op.operation_type == NetworkOperationType.olt_ont_sync
        assert op.target_type == NetworkOperationTargetType.olt
        assert str(op.target_id) == target_id
        assert op.retry_count == 0
        assert op.max_retries == 3
        assert op.started_at is None
        assert op.completed_at is None
        assert op.error is None

    def test_create_with_payload_and_initiator(self, db_session):
        """Optional fields are stored correctly."""
        target_id = _make_target_id()
        payload = {"profile_id": "abc", "dry_run": False}
        op = network_operations.start(
            db_session,
            NetworkOperationType.ont_provision,
            NetworkOperationTargetType.ont,
            target_id,
            input_payload=payload,
            initiated_by="admin:alice",
            correlation_key="ont_provision:test",
        )
        assert op.input_payload == payload
        assert op.initiated_by == "admin:alice"
        assert op.correlation_key == "ont_provision:test"


# ---------------------------------------------------------------------------
# Lifecycle transition tests
# ---------------------------------------------------------------------------


class TestNetworkOperationLifecycle:
    def test_pending_to_running(self, db_session):
        """mark_running sets status and started_at."""
        op = network_operations.start(
            db_session,
            NetworkOperationType.ont_reboot,
            NetworkOperationTargetType.ont,
            _make_target_id(),
        )
        updated = network_operations.mark_running(db_session, str(op.id))
        assert updated.status == NetworkOperationStatus.running
        assert updated.started_at is not None

    def test_running_to_succeeded(self, db_session):
        """mark_succeeded sets status, completed_at, and output_payload."""
        op = network_operations.start(
            db_session,
            NetworkOperationType.olt_ont_sync,
            NetworkOperationTargetType.olt,
            _make_target_id(),
        )
        network_operations.mark_running(db_session, str(op.id))
        payload = {"discovered": 12, "created": 3, "updated": 9}
        updated = network_operations.mark_succeeded(
            db_session, str(op.id), output_payload=payload
        )
        assert updated.status == NetworkOperationStatus.succeeded
        assert updated.completed_at is not None
        assert updated.output_payload == payload

    def test_running_to_failed(self, db_session):
        """mark_failed sets status, completed_at, and error."""
        op = network_operations.start(
            db_session,
            NetworkOperationType.ont_send_conn_request,
            NetworkOperationTargetType.ont,
            _make_target_id(),
        )
        network_operations.mark_running(db_session, str(op.id))
        updated = network_operations.mark_failed(
            db_session, str(op.id), "Connection refused"
        )
        assert updated.status == NetworkOperationStatus.failed
        assert updated.completed_at is not None
        assert updated.error == "Connection refused"

    def test_running_to_failed_with_payload(self, db_session):
        """mark_failed stores both error and output_payload."""
        op = network_operations.start(
            db_session,
            NetworkOperationType.olt_ont_sync,
            NetworkOperationTargetType.olt,
            _make_target_id(),
        )
        network_operations.mark_running(db_session, str(op.id))
        partial = {"discovered": 10, "created": 8}
        updated = network_operations.mark_failed(
            db_session, str(op.id), "Failed on ONT 11",
            output_payload=partial,
        )
        assert updated.status == NetworkOperationStatus.failed
        assert updated.error == "Failed on ONT 11"
        assert updated.output_payload == partial

    def test_mark_waiting(self, db_session):
        """mark_waiting sets status and waiting_reason."""
        op = network_operations.start(
            db_session,
            NetworkOperationType.tr069_bootstrap,
            NetworkOperationTargetType.ont,
            _make_target_id(),
        )
        network_operations.mark_running(db_session, str(op.id))
        updated = network_operations.mark_waiting(
            db_session, str(op.id), "next_inform"
        )
        assert updated.status == NetworkOperationStatus.waiting
        assert updated.waiting_reason == "next_inform"

    def test_mark_canceled(self, db_session):
        """mark_canceled sets status and completed_at."""
        op = network_operations.start(
            db_session,
            NetworkOperationType.ont_reboot,
            NetworkOperationTargetType.ont,
            _make_target_id(),
        )
        updated = network_operations.mark_canceled(db_session, str(op.id))
        assert updated.status == NetworkOperationStatus.canceled
        assert updated.completed_at is not None

    def test_get_nonexistent_raises_404(self, db_session):
        """Fetching a nonexistent operation raises 404."""
        with pytest.raises(HTTPException) as exc_info:
            network_operations.get(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_mark_running_nonexistent_raises_404(self, db_session):
        """mark_running raises 404 for nonexistent operation."""
        with pytest.raises(HTTPException) as exc_info:
            network_operations.mark_running(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_mark_succeeded_nonexistent_raises_404(self, db_session):
        """mark_succeeded raises 404 for nonexistent operation."""
        with pytest.raises(HTTPException) as exc_info:
            network_operations.mark_succeeded(db_session, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404

    def test_mark_failed_nonexistent_raises_404(self, db_session):
        """mark_failed raises 404 for nonexistent operation."""
        with pytest.raises(HTTPException) as exc_info:
            network_operations.mark_failed(db_session, str(uuid.uuid4()), "err")
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# State transition guard tests
# ---------------------------------------------------------------------------


class TestStateTransitionGuards:
    def _make_succeeded_op(self, db_session):
        """Create an operation in succeeded status."""
        op = network_operations.start(
            db_session,
            NetworkOperationType.ont_reboot,
            NetworkOperationTargetType.ont,
            _make_target_id(),
        )
        network_operations.mark_running(db_session, str(op.id))
        network_operations.mark_succeeded(db_session, str(op.id))
        return op

    def test_cannot_mark_succeeded_op_as_running(self, db_session):
        """Terminal status rejects further transitions."""
        op = self._make_succeeded_op(db_session)
        with pytest.raises(HTTPException) as exc_info:
            network_operations.mark_running(db_session, str(op.id))
        assert exc_info.value.status_code == 409

    def test_cannot_mark_succeeded_op_as_failed(self, db_session):
        """Cannot overwrite a succeeded operation with failure."""
        op = self._make_succeeded_op(db_session)
        with pytest.raises(HTTPException) as exc_info:
            network_operations.mark_failed(db_session, str(op.id), "late error")
        assert exc_info.value.status_code == 409

    def test_cannot_mark_failed_op_as_succeeded(self, db_session):
        """Cannot overwrite a failed operation with success."""
        op = network_operations.start(
            db_session,
            NetworkOperationType.ont_reboot,
            NetworkOperationTargetType.ont,
            _make_target_id(),
        )
        network_operations.mark_running(db_session, str(op.id))
        network_operations.mark_failed(db_session, str(op.id), "error")
        with pytest.raises(HTTPException) as exc_info:
            network_operations.mark_succeeded(db_session, str(op.id))
        assert exc_info.value.status_code == 409

    def test_cannot_mark_canceled_op_as_running(self, db_session):
        """Cannot re-activate a canceled operation."""
        op = network_operations.start(
            db_session,
            NetworkOperationType.ont_reboot,
            NetworkOperationTargetType.ont,
            _make_target_id(),
        )
        network_operations.mark_canceled(db_session, str(op.id))
        with pytest.raises(HTTPException) as exc_info:
            network_operations.mark_running(db_session, str(op.id))
        assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# Correlation key dedup tests
# ---------------------------------------------------------------------------


class TestCorrelationKeyDedup:
    def test_duplicate_active_key_rejected(self, db_session):
        """A second pending/running op with the same key raises 409."""
        target_id = _make_target_id()
        network_operations.start(
            db_session,
            NetworkOperationType.olt_ont_sync,
            NetworkOperationTargetType.olt,
            target_id,
            correlation_key="olt_sync:test1",
        )
        db_session.flush()

        with pytest.raises(HTTPException) as exc_info:
            network_operations.start(
                db_session,
                NetworkOperationType.olt_ont_sync,
                NetworkOperationTargetType.olt,
                target_id,
                correlation_key="olt_sync:test1",
            )
        assert exc_info.value.status_code == 409

    def test_completed_key_allows_new_operation(self, db_session):
        """A completed op's correlation key can be reused."""
        target_id = _make_target_id()
        op1 = network_operations.start(
            db_session,
            NetworkOperationType.olt_ont_sync,
            NetworkOperationTargetType.olt,
            target_id,
            correlation_key="olt_sync:test2",
        )
        network_operations.mark_running(db_session, str(op1.id))
        network_operations.mark_succeeded(db_session, str(op1.id))
        db_session.flush()

        # Should not raise — previous op is succeeded
        op2 = network_operations.start(
            db_session,
            NetworkOperationType.olt_ont_sync,
            NetworkOperationTargetType.olt,
            target_id,
            correlation_key="olt_sync:test2",
        )
        assert op2.id != op1.id

    def test_waiting_op_blocks_duplicate(self, db_session):
        """A waiting operation blocks duplicate creation."""
        target_id = _make_target_id()
        op = network_operations.start(
            db_session,
            NetworkOperationType.tr069_bootstrap,
            NetworkOperationTargetType.ont,
            target_id,
            correlation_key="tr069:test_wait",
        )
        network_operations.mark_running(db_session, str(op.id))
        network_operations.mark_waiting(db_session, str(op.id), "next_inform")
        db_session.flush()

        with pytest.raises(HTTPException) as exc_info:
            network_operations.start(
                db_session,
                NetworkOperationType.tr069_bootstrap,
                NetworkOperationTargetType.ont,
                target_id,
                correlation_key="tr069:test_wait",
            )
        assert exc_info.value.status_code == 409

    def test_no_key_allows_concurrent(self, db_session):
        """Operations without correlation keys are always allowed."""
        target_id = _make_target_id()
        op1 = network_operations.start(
            db_session,
            NetworkOperationType.ont_reboot,
            NetworkOperationTargetType.ont,
            target_id,
        )
        op2 = network_operations.start(
            db_session,
            NetworkOperationType.ont_reboot,
            NetworkOperationTargetType.ont,
            target_id,
        )
        assert op1.id != op2.id


# ---------------------------------------------------------------------------
# list_for_device tests
# ---------------------------------------------------------------------------


class TestListForDevice:
    def test_returns_operations_for_target(self, db_session):
        """list_for_device returns only ops matching target type+id."""
        target_id = _make_target_id()
        other_id = _make_target_id()

        # Create ops for target and a different device
        network_operations.start(
            db_session,
            NetworkOperationType.ont_reboot,
            NetworkOperationTargetType.ont,
            target_id,
        )
        network_operations.start(
            db_session,
            NetworkOperationType.ont_reboot,
            NetworkOperationTargetType.ont,
            target_id,
        )
        network_operations.start(
            db_session,
            NetworkOperationType.ont_reboot,
            NetworkOperationTargetType.ont,
            other_id,
        )
        db_session.flush()

        ops = network_operations.list_for_device(
            db_session, NetworkOperationTargetType.ont, target_id
        )
        assert len(ops) == 2
        for op in ops:
            assert str(op.target_id) == target_id

    def test_excludes_child_operations(self, db_session):
        """list_for_device returns only top-level (parentless) operations."""
        target_id = _make_target_id()
        parent = network_operations.start(
            db_session,
            NetworkOperationType.ont_provision,
            NetworkOperationTargetType.ont,
            target_id,
        )
        # Create a child op
        network_operations.start(
            db_session,
            NetworkOperationType.ont_set_pppoe,
            NetworkOperationTargetType.ont,
            target_id,
            parent_id=str(parent.id),
        )
        db_session.flush()

        ops = network_operations.list_for_device(
            db_session, NetworkOperationTargetType.ont, target_id
        )
        assert len(ops) == 1
        assert ops[0].id == parent.id

    def test_pagination(self, db_session):
        """list_for_device respects limit and offset."""
        target_id = _make_target_id()
        for _ in range(5):
            network_operations.start(
                db_session,
                NetworkOperationType.ont_reboot,
                NetworkOperationTargetType.ont,
                target_id,
            )
        db_session.flush()

        page = network_operations.list_for_device(
            db_session, NetworkOperationTargetType.ont, target_id, limit=2, offset=0
        )
        assert len(page) == 2


# ---------------------------------------------------------------------------
# Parent/child status derivation tests
# ---------------------------------------------------------------------------


class TestParentStatusDerivation:
    def _make_parent_with_children(self, db_session, child_statuses):
        """Helper to create a parent with children in specified statuses."""
        target_id = _make_target_id()
        parent = network_operations.start(
            db_session,
            NetworkOperationType.ont_provision,
            NetworkOperationTargetType.ont,
            target_id,
        )
        for status in child_statuses:
            child = network_operations.start(
                db_session,
                NetworkOperationType.ont_set_pppoe,
                NetworkOperationTargetType.ont,
                target_id,
                parent_id=str(parent.id),
            )
            if status == NetworkOperationStatus.running:
                network_operations.mark_running(db_session, str(child.id))
            elif status == NetworkOperationStatus.succeeded:
                network_operations.mark_running(db_session, str(child.id))
                network_operations.mark_succeeded(db_session, str(child.id))
            elif status == NetworkOperationStatus.failed:
                network_operations.mark_running(db_session, str(child.id))
                network_operations.mark_failed(
                    db_session, str(child.id), "test error"
                )
            elif status == NetworkOperationStatus.waiting:
                network_operations.mark_running(db_session, str(child.id))
                network_operations.mark_waiting(
                    db_session, str(child.id), "test wait"
                )
        db_session.flush()
        return parent

    def test_all_succeeded(self, db_session):
        """Parent is succeeded when all children are succeeded."""
        parent = self._make_parent_with_children(
            db_session,
            [NetworkOperationStatus.succeeded, NetworkOperationStatus.succeeded],
        )
        updated = network_operations.update_parent_status(
            db_session, str(parent.id)
        )
        assert updated.status == NetworkOperationStatus.succeeded
        assert updated.completed_at is not None

    def test_any_running(self, db_session):
        """Parent is running when any child is running."""
        parent = self._make_parent_with_children(
            db_session,
            [NetworkOperationStatus.succeeded, NetworkOperationStatus.running],
        )
        updated = network_operations.update_parent_status(
            db_session, str(parent.id)
        )
        assert updated.status == NetworkOperationStatus.running
        assert updated.completed_at is None


    def test_any_failed(self, db_session):
        """Parent is failed when any child is failed (and none running)."""
        parent = self._make_parent_with_children(
            db_session,
            [NetworkOperationStatus.succeeded, NetworkOperationStatus.failed],
        )
        updated = network_operations.update_parent_status(
            db_session, str(parent.id)
        )
        assert updated.status == NetworkOperationStatus.failed
        assert updated.completed_at is not None

    def test_any_pending(self, db_session):
        """Parent is pending when any child is pending (and none running/failed)."""
        parent = self._make_parent_with_children(
            db_session,
            [NetworkOperationStatus.succeeded, NetworkOperationStatus.pending],
        )
        updated = network_operations.update_parent_status(
            db_session, str(parent.id)
        )
        assert updated.status == NetworkOperationStatus.pending
        assert updated.completed_at is None

    def test_any_waiting(self, db_session):
        """Parent is waiting when children are succeeded + waiting."""
        parent = self._make_parent_with_children(
            db_session,
            [NetworkOperationStatus.succeeded, NetworkOperationStatus.waiting],
        )
        updated = network_operations.update_parent_status(
            db_session, str(parent.id)
        )
        assert updated.status == NetworkOperationStatus.waiting
        assert updated.completed_at is None

    def test_no_children_returns_unchanged(self, db_session):
        """Parent with no children is returned unchanged."""
        target_id = _make_target_id()
        parent = network_operations.start(
            db_session,
            NetworkOperationType.ont_provision,
            NetworkOperationTargetType.ont,
            target_id,
        )
        db_session.flush()
        updated = network_operations.update_parent_status(
            db_session, str(parent.id)
        )
        assert updated.status == NetworkOperationStatus.pending
        assert updated.completed_at is None

    def test_nonexistent_parent_raises_404(self, db_session):
        """update_parent_status raises 404 for nonexistent parent."""
        with pytest.raises(HTTPException) as exc_info:
            network_operations.update_parent_status(
                db_session, str(uuid.uuid4())
            )
        assert exc_info.value.status_code == 404


class TestCleanupOldOperations:
    def test_preserves_terminal_parent_with_active_child(self, db_session, monkeypatch):
        """Cleanup must not purge terminal parents while active children still exist."""
        target_id = _make_target_id()
        parent = network_operations.start(
            db_session,
            NetworkOperationType.ont_provision,
            NetworkOperationTargetType.ont,
            target_id,
        )
        child = network_operations.start(
            db_session,
            NetworkOperationType.ont_set_pppoe,
            NetworkOperationTargetType.ont,
            target_id,
            parent_id=str(parent.id),
        )
        parent_id = parent.id
        child_id = child.id
        network_operations.mark_running(db_session, str(child_id))
        network_operations.mark_failed(db_session, str(parent_id), "prior failure")
        parent.completed_at = datetime.now(UTC) - timedelta(days=120)
        db_session.flush()

        monkeypatch.setattr(
            "app.tasks.network_operations.SessionLocal",
            lambda: db_session,
        )

        result = cleanup_old_operations()

        assert result["purged"] == 0
        assert db_session.get(NetworkOperation, parent_id) is not None
        assert db_session.get(NetworkOperation, child_id) is not None


# ---------------------------------------------------------------------------
# tracked_operation context manager tests
# ---------------------------------------------------------------------------


class TestTrackedOperationContextManager:
    def test_success_path(self, db_session):
        """Context manager marks operation succeeded on normal exit."""
        target_id = _make_target_id()
        with tracked_operation(
            db_session,
            NetworkOperationType.ont_reboot,
            NetworkOperationTargetType.ont,
            target_id,
        ) as op:
            op.output_payload = {"result": "ok"}

        refreshed = db_session.get(NetworkOperation, op.id)
        assert refreshed.status == NetworkOperationStatus.succeeded

    def test_failure_path(self, db_session):
        """Context manager marks operation failed on exception and re-raises."""
        target_id = _make_target_id()
        with pytest.raises(ValueError, match="boom"):
            with tracked_operation(
                db_session,
                NetworkOperationType.ont_reboot,
                NetworkOperationTargetType.ont,
                target_id,
            ) as op:
                raise ValueError("boom")

        refreshed = db_session.get(NetworkOperation, op.id)
        assert refreshed.status == NetworkOperationStatus.failed
        assert refreshed.error == "boom"


# ---------------------------------------------------------------------------
# run_tracked_action tests
# ---------------------------------------------------------------------------


class _FakeResult:
    """Minimal ActionResult stand-in for testing."""

    def __init__(self, success: bool, message: str = "", data: object = None):
        self.success = success
        self.message = message
        self.data = data


class TestRunTrackedAction:
    def test_success_path(self, db_session):
        """Successful action marks operation as succeeded."""
        target_id = _make_target_id()
        result = run_tracked_action(
            db_session,
            NetworkOperationType.ont_reboot,
            NetworkOperationTargetType.ont,
            target_id,
            lambda: _FakeResult(success=True, message="OK", data={"rebooted": True}),
            correlation_key=f"test_success:{target_id}",
        )
        assert result.success is True

        ops = network_operations.list_for_device(
            db_session, NetworkOperationTargetType.ont, target_id
        )
        assert len(ops) == 1
        assert ops[0].status == NetworkOperationStatus.succeeded

    def test_logical_failure_path(self, db_session):
        """Action returning success=False marks operation as failed."""
        target_id = _make_target_id()
        result = run_tracked_action(
            db_session,
            NetworkOperationType.ont_reboot,
            NetworkOperationTargetType.ont,
            target_id,
            lambda: _FakeResult(success=False, message="Device unreachable"),
        )
        assert result.success is False

        ops = network_operations.list_for_device(
            db_session, NetworkOperationTargetType.ont, target_id
        )
        assert len(ops) == 1
        assert ops[0].status == NetworkOperationStatus.failed
        assert ops[0].error == "Device unreachable"

    def test_409_conflict_returns_failure(self, db_session):
        """Duplicate correlation key returns ActionResult(success=False)."""
        target_id = _make_target_id()
        # Create an active operation first
        network_operations.start(
            db_session,
            NetworkOperationType.ont_reboot,
            NetworkOperationTargetType.ont,
            target_id,
            correlation_key="test_409_conflict",
        )
        db_session.flush()

        result = run_tracked_action(
            db_session,
            NetworkOperationType.ont_reboot,
            NetworkOperationTargetType.ont,
            target_id,
            lambda: _FakeResult(success=True),
            correlation_key="test_409_conflict",
        )
        assert result.success is False
        assert "already in progress" in result.message

    def test_exception_path(self, db_session):
        """Action raising exception marks op as failed and re-raises."""
        target_id = _make_target_id()

        def failing_action():
            raise ConnectionError("SSH timeout")

        with pytest.raises(ConnectionError, match="SSH timeout"):
            run_tracked_action(
                db_session,
                NetworkOperationType.ont_reboot,
                NetworkOperationTargetType.ont,
                target_id,
                failing_action,
            )

        ops = network_operations.list_for_device(
            db_session, NetworkOperationTargetType.ont, target_id
        )
        assert len(ops) == 1
        assert ops[0].status == NetworkOperationStatus.failed
        assert "SSH timeout" in ops[0].error


# ---------------------------------------------------------------------------
# update_parent_status re-derivation from terminal tests
# ---------------------------------------------------------------------------


class TestParentReDerivation:
    def test_parent_can_re_derive_from_terminal(self, db_session):
        """Parent previously failed can re-derive to running after child retry."""
        target_id = _make_target_id()
        parent = network_operations.start(
            db_session,
            NetworkOperationType.ont_provision,
            NetworkOperationTargetType.ont,
            target_id,
        )
        # Create two children, both fail
        child1 = network_operations.start(
            db_session,
            NetworkOperationType.ont_set_pppoe,
            NetworkOperationTargetType.ont,
            target_id,
            parent_id=str(parent.id),
        )
        child2 = network_operations.start(
            db_session,
            NetworkOperationType.ont_send_conn_request,
            NetworkOperationTargetType.ont,
            target_id,
            parent_id=str(parent.id),
        )
        network_operations.mark_running(db_session, str(child1.id))
        network_operations.mark_failed(db_session, str(child1.id), "fail1")
        network_operations.mark_running(db_session, str(child2.id))
        network_operations.mark_failed(db_session, str(child2.id), "fail2")
        db_session.flush()

        # Parent derives to failed
        updated = network_operations.update_parent_status(
            db_session, str(parent.id)
        )
        assert updated.status == NetworkOperationStatus.failed

        # Retry child2 — mark it running again (new child op in real code,
        # but simulating by directly updating for test purposes)
        child2.status = NetworkOperationStatus.running
        child2.error = None
        db_session.flush()

        # Parent should re-derive to running (bypassing terminal check)
        updated = network_operations.update_parent_status(
            db_session, str(parent.id)
        )
        assert updated.status == NetworkOperationStatus.running
        assert updated.completed_at is None
