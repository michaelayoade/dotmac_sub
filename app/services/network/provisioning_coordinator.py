"""Provisioning Coordinator - Orchestrates OLT and ACS provisioning steps.

Coordinates the multi-step ONT provisioning sequence:
1. OLT Registration (ont add, service-ports)
2. Management IP configuration
3. TR-069 profile binding on OLT
4. ACS device binding/discovery
5. Config push via ACS (WiFi, LAN, WAN)

Usage:
    coordinator = ProvisioningCoordinator(db)
    result = coordinator.provision_ont(
        olt_id=olt_id,
        fsp=fsp,
        serial_number=serial_number,
        profile_id=profile_id,
    )

    if result.success:
        print(f"Provisioned in {result.duration_ms}ms")
    for step in result.steps:
        print(f"  {step.phase}: {step.status} - {step.message}")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from starlette.requests import Request

    from app.models.network import OLTDevice, OntUnit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums and Data Classes
# ---------------------------------------------------------------------------


class ProvisioningPhase(str, Enum):
    """Phases of the provisioning process."""

    # OLT-side phases
    olt_registration = "olt_registration"
    service_port_creation = "service_port_creation"
    management_ip = "management_ip"
    tr069_profile_bind = "tr069_profile_bind"

    # ACS-side phases
    acs_discovery = "acs_discovery"
    acs_config_push = "acs_config_push"

    # Verification
    verification = "verification"

    # Rollback
    rollback = "rollback"


class StepStatus(str, Enum):
    """Status of a provisioning step."""

    pending = "pending"
    running = "running"
    success = "success"
    warning = "warning"
    failed = "failed"
    skipped = "skipped"


@dataclass
class ProvisioningStep:
    """A single step in the provisioning process."""

    phase: ProvisioningPhase
    name: str
    status: StepStatus = StepStatus.pending
    message: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None
    data: dict = field(default_factory=dict)

    @property
    def duration_ms(self) -> int | None:
        if self.started_at and self.completed_at:
            return int((self.completed_at - self.started_at).total_seconds() * 1000)
        return None

    def start(self) -> None:
        self.status = StepStatus.running
        self.started_at = datetime.now(UTC)

    def complete(self, success: bool, message: str, data: dict | None = None) -> None:
        self.status = StepStatus.success if success else StepStatus.failed
        self.message = message
        self.completed_at = datetime.now(UTC)
        if data:
            self.data.update(data)

    def skip(self, reason: str) -> None:
        self.status = StepStatus.skipped
        self.message = reason
        self.completed_at = datetime.now(UTC)

    def warn(self, message: str) -> None:
        self.status = StepStatus.warning
        self.message = message
        self.completed_at = datetime.now(UTC)


@dataclass
class ProvisioningResult:
    """Result of the complete provisioning process."""

    success: bool
    message: str
    steps: list[ProvisioningStep] = field(default_factory=list)
    ont_id: str | None = None
    ont_id_on_olt: int | None = None
    serial_number: str | None = None
    fsp: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    @property
    def duration_ms(self) -> int:
        if self.completed_at:
            return int((self.completed_at - self.started_at).total_seconds() * 1000)
        return int((datetime.now(UTC) - self.started_at).total_seconds() * 1000)

    @property
    def failed_step(self) -> ProvisioningStep | None:
        for step in self.steps:
            if step.status == StepStatus.failed:
                return step
        return None

    @property
    def phase_summary(self) -> dict[str, str]:
        """Get a summary of each phase's status."""
        return {step.phase.value: step.status.value for step in self.steps}

    def add_step(
        self,
        phase: ProvisioningPhase,
        name: str,
        success: bool,
        message: str,
        data: dict | None = None,
    ) -> ProvisioningStep:
        """Add a completed step."""
        step = ProvisioningStep(
            phase=phase,
            name=name,
            status=StepStatus.success if success else StepStatus.failed,
            message=message,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            data=data or {},
        )
        self.steps.append(step)
        return step

    def to_operation_result(self):
        """Convert to OperationResult for response rendering."""
        from app.services.network.result_adapter import OperationResult, ResultStatus

        data = {
            "ont_id": self.ont_id,
            "ont_id_on_olt": self.ont_id_on_olt,
            "serial_number": self.serial_number,
            "fsp": self.fsp,
            "duration_ms": self.duration_ms,
            "steps": [
                {
                    "phase": s.phase.value,
                    "name": s.name,
                    "status": s.status.value,
                    "message": s.message,
                }
                for s in self.steps
            ],
        }

        if self.success:
            return OperationResult(
                status=ResultStatus.success,
                message=self.message,
                title="Provisioning Complete",
                data={k: v for k, v in data.items() if v is not None},
            )

        return OperationResult(
            status=ResultStatus.error,
            message=self.message,
            title="Provisioning Failed",
            data={k: v for k, v in data.items() if v is not None},
        )


