"""Direct ONT provisioning orchestration.

This is an explicit linear flow driven by OLT defaults plus
``OntUnit.desired_config``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.services.network.ont_provisioning.result import StepResult


@dataclass
class OntProvisioningResult:
    """Result returned by the direct ONT provisioning orchestrator."""

    success: bool
    message: str
    ont_id: str
    duration_ms: int
    steps: list[StepResult]
    failed_step: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "message": self.message,
            "ont_id": self.ont_id,
            "duration_ms": self.duration_ms,
            "steps": [
                {
                    "step_name": step.step_name,
                    "success": step.success,
                    "message": step.message,
                    "duration_ms": step.duration_ms,
                    "critical": step.critical,
                    "skipped": step.skipped,
                    "waiting": step.waiting,
                    "data": step.data,
                }
                for step in self.steps
            ],
            "failed_step": self.failed_step,
        }


def _finish(
    *,
    ont_id: str,
    t0: float,
    steps: list[StepResult],
    success: bool,
    message: str,
    failed_step: str | None = None,
) -> OntProvisioningResult:
    return OntProvisioningResult(
        success=success,
        message=message,
        ont_id=ont_id,
        duration_ms=int((time.monotonic() - t0) * 1000),
        steps=steps,
        failed_step=failed_step,
    )


def provision_ont_from_desired_config(
    db: Session,
    ont_id: str,
    *,
    dry_run: bool = False,
    allow_low_optical_margin: bool = False,
    wait_for_acs: bool = True,
    apply_acs_config: bool = True,
) -> OntProvisioningResult:
    """Provision one ONT from OLT defaults plus ``OntUnit.desired_config``."""
    from app.services.network.ont_provision_steps import (
        apply_saved_service_config,
        provision_with_reconciliation,
        wait_tr069_bootstrap,
    )
    from app.models.network import OntUnit
    from app.services.network.effective_ont_config import resolve_effective_ont_config

    t0 = time.monotonic()
    steps: list[StepResult] = []
    try:
        ont = db.get(OntUnit, ont_id)
    except Exception:
        ont = None
    effective_values = (
        resolve_effective_ont_config(db, ont).get("values", {}) if ont else {}
    )
    has_tr069 = bool(
        effective_values.get("tr069_acs_server_id")
        and effective_values.get("tr069_olt_profile_id")
    )

    provision_result = provision_with_reconciliation(
        db,
        ont_id,
        dry_run=dry_run,
        allow_low_optical_margin=allow_low_optical_margin,
    )
    steps.append(provision_result)
    if not provision_result.success:
        return _finish(
            ont_id=ont_id,
            t0=t0,
            steps=steps,
            success=False,
            message=provision_result.message,
            failed_step=provision_result.step_name,
        )
    if dry_run:
        return _finish(
            ont_id=ont_id,
            t0=t0,
            steps=steps,
            success=True,
            message=provision_result.message,
        )

    if wait_for_acs and has_tr069:
        wait_result = wait_tr069_bootstrap(db, ont_id)
        steps.append(wait_result)
        if not wait_result.success:
            return _finish(
                ont_id=ont_id,
                t0=t0,
                steps=steps,
                success=False,
                message=wait_result.message,
                failed_step=wait_result.step_name,
            )

    if apply_acs_config and has_tr069:
        acs_result = apply_saved_service_config(db, ont_id)
        steps.append(acs_result)
        if not acs_result.success:
            return _finish(
                ont_id=ont_id,
                t0=t0,
                steps=steps,
                success=False,
                message=acs_result.message,
                failed_step=acs_result.step_name,
            )

    return _finish(
        ont_id=ont_id,
        t0=t0,
        steps=steps,
        success=True,
        message="ONT provisioning completed",
    )
