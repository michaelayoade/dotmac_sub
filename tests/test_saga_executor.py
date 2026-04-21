"""Unit tests for the saga executor.

Tests the saga pattern implementation including:
- Step execution
- Compensation on failure
- Result building
- Context handling
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.models.compensation_failure import CompensationFailure
from app.models.saga_execution import ProvisioningStepExecutionStatus
from app.services.network.ont_provisioning.result import StepResult
from app.services.network.ont_provisioning.saga.executor import (
    SagaExecutor,
    execute_saga,
)
from app.services.network.ont_provisioning.saga.types import (
    SagaContext,
    SagaDefinition,
    SagaExecutionStatus,
    SagaStep,
)


@pytest.fixture
def mock_db():
    """Create a mock database session."""
    db = MagicMock()
    db.get = MagicMock(return_value=None)
    return db


@pytest.fixture
def sample_ont_id():
    """Generate a sample ONT ID."""
    return str(uuid.uuid4())


@pytest.fixture
def sample_execution_id():
    """Generate a sample execution ID."""
    return str(uuid.uuid4())


def make_success_step(name: str) -> SagaStep:
    """Create a step that always succeeds."""
    return SagaStep(
        name=name,
        action=lambda ctx: StepResult(
            step_name=name,
            success=True,
            message=f"{name} completed",
        ),
        critical=True,
    )


def make_failure_step(name: str, critical: bool = True) -> SagaStep:
    """Create a step that always fails."""
    return SagaStep(
        name=name,
        action=lambda ctx: StepResult(
            step_name=name,
            success=False,
            message=f"{name} failed",
            critical=critical,
        ),
        critical=critical,
    )


def make_compensatable_step(
    name: str,
    compensation_succeeds: bool = True,
) -> SagaStep:
    """Create a step with compensation that tracks calls."""

    def compensate(ctx, original):
        ctx.step_data[f"{name}_compensated"] = True
        return StepResult(
            step_name=f"{name}_compensation",
            success=compensation_succeeds,
            message=f"Compensated {name}" if compensation_succeeds else "Compensation failed",
        )

    return SagaStep(
        name=name,
        action=lambda ctx: StepResult(
            step_name=name,
            success=True,
            message=f"{name} completed",
        ),
        compensate=compensate,
        critical=True,
    )


class TestSagaExecutorBasic:
    """Test basic saga execution."""

    def test_empty_saga_succeeds(self, mock_db, sample_ont_id, sample_execution_id):
        """An empty saga should succeed immediately."""
        saga = SagaDefinition(
            name="empty_saga",
            description="Test empty saga",
            steps=[],
        )

        context = SagaContext(
            db=mock_db,
            ont_id=sample_ont_id,
            saga_execution_id=sample_execution_id,
        )

        # Mock the model loading
        with patch.object(SagaExecutor, "_load_context_models", return_value=True):
            executor = SagaExecutor(saga, context)
            result = executor.execute()

        assert result.success is True
        assert result.status == SagaExecutionStatus.succeeded
        assert len(result.steps_executed) == 0

    def test_single_success_step(self, mock_db, sample_ont_id, sample_execution_id):
        """A single successful step should complete the saga."""
        saga = SagaDefinition(
            name="single_step",
            description="Test single step",
            steps=[make_success_step("step1")],
        )

        context = SagaContext(
            db=mock_db,
            ont_id=sample_ont_id,
            saga_execution_id=sample_execution_id,
        )

        with patch.object(SagaExecutor, "_load_context_models", return_value=True):
            executor = SagaExecutor(saga, context)
            result = executor.execute()

        assert result.success is True
        assert len(result.steps_executed) == 1
        assert result.steps_executed[0].step_name == "step1"
        assert result.steps_executed[0].success is True

    def test_multiple_success_steps(self, mock_db, sample_ont_id, sample_execution_id):
        """Multiple successful steps should all execute."""
        saga = SagaDefinition(
            name="multi_step",
            description="Test multiple steps",
            steps=[
                make_success_step("step1"),
                make_success_step("step2"),
                make_success_step("step3"),
            ],
        )

        context = SagaContext(
            db=mock_db,
            ont_id=sample_ont_id,
            saga_execution_id=sample_execution_id,
        )

        with patch.object(SagaExecutor, "_load_context_models", return_value=True):
            executor = SagaExecutor(saga, context)
            result = executor.execute()

        assert result.success is True
        assert len(result.steps_executed) == 3
        assert all(s.success for s in result.steps_executed)


class TestSagaExecutorFailure:
    """Test saga failure handling."""

    def test_critical_failure_stops_saga(
        self, mock_db, sample_ont_id, sample_execution_id
    ):
        """A critical failure should stop the saga."""
        saga = SagaDefinition(
            name="failure_test",
            description="Test failure handling",
            steps=[
                make_success_step("step1"),
                make_failure_step("step2", critical=True),
                make_success_step("step3"),  # Should not execute
            ],
        )

        context = SagaContext(
            db=mock_db,
            ont_id=sample_ont_id,
            saga_execution_id=sample_execution_id,
        )

        with patch.object(SagaExecutor, "_load_context_models", return_value=True):
            executor = SagaExecutor(saga, context)
            result = executor.execute()

        assert result.success is False
        assert result.failed_step == "step2"
        assert len(result.steps_executed) == 2  # step1 and step2
        # step3 should not have executed
        assert "step3" not in [s.step_name for s in result.steps_executed]

    def test_saga_timeout_fails_stuck_step(
        self, mock_db, sample_ont_id, sample_execution_id
    ):
        """A saga deadline should fail a step that blocks past the limit."""

        def stuck_step(ctx):
            time.sleep(1)
            return StepResult(
                step_name="stuck",
                success=True,
                message="should not complete",
            )

        saga = SagaDefinition(
            name="timeout_test",
            description="Test saga timeout",
            steps=[SagaStep(name="stuck", action=stuck_step, critical=True)],
            timeout_seconds=0.01,
        )

        context = SagaContext(
            db=mock_db,
            ont_id=sample_ont_id,
            saga_execution_id=sample_execution_id,
        )

        with patch.object(SagaExecutor, "_load_context_models", return_value=True):
            result = SagaExecutor(saga, context).execute()

        assert result.success is False
        assert result.failed_step == "stuck"
        assert "timed out" in result.message

    def test_noncritical_failure_continues(
        self, mock_db, sample_ont_id, sample_execution_id
    ):
        """A non-critical failure should not stop the saga."""
        saga = SagaDefinition(
            name="noncritical_test",
            description="Test non-critical failure",
            steps=[
                make_success_step("step1"),
                make_failure_step("step2", critical=False),
                make_success_step("step3"),
            ],
        )

        context = SagaContext(
            db=mock_db,
            ont_id=sample_ont_id,
            saga_execution_id=sample_execution_id,
        )

        with patch.object(SagaExecutor, "_load_context_models", return_value=True):
            executor = SagaExecutor(saga, context)
            result = executor.execute()

        assert result.success is True  # Overall success despite step2 failure
        assert len(result.steps_executed) == 3
        assert result.steps_executed[1].success is False  # step2 failed


class TestSagaExecutorCompensation:
    """Test compensation (rollback) behavior."""

    def test_compensation_on_critical_failure(
        self, mock_db, sample_ont_id, sample_execution_id
    ):
        """Compensation should run for completed steps on critical failure."""
        saga = SagaDefinition(
            name="compensation_test",
            description="Test compensation",
            steps=[
                make_compensatable_step("step1"),
                make_compensatable_step("step2"),
                make_failure_step("step3", critical=True),
            ],
        )

        context = SagaContext(
            db=mock_db,
            ont_id=sample_ont_id,
            saga_execution_id=sample_execution_id,
        )

        with patch.object(SagaExecutor, "_load_context_models", return_value=True):
            executor = SagaExecutor(saga, context)
            result = executor.execute()

        assert result.success is False
        assert result.failed_step == "step3"

        # Check compensations ran (reverse order)
        assert len(result.steps_compensated) == 2
        assert result.steps_compensated[0].step_name == "step2"
        assert result.steps_compensated[1].step_name == "step1"

        # Check context was updated
        assert context.step_data.get("step1_compensated") is True
        assert context.step_data.get("step2_compensated") is True

    def test_compensation_failure_tracked(
        self, mock_db, sample_ont_id, sample_execution_id
    ):
        """Failed compensations should be tracked for manual cleanup."""

        def failing_compensate(ctx, original):
            return StepResult(
                step_name="step1_compensation",
                success=False,
                message="Compensation failed",
            )

        failing_comp_step = SagaStep(
            name="step1",
            action=lambda ctx: StepResult(
                step_name="step1",
                success=True,
                message="step1 completed",
            ),
            compensate=failing_compensate,
            critical=True,
        )

        saga = SagaDefinition(
            name="compensation_failure_test",
            description="Test compensation failure tracking",
            steps=[
                failing_comp_step,
                make_failure_step("step2", critical=True),
            ],
        )

        context = SagaContext(
            db=mock_db,
            ont_id=sample_ont_id,
            saga_execution_id=sample_execution_id,
        )

        with patch.object(SagaExecutor, "_load_context_models", return_value=True):
            with patch(
                "app.services.notification_adapter.notify"
            ) as mock_notify:
                executor = SagaExecutor(saga, context)
                result = executor.execute()

        assert result.success is False
        assert result.status == SagaExecutionStatus.compensation_failed
        assert len(result.compensation_failures) == 1
        assert result.compensation_failures[0][0] == "step1"
        assert "step1" in result.steps_needing_manual_cleanup

    def test_retryable_saga_compensation_failure_is_persisted(
        self, mock_db, sample_ont_id, sample_execution_id
    ):
        """Mapped saga compensation failures should create CompensationFailure rows."""

        def failing_compensate(ctx, original):
            return StepResult(
                step_name="rollback_service_ports",
                success=False,
                message="service-port rollback failed",
            )

        saga = SagaDefinition(
            name="full_provisioning",
            description="Test persistence of retryable compensation failures",
            steps=[
                SagaStep(
                    name="create_service_ports",
                    action=lambda ctx: StepResult(
                        step_name="create_service_ports",
                        success=True,
                        message="created",
                    ),
                    compensate=failing_compensate,
                    critical=True,
                ),
                make_failure_step("step2", critical=True),
            ],
        )

        context = SagaContext(
            db=mock_db,
            ont_id=sample_ont_id,
            saga_execution_id=sample_execution_id,
        )
        context.ont = MagicMock(id=uuid.uuid4())
        context.olt = MagicMock(id=uuid.uuid4())

        with patch.object(SagaExecutor, "_load_context_models", return_value=True):
            with patch("app.services.notification_adapter.notify"):
                executor = SagaExecutor(saga, context)
                result = executor.execute()

        persisted_rows = [
            call.args[0]
            for call in mock_db.add.call_args_list
            if isinstance(call.args[0], CompensationFailure)
        ]
        assert result.status == SagaExecutionStatus.compensation_failed
        assert len(persisted_rows) == 1
        assert persisted_rows[0].step_name == "rollback_service_ports"
        assert persisted_rows[0].operation_type == "saga:full_provisioning"
        assert persisted_rows[0].resource_id == "create_service_ports"


class TestSagaExecutorContext:
    """Test context handling."""

    def test_step_data_shared_between_steps(
        self, mock_db, sample_ont_id, sample_execution_id
    ):
        """Steps should be able to share data via context."""

        def step1_action(ctx):
            ctx.step_data["value"] = 42
            return StepResult(step_name="step1", success=True, message="Set value")

        def step2_action(ctx):
            value = ctx.step_data.get("value")
            return StepResult(
                step_name="step2",
                success=value == 42,
                message=f"Got value: {value}",
            )

        saga = SagaDefinition(
            name="context_test",
            description="Test context sharing",
            steps=[
                SagaStep(name="step1", action=step1_action, critical=True),
                SagaStep(name="step2", action=step2_action, critical=True),
            ],
        )

        context = SagaContext(
            db=mock_db,
            ont_id=sample_ont_id,
            saga_execution_id=sample_execution_id,
        )

        with patch.object(SagaExecutor, "_load_context_models", return_value=True):
            executor = SagaExecutor(saga, context)
            result = executor.execute()

        assert result.success is True
        assert context.step_data["value"] == 42

    def test_initial_step_data_available(
        self, mock_db, sample_ont_id, sample_execution_id
    ):
        """Initial step_data should be available to all steps."""

        def check_step_data(ctx):
            return StepResult(
                step_name="check",
                success=ctx.step_data.get("initial_key") == "initial_value",
                message="Checked initial data",
            )

        saga = SagaDefinition(
            name="initial_data_test",
            description="Test initial data",
            steps=[SagaStep(name="check", action=check_step_data, critical=True)],
        )

        context = SagaContext(
            db=mock_db,
            ont_id=sample_ont_id,
            saga_execution_id=sample_execution_id,
            step_data={"initial_key": "initial_value"},
        )

        with patch.object(SagaExecutor, "_load_context_models", return_value=True):
            executor = SagaExecutor(saga, context)
            result = executor.execute()

        assert result.success is True


class TestSagaExecutorCallbacks:
    """Test success/failure callbacks."""

    def test_success_callback_called(
        self, mock_db, sample_ont_id, sample_execution_id
    ):
        """Success callback should be called on successful completion."""
        callback_called = {"value": False}

        def on_success(ctx, result):
            callback_called["value"] = True

        saga = SagaDefinition(
            name="callback_test",
            description="Test success callback",
            steps=[make_success_step("step1")],
            on_success=on_success,
        )

        context = SagaContext(
            db=mock_db,
            ont_id=sample_ont_id,
            saga_execution_id=sample_execution_id,
        )

        with patch.object(SagaExecutor, "_load_context_models", return_value=True):
            executor = SagaExecutor(saga, context)
            result = executor.execute()

        assert result.success is True
        assert callback_called["value"] is True

    def test_failure_callback_called(
        self, mock_db, sample_ont_id, sample_execution_id
    ):
        """Failure callback should be called on failure."""
        callback_called = {"value": False}

        def on_failure(ctx, result):
            callback_called["value"] = True

        saga = SagaDefinition(
            name="callback_test",
            description="Test failure callback",
            steps=[make_failure_step("step1", critical=True)],
            on_failure=on_failure,
        )

        context = SagaContext(
            db=mock_db,
            ont_id=sample_ont_id,
            saga_execution_id=sample_execution_id,
        )

        with patch.object(SagaExecutor, "_load_context_models", return_value=True):
            executor = SagaExecutor(saga, context)
            result = executor.execute()

        assert result.success is False
        assert callback_called["value"] is True


class TestExecuteSagaHelper:
    """Test the execute_saga helper function."""

    def test_execute_saga_creates_context(self, mock_db, sample_ont_id):
        """execute_saga should create a proper context."""
        saga = SagaDefinition(
            name="helper_test",
            description="Test helper",
            steps=[make_success_step("step1")],
        )

        with patch.object(SagaExecutor, "_load_context_models", return_value=True):
            result = execute_saga(
                mock_db,
                saga,
                sample_ont_id,
                step_data={"key": "value"},
                initiated_by="test_user",
            )

        assert result.success is True
        assert result.saga_name == "helper_test"

    def test_execute_saga_with_dry_run(self, mock_db, sample_ont_id):
        """execute_saga should pass dry_run to context."""
        dry_run_seen = {"value": False}

        def check_dry_run(ctx):
            dry_run_seen["value"] = ctx.dry_run
            return StepResult(step_name="check", success=True, message="Checked")

        saga = SagaDefinition(
            name="dry_run_test",
            description="Test dry run",
            steps=[SagaStep(name="check", action=check_dry_run, critical=True)],
        )

        with patch.object(SagaExecutor, "_load_context_models", return_value=True):
            result = execute_saga(mock_db, saga, sample_ont_id, dry_run=True)

        assert result.success is True
        assert dry_run_seen["value"] is True


class TestSagaResult:
    """Test SagaResult properties and serialization."""

    def test_result_to_dict(self, mock_db, sample_ont_id, sample_execution_id):
        """SagaResult.to_dict should produce valid JSON-serializable output."""
        saga = SagaDefinition(
            name="serialization_test",
            description="Test serialization",
            steps=[
                make_success_step("step1"),
                make_failure_step("step2", critical=False),
            ],
        )

        context = SagaContext(
            db=mock_db,
            ont_id=sample_ont_id,
            saga_execution_id=sample_execution_id,
        )

        with patch.object(SagaExecutor, "_load_context_models", return_value=True):
            executor = SagaExecutor(saga, context)
            result = executor.execute()

        result_dict = result.to_dict()

        assert isinstance(result_dict, dict)
        assert result_dict["saga_name"] == "serialization_test"
        assert result_dict["success"] is True
        assert isinstance(result_dict["steps_executed"], list)
        assert len(result_dict["steps_executed"]) == 2
        assert isinstance(result_dict["duration_ms"], int)


class TestSagaExecutorResume:
    """Test durable saga step resume behavior."""

    def test_resumable_step_skips_reexecution_on_retry(
        self, mock_db, sample_ont_id, sample_execution_id, monkeypatch
    ):
        """A resumable step with a durable success checkpoint should be skipped."""

        step1_calls: list[str] = []
        step2_calls: list[str] = []
        persisted_statuses: list[tuple[str, bool]] = []

        saga = SagaDefinition(
            name="resume_test",
            description="Resume from step checkpoint",
            steps=[
                SagaStep(
                    name="step1",
                    action=lambda ctx: step1_calls.append("step1")
                    or StepResult(
                        step_name="step1",
                        success=True,
                        message="step1 completed",
                    ),
                    critical=True,
                    resumable=True,
                ),
                SagaStep(
                    name="step2",
                    action=lambda ctx: step2_calls.append("step2")
                    or StepResult(
                        step_name="step2",
                        success=True,
                        message="step2 completed",
                    ),
                    critical=True,
                    resumable=True,
                ),
            ],
        )

        context = SagaContext(
            db=mock_db,
            ont_id=sample_ont_id,
            saga_execution_id=sample_execution_id,
            correlation_key="saga:resume_test:ont-1",
        )

        checkpoint = MagicMock()
        checkpoint.saga_execution_id = uuid.uuid4()
        checkpoint.status = ProvisioningStepExecutionStatus.succeeded
        checkpoint.result_data = {
            "step_name": "step1",
            "success": True,
            "message": "step1 completed",
            "data": {"service_port_id": "sp-1"},
        }

        from app.services.network.ont_provisioning.saga import persistence

        monkeypatch.setattr(
            persistence.saga_step_executions,
            "get_resumable_steps",
            lambda *args, **kwargs: {"step1": checkpoint},
        )
        monkeypatch.setattr(
            persistence.saga_step_executions,
            "mark_running",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            persistence.saga_step_executions,
            "mark_completed",
            lambda *args, **kwargs: persisted_statuses.append(
                (kwargs["step_name"], kwargs["record"].skipped)
            ),
        )
        monkeypatch.setattr(
            persistence.saga_step_executions,
            "mark_compensated",
            lambda *args, **kwargs: None,
        )

        with patch.object(SagaExecutor, "_load_context_models", return_value=True):
            result = SagaExecutor(saga, context).execute()

        assert result.success is True
        assert step1_calls == []
        assert step2_calls == ["step2"]
        assert result.steps_executed[0].step_name == "step1"
        assert result.steps_executed[0].skipped is True
        assert context.step_data["resumed_steps"]["step1"]["service_port_id"] == "sp-1"
        assert persisted_statuses == [("step1", True), ("step2", False)]

    def test_non_resumable_step_still_executes(
        self, mock_db, sample_ont_id, sample_execution_id, monkeypatch
    ):
        """A checkpoint must not skip a step that did not opt into resume."""

        step_calls: list[str] = []

        saga = SagaDefinition(
            name="resume_test",
            description="Resume only when enabled",
            steps=[
                SagaStep(
                    name="step1",
                    action=lambda ctx: step_calls.append("step1")
                    or StepResult(
                        step_name="step1",
                        success=True,
                        message="step1 completed",
                    ),
                    critical=True,
                    resumable=False,
                ),
            ],
        )

        context = SagaContext(
            db=mock_db,
            ont_id=sample_ont_id,
            saga_execution_id=sample_execution_id,
            correlation_key="saga:resume_test:ont-1",
        )

        checkpoint = MagicMock()
        checkpoint.saga_execution_id = uuid.uuid4()
        checkpoint.status = ProvisioningStepExecutionStatus.succeeded
        checkpoint.result_data = {}

        from app.services.network.ont_provisioning.saga import persistence

        monkeypatch.setattr(
            persistence.saga_step_executions,
            "get_resumable_steps",
            lambda *args, **kwargs: {"step1": checkpoint},
        )
        monkeypatch.setattr(
            persistence.saga_step_executions,
            "mark_running",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            persistence.saga_step_executions,
            "mark_completed",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            persistence.saga_step_executions,
            "mark_compensated",
            lambda *args, **kwargs: None,
        )

        with patch.object(SagaExecutor, "_load_context_models", return_value=True):
            result = SagaExecutor(saga, context).execute()

        assert result.success is True
        assert step_calls == ["step1"]