# ---------------------------------------------------------------------------
# Provisioning Coordinator
# ---------------------------------------------------------------------------


class ProvisioningCoordinator:
    """Orchestrates multi-step ONT provisioning across OLT and ACS.

    This coordinator handles the complete provisioning sequence:
    1. OLT Registration - Add ONT to OLT with line/service profiles
    2. Service Ports - Create service-port mappings for VLANs
    3. Management IP - Configure IP for ONT management access
    4. TR-069 Bind - Bind ACS profile so ONT reports to GenieACS
    5. ACS Discovery - Wait for ONT to appear in ACS
    6. Config Push - Push WiFi/LAN/WAN config via TR-069

    The coordinator tracks each step and supports:
    - Step-by-step progress tracking
    - Partial failure handling
    - Rollback on critical failures
    - Async execution via Celery
    """

    def __init__(
        self,
        db: Session,
        *,
        request: Request | None = None,
        initiated_by: str | None = None,
    ):
        self.db = db
        self.request = request
        self.initiated_by = initiated_by or self._resolve_actor()
        self._result: ProvisioningResult | None = None

    def _resolve_actor(self) -> str | None:
        if self.request:
            from app.services.network.action_logging import actor_label

            return actor_label(self.request)
        return None

    def provision_ont(
        self,
        olt_id: str,
        fsp: str,
        serial_number: str,
        *,
        profile_id: str | None = None,
        force_reauthorize: bool = False,
        skip_acs_config: bool = False,
        acs_config_timeout_seconds: int = 120,
    ) -> ProvisioningResult:
        """Execute the complete provisioning sequence.

        Args:
            olt_id: OLT device ID
            fsp: Frame/Slot/Port location (e.g., "0/1/0")
            serial_number: ONT serial number
            profile_id: Optional provisioning profile ID
            force_reauthorize: Delete existing registration first
            skip_acs_config: Skip ACS config push (just OLT registration)
            acs_config_timeout_seconds: Timeout for ACS operations

        Returns:
            ProvisioningResult with all steps and final status
        """
        self._result = ProvisioningResult(
            success=False,
            message="Provisioning started",
            serial_number=serial_number,
            fsp=fsp,
        )

        try:
            # Phase 1: OLT Registration
            if not self._execute_olt_registration(
                olt_id, fsp, serial_number, force_reauthorize
            ):
                return self._finalize("OLT registration failed")

            if not self._execute_post_registration_saga(
                olt_id,
                skip_acs_config=skip_acs_config,
                acs_config_timeout_seconds=acs_config_timeout_seconds,
            ):
                return self._finalize("Post-registration provisioning saga failed")

            return self._finalize("Provisioning completed successfully", success=True)

        except Exception as exc:
            logger.error(
                "Provisioning failed for %s on %s: %s",
                serial_number,
                olt_id,
                exc,
                exc_info=True,
            )
            self._result.add_step(
                ProvisioningPhase.rollback,
                "Unexpected error",
                False,
                str(exc),
            )
            return self._finalize(f"Provisioning failed: {exc}")

    def _finalize(self, message: str, success: bool = False) -> ProvisioningResult:
        """Finalize the provisioning result."""
        assert self._result is not None
        if not success:
            self._compensate_after_failure()
        self._result.success = success
        self._result.message = message
        self._result.completed_at = datetime.now(UTC)
        return self._result

    def _compensate_after_failure(self) -> None:
        """Run known compensating actions for coordinator-managed side effects."""
        assert self._result is not None
        if not self._result.ont_id:
            return
        if any(step.phase == ProvisioningPhase.rollback for step in self._result.steps):
            return
        step = ProvisioningStep(
            phase=ProvisioningPhase.rollback,
            name="Compensate provisioning side effects",
        )
        step.start()
        self._result.steps.append(step)
        try:
            from app.services.network.ont_provision_steps import rollback_service_ports

            rollback = rollback_service_ports(self.db, self._result.ont_id)
            step.complete(
                rollback.success,
                rollback.message,
                {"compensated_step": rollback.step_name},
            )
        except Exception as exc:
            logger.error(
                "Provisioning compensation failed for ONT %s: %s",
                self._result.ont_id,
                exc,
                exc_info=True,
                extra={
                    "event": "provisioning_compensation_failed",
                    "ont_id": self._result.ont_id,
                },
            )
            step.complete(False, f"Compensation failed: {exc}")

    # -----------------------------------------------------------------------
    # Phase Executors
    # -----------------------------------------------------------------------

    def _execute_post_registration_saga(
        self,
        olt_id: str,
        *,
        skip_acs_config: bool,
        acs_config_timeout_seconds: int,
    ) -> bool:
        """Run coordinator post-registration phases through SagaExecutor."""
        assert self._result is not None
        if not self._result.ont_id:
            return False

        from app.services.network.ont_provisioning.result import StepResult
        from app.services.network.ont_provisioning.saga import (
            SagaContext,
            SagaDefinition,
            SagaExecutor,
            SagaStep,
            generate_saga_execution_id,
            saga_executions,
        )

        def _step_result(name: str, ok: bool, *, critical: bool = False) -> StepResult:
            return StepResult(
                step_name=name,
                success=ok,
                message=f"{name.replace('_', ' ').title()} {'completed' if ok else 'failed'}",
                critical=critical,
            )

        def _verify_service_ports(_ctx: SagaContext) -> StepResult:
            ok = self._execute_service_port_creation(olt_id)
            return _step_result("verify_service_ports", ok)

        def _compensate_service_ports(
            ctx: SagaContext, _original: StepResult
        ) -> StepResult:
            from app.services.network.ont_provision_steps import rollback_service_ports

            return rollback_service_ports(ctx.db, ctx.ont_id)

        def _management_ip(_ctx: SagaContext) -> StepResult:
            ok = self._execute_management_ip_config(olt_id)
            return _step_result("management_ip", ok)

        def _tr069_binding(_ctx: SagaContext) -> StepResult:
            ok = self._execute_tr069_binding(olt_id)
            return _step_result("tr069_binding", ok)

        def _acs_discovery(_ctx: SagaContext) -> StepResult:
            ok = self._execute_acs_discovery(acs_config_timeout_seconds)
            return _step_result("acs_discovery", ok)

        def _acs_config_push(_ctx: SagaContext) -> StepResult:
            ok = self._execute_acs_config_push()
            return _step_result("acs_config_push", ok)

        steps = [
            SagaStep(
                name="verify_service_ports",
                action=_verify_service_ports,
                compensate=_compensate_service_ports,
                critical=False,
                resumable=True,
                description="Verify service ports after OLT registration",
            ),
            SagaStep(
                name="management_ip",
                action=_management_ip,
                critical=False,
                resumable=True,
                description="Verify or configure management IP",
            ),
            SagaStep(
                name="tr069_binding",
                action=_tr069_binding,
                critical=False,
                resumable=True,
                description="Bind TR-069 server profile",
            ),
        ]
        if not skip_acs_config:
            steps.extend(
                [
                    SagaStep(
                        name="acs_discovery",
                        action=_acs_discovery,
                        critical=False,
                        resumable=True,
                        description="Queue ACS discovery wait",
                    ),
                    SagaStep(
                        name="acs_config_push",
                        action=_acs_config_push,
                        critical=False,
                        resumable=True,
                        description="Queue ACS config push",
                    ),
                ]
            )

        saga = SagaDefinition(
            name="coordinated_post_registration",
            description="Coordinator post-registration OLT/ACS provisioning phases",
            steps=steps,
            version="1.0",
        )
        context = SagaContext(
            db=self.db,
            ont_id=self._result.ont_id,
            saga_execution_id=generate_saga_execution_id(),
            ont=self._get_ont(self._result.ont_id),
            olt=self._get_olt(olt_id),
            initiated_by=self.initiated_by,
            correlation_key=(
                f"saga:coordinated_post_registration:{self._result.ont_id}"
            ),
        )
        saga_executions.create(self.db, saga, context)
        saga_executions.mark_running(self.db, context.saga_execution_id)
        saga_result = SagaExecutor(saga, context).execute()
        if hasattr(saga_result, "status"):
            saga_executions.mark_completed(
                self.db,
                context.saga_execution_id,
                saga_result,
            )
        if saga_result.compensation_failures:
            self._result.add_step(
                ProvisioningPhase.rollback,
                "Saga compensation failures",
                False,
                "; ".join(
                    f"{step}: {error}"
                    for step, error in saga_result.compensation_failures
                ),
            )
        return saga_result.success

    def _execute_olt_registration(
        self,
        olt_id: str,
        fsp: str,
        serial_number: str,
        force_reauthorize: bool,
    ) -> bool:
        """Phase 1: Register ONT on OLT."""
        from app.services.network.olt_authorization_workflow import (
            authorize_autofind_ont_and_provision_network_audited,
        )

        assert self._result is not None

        step = ProvisioningStep(
            phase=ProvisioningPhase.olt_registration,
            name="Register ONT on OLT",
        )
        step.start()
        self._result.steps.append(step)

        try:
            result = authorize_autofind_ont_and_provision_network_audited(
                self.db,
                olt_id,
                fsp,
                serial_number,
                force_reauthorize=force_reauthorize,
                request=self.request,
            )

            step.complete(
                result.success,
                result.message,
                {
                    "ont_id": result.ont_id,
                    "ont_id_on_olt": result.ont_id_on_olt,
                },
            )

            if result.success:
                self._result.ont_id = result.ont_id
                self._result.ont_id_on_olt = result.ont_id_on_olt

            return result.success

        except Exception as exc:
            step.complete(False, f"Registration failed: {exc}")
            return False

    def _execute_service_port_creation(self, olt_id: str) -> bool:
        """Phase 2: Create service ports (usually done in registration)."""
        assert self._result is not None

        step = ProvisioningStep(
            phase=ProvisioningPhase.service_port_creation,
            name="Verify service ports",
        )
        step.start()
        self._result.steps.append(step)

        # Service ports are typically created during authorization
        # This step verifies they exist
        if not self._result.ont_id:
            step.skip("No ONT ID - skipping service port verification")
            return True

        try:
            from app.services.network.olt_write_reconciliation import (
                verify_ont_service_ports,
            )

            olt = self._get_olt(olt_id)
            if not olt:
                step.warn("OLT not found for verification")
                return True

            ont = self._get_ont(self._result.ont_id)
            if not ont:
                step.warn("ONT not found for verification")
                return True

            verification = verify_ont_service_ports(
                olt,
                fsp=self._result.fsp or "",
                ont_id_on_olt=self._result.ont_id_on_olt or 0,
            )

            if verification.success:
                step.complete(True, verification.message, verification.details)
            else:
                step.warn(verification.message)

            return True  # Non-fatal

        except Exception as exc:
            step.warn(f"Service port verification failed: {exc}")
            return True  # Non-fatal

    def _execute_management_ip_config(self, olt_id: str) -> bool:
        """Phase 3: Configure management IP."""
        assert self._result is not None

        step = ProvisioningStep(
            phase=ProvisioningPhase.management_ip,
            name="Configure management IP",
        )
        step.start()
        self._result.steps.append(step)

        if not self._result.ont_id:
            step.skip("No ONT ID - skipping management IP")
            return True

        try:
            ont = self._get_ont(self._result.ont_id)
            if not ont:
                step.skip("ONT not found")
                return True

            # Check if management IP is already configured
            mgmt_ip = getattr(ont, "management_ip", None)
            if mgmt_ip:
                step.complete(True, f"Management IP already configured: {mgmt_ip}")
                return True

            # Management IP configuration is typically done during authorization
            # via the provisioning profile. This step just verifies.
            step.complete(
                True,
                "Management IP will be configured by provisioning profile",
            )
            return True

        except Exception as exc:
            step.warn(f"Management IP check failed: {exc}")
            return True  # Non-fatal

    def _execute_tr069_binding(self, olt_id: str) -> bool:
        """Phase 4: Bind TR-069 server profile on OLT."""
        assert self._result is not None

        step = ProvisioningStep(
            phase=ProvisioningPhase.tr069_profile_bind,
            name="Bind TR-069 profile",
        )
        step.start()
        self._result.steps.append(step)

        if not self._result.ont_id or not self._result.ont_id_on_olt:
            step.skip("No ONT registration - skipping TR-069 bind")
            return True

        try:
            from app.services.network.olt_protocol_adapters import get_protocol_adapter
            from app.services.network.olt_tr069_admin import (
                ensure_tr069_profile_for_linked_acs,
            )

            olt = self._get_olt(olt_id)
            if not olt:
                step.warn("OLT not found")
                return True

            # Ensure TR-069 profile exists on OLT
            profile_ok, profile_msg, profile_id = ensure_tr069_profile_for_linked_acs(
                olt
            )
            if not profile_ok or profile_id is None:
                step.warn(f"TR-069 profile not available: {profile_msg}")
                return True

            # Bind profile to ONT
            bind_result = get_protocol_adapter(olt).bind_tr069_profile(
                self._result.fsp or "",
                self._result.ont_id_on_olt,
                profile_id=profile_id,
            )
            bind_ok = bind_result.success
            bind_msg = bind_result.message

            step.complete(bind_ok, bind_msg, {"profile_id": profile_id})
            return bind_ok

        except Exception as exc:
            step.warn(f"TR-069 binding failed: {exc}")
            return True  # Non-fatal

    def _execute_acs_discovery(self, timeout_seconds: int) -> bool:
        """Phase 5: Wait for ONT to appear in ACS."""
        assert self._result is not None

        step = ProvisioningStep(
            phase=ProvisioningPhase.acs_discovery,
            name="Wait for ACS discovery",
        )
        step.start()
        self._result.steps.append(step)

        if not self._result.ont_id:
            step.skip("No ONT ID - skipping ACS discovery")
            return True

        try:
            ont = self._get_ont(self._result.ont_id)
            if not ont:
                step.skip("ONT not found")
                return True

            # Check if already discovered
            acs_device_id = getattr(ont, "acs_device_id", None)
            acs_last_inform = getattr(ont, "acs_last_inform_at", None)

            if acs_device_id and acs_last_inform:
                step.complete(
                    True,
                    f"ONT already discovered in ACS: {acs_device_id}",
                    {"acs_device_id": acs_device_id},
                )
                return True

            # Queue wait task for async discovery
            from app.services.network.ont_provision_steps import (
                queue_wait_tr069_bootstrap,
            )

            wait_result = queue_wait_tr069_bootstrap(self.db, self._result.ont_id)
            step.complete(
                True,
                f"Queued ACS discovery wait: {wait_result.message}",
                {"operation_id": getattr(wait_result, "operation_id", None)},
            )
            return True

        except Exception as exc:
            step.warn(f"ACS discovery setup failed: {exc}")
            return True  # Non-fatal

    def _execute_acs_config_push(self) -> bool:
        """Phase 6: Push configuration via ACS/TR-069."""
        assert self._result is not None

        step = ProvisioningStep(
            phase=ProvisioningPhase.acs_config_push,
            name="Push ACS configuration",
        )
        step.start()
        self._result.steps.append(step)

        if not self._result.ont_id:
            step.skip("No ONT ID - skipping config push")
            return True

        try:
            ont = self._get_ont(self._result.ont_id)
            if not ont:
                step.skip("ONT not found")
                return True

            # Check if ONT is ready for config push
            acs_device_id = getattr(ont, "acs_device_id", None)
            if not acs_device_id:
                step.skip("ONT not yet discovered in ACS - config push deferred")
                return True

            # Queue config push via provisioning profile
            from app.services.network.ont_profile_push import queue_profile_push

            push_result = queue_profile_push(self.db, str(ont.id))

            if push_result.success:
                step.complete(
                    True,
                    push_result.message,
                    {"operation_id": getattr(push_result, "operation_id", None)},
                )
            else:
                step.warn(push_result.message)

            return push_result.success

        except Exception as exc:
            step.warn(f"Config push failed: {exc}")
            return True  # Non-fatal

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _get_olt(self, olt_id: str) -> OLTDevice | None:
        from app.services.network.olt_inventory import get_olt_or_none

        return get_olt_or_none(self.db, olt_id)

    def _get_ont(self, ont_id: str) -> OntUnit | None:
        from app.models.network import OntUnit

        return self.db.get(OntUnit, ont_id)


# ---------------------------------------------------------------------------
# Async Coordinator (Celery-backed)
# ---------------------------------------------------------------------------


@dataclass
class AsyncProvisioningResult:
    """Result from queuing async provisioning."""

    queued: bool
    message: str
    operation_id: str | None = None
    correlation_key: str | None = None


def queue_provisioning(
    db: Session,
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    profile_id: str | None = None,
    force_reauthorize: bool = False,
    initiated_by: str | None = None,
    request: Request | None = None,
) -> AsyncProvisioningResult:
    """Queue the complete provisioning workflow as a tracked operation.

    This queues a Celery task that uses ProvisioningCoordinator to execute
    all provisioning steps. Progress is tracked via NetworkOperation.

    Args:
        db: Database session
        olt_id: OLT device ID
        fsp: Frame/Slot/Port location
        serial_number: ONT serial number
        profile_id: Optional provisioning profile ID
        force_reauthorize: Delete existing registration first
        initiated_by: User/system that initiated
        request: HTTP request for audit

    Returns:
        AsyncProvisioningResult with operation tracking info
    """
    from app.models.network_operation import (
        NetworkOperation,
        NetworkOperationStatus,
        NetworkOperationTargetType,
        NetworkOperationType,
    )
    from app.services.network_operations import network_operations

    if initiated_by is None and request is not None:
        from app.services.network.action_logging import actor_label

        initiated_by = actor_label(request)

    normalized_serial = str(serial_number).replace("-", "").strip().upper()
    mode = "force" if force_reauthorize else "normal"
    correlation_key = f"provision:{mode}:{olt_id}:{fsp}:{normalized_serial}"

    try:
        op = network_operations.start(
            db,
            NetworkOperationType.ont_provision,
            NetworkOperationTargetType.olt,
            olt_id,
            correlation_key=correlation_key,
            initiated_by=initiated_by,
            input_payload={
                "phase": "provisioning",
                "title": "ONT Provisioning",
                "message": "Queued complete provisioning workflow.",
                "olt_id": olt_id,
                "fsp": fsp,
                "serial_number": serial_number,
                "profile_id": profile_id,
                "force_reauthorize": force_reauthorize,
            },
        )
    except Exception as exc:
        # Check if already in progress
        from fastapi import HTTPException

        if isinstance(exc, HTTPException) and exc.status_code == 409:
            from sqlalchemy import select

            existing = db.scalars(
                select(NetworkOperation.id).where(
                    NetworkOperation.correlation_key == correlation_key,
                    NetworkOperation.status.in_(
                        (
                            NetworkOperationStatus.pending,
                            NetworkOperationStatus.running,
                            NetworkOperationStatus.waiting,
                        )
                    ),
                )
            ).first()
            return AsyncProvisioningResult(
                queued=True,
                message="Provisioning is already in progress.",
                operation_id=str(existing) if existing else None,
                correlation_key=correlation_key,
            )
        raise

    network_operations.mark_waiting(db, str(op.id), "Queued provisioning workflow.")
    db.commit()

    try:
        from app.services.queue_adapter import enqueue_task

        dispatch = enqueue_task(
            "app.tasks.provisioning.run_coordinated_provisioning_task",
            args=[str(op.id), olt_id, fsp, serial_number],
            kwargs={
                "profile_id": profile_id,
                "force_reauthorize": force_reauthorize,
            },
            correlation_id=correlation_key,
            source="provisioning_coordinator",
        )
        if not dispatch.queued:
            raise RuntimeError(dispatch.error or "Failed to queue provisioning task")

        return AsyncProvisioningResult(
            queued=True,
            message="Provisioning workflow queued. Track progress in operation history.",
            operation_id=str(op.id),
            correlation_key=correlation_key,
        )

    except Exception as exc:
        network_operations.mark_failed(
            db,
            str(op.id),
            f"Failed to queue provisioning: {exc}",
        )
        db.commit()
        logger.error(
            "Failed to queue provisioning for %s on %s: %s",
            serial_number,
            olt_id,
            exc,
            exc_info=True,
        )
        return AsyncProvisioningResult(
            queued=False,
            message=f"Failed to queue provisioning: {exc}",
            operation_id=str(op.id),
        )


# ---------------------------------------------------------------------------
# Convenience Functions
# ---------------------------------------------------------------------------


def provision_ont_sync(
    db: Session,
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    profile_id: str | None = None,
    force_reauthorize: bool = False,
    request: Request | None = None,
) -> ProvisioningResult:
    """Execute synchronous provisioning (blocking).

    Use this for testing or when Celery is unavailable.
    For production, prefer queue_provisioning().
    """
    coordinator = ProvisioningCoordinator(db, request=request)
    return coordinator.provision_ont(
        olt_id,
        fsp,
        serial_number,
        profile_id=profile_id,
        force_reauthorize=force_reauthorize,
    )


def provision_ont_resilient(
    db: Session,
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    profile_id: str | None = None,
    force_reauthorize: bool = False,
    request: Request | None = None,
    prefer_sync: bool = False,
) -> ProvisioningResult | AsyncProvisioningResult:
    """Provision ONT with async/sync fallback.

    Tries to queue async provisioning first. Falls back to sync execution
    if Celery is unavailable.

    Args:
        db: Database session
        olt_id: OLT device ID
        fsp: Frame/Slot/Port location
        serial_number: ONT serial number
        profile_id: Optional provisioning profile ID
        force_reauthorize: Delete existing registration first
        request: HTTP request for audit
        prefer_sync: Skip async and run synchronously

    Returns:
        ProvisioningResult (sync) or AsyncProvisioningResult (async)
    """
    from app.services.network.authorization_executor import is_celery_available

    if prefer_sync or not is_celery_available():
        logger.info(
            "Running provisioning synchronously (prefer_sync=%s, celery=%s)",
            prefer_sync,
            is_celery_available() if not prefer_sync else "skipped",
        )
        return provision_ont_sync(
            db,
            olt_id,
            fsp,
            serial_number,
            profile_id=profile_id,
            force_reauthorize=force_reauthorize,
            request=request,
        )

    return queue_provisioning(
        db,
        olt_id,
        fsp,
        serial_number,
        profile_id=profile_id,
        force_reauthorize=force_reauthorize,
        request=request,
    )
