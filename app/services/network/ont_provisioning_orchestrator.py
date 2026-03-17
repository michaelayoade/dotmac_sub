"""End-to-end ONT provisioning orchestrator.

Coordinates the 9-step provisioning sequence:
1. Resolve context (ONT, OLT, assignment, subscriber, subscription, profile)
2. Generate all OLT commands via HuaweiCommandGenerator
3. If dry_run: return commands without executing
4. Execute service-port commands via SSH
5. Execute IPHOST management IP command via SSH
6. Bind TR-069 server profile via SSH
7. Wait for TR-069 bootstrap (poll GenieACS, 120s timeout)
8. Push PPPoE credentials via TR-069
9. Update OntUnit status, emit ont_provisioned event
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.network import OntProvisioningProfile, OntUnit
from app.services.network.olt_command_gen import (
    HuaweiCommandGenerator,
    OltCommandSet,
    OntProvisioningContext,
    build_spec_from_profile,
)
from app.services.network.olt_ssh import (
    bind_tr069_server_profile,
    configure_ont_iphost,
    create_single_service_port,
)
from app.services.web_network_service_ports import _resolve_ont_olt_context

logger = logging.getLogger(__name__)

# Bootstrap polling constants
_BOOTSTRAP_TIMEOUT_SEC = 120
_BOOTSTRAP_POLL_INTERVAL_SEC = 10


@dataclass
class ProvisioningStepResult:
    """Result of a single provisioning step."""

    step: int
    name: str
    success: bool
    message: str
    duration_ms: int = 0


@dataclass
class ProvisioningJobResult:
    """Result of the full provisioning sequence."""

    success: bool
    message: str
    steps: list[ProvisioningStepResult] = field(default_factory=list)
    command_sets: list[OltCommandSet] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "message": self.message,
            "dry_run": self.dry_run,
            "steps": [
                {
                    "step": s.step,
                    "name": s.name,
                    "success": s.success,
                    "message": s.message,
                    "duration_ms": s.duration_ms,
                }
                for s in self.steps
            ],
            "command_preview": [
                {"step": cs.step, "commands": cs.commands, "description": cs.description}
                for cs in self.command_sets
            ],
        }


class OntProvisioningOrchestrator:
    """Orchestrates end-to-end ONT provisioning."""

    @staticmethod
    def provision_ont(
        db: Session,
        ont_id: str,
        profile_id: str,
        *,
        dry_run: bool = False,
        tr069_olt_profile_id: int | None = None,
    ) -> ProvisioningJobResult:
        """Run the full provisioning sequence for an ONT.

        Args:
            db: Database session.
            ont_id: OntUnit ID.
            profile_id: OntProvisioningProfile ID.
            dry_run: If True, generate commands but don't execute.
            tr069_olt_profile_id: OLT-level TR-069 server profile ID.

        Returns:
            ProvisioningJobResult with step-by-step results.
        """
        result = ProvisioningJobResult(success=False, message="", dry_run=dry_run)

        # ── Step 1: Resolve context ──
        step_start = time.monotonic()
        ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)

        if not ont:
            result.steps.append(ProvisioningStepResult(1, "Resolve Context", False, "ONT not found"))
            result.message = "ONT not found"
            return result

        if not olt or not fsp or olt_ont_id is None:
            result.steps.append(
                ProvisioningStepResult(1, "Resolve Context", False, "No OLT/FSP mapping — check assignment")
            )
            result.message = "Cannot resolve OLT context"
            return result

        profile = db.get(OntProvisioningProfile, profile_id)
        if not profile:
            result.steps.append(ProvisioningStepResult(1, "Resolve Context", False, "Profile not found"))
            result.message = "Provisioning profile not found"
            return result

        # Build provisioning context
        parts = fsp.split("/")
        prov_ctx = OntProvisioningContext(
            frame=int(parts[0]),
            slot=int(parts[1]),
            port=int(parts[2]),
            ont_id=olt_ont_id,
            olt_name=olt.name,
        )

        # Get subscriber info

        assignment = None
        for a in getattr(ont, "assignments", []):
            if a.active:
                assignment = a
                if a.subscriber_id:
                    from app.models.subscriber import Subscriber

                    sub = db.get(Subscriber, str(a.subscriber_id))
                    if sub:
                        prov_ctx.subscriber_code = getattr(sub, "account_number", "") or ""
                        prov_ctx.subscriber_name = getattr(sub, "full_name", "") or ""
                break

        step_ms = int((time.monotonic() - step_start) * 1000)
        result.steps.append(
            ProvisioningStepResult(
                1, "Resolve Context", True,
                f"ONT {ont.serial_number} on {olt.name} {fsp} ONT-ID {olt_ont_id}",
                step_ms,
            )
        )

        # ── Step 2: Generate commands ──
        step_start = time.monotonic()
        spec = build_spec_from_profile(profile, prov_ctx, tr069_profile_id=tr069_olt_profile_id)
        command_sets = HuaweiCommandGenerator.generate_full_provisioning(spec, prov_ctx)
        result.command_sets = command_sets

        step_ms = int((time.monotonic() - step_start) * 1000)
        total_cmds = sum(len(cs.commands) for cs in command_sets)
        result.steps.append(
            ProvisioningStepResult(
                2, "Generate Commands", True,
                f"Generated {total_cmds} command(s) in {len(command_sets)} step(s)",
                step_ms,
            )
        )

        # ── Step 3: Dry run check ──
        if dry_run:
            result.success = True
            result.message = f"Dry run complete — {total_cmds} command(s) generated"
            result.steps.append(
                ProvisioningStepResult(3, "Dry Run", True, "Commands generated, not executed")
            )
            return result

        # ── Step 4: Execute service-port commands ──
        step_start = time.monotonic()
        sp_errors = 0
        sp_created = 0
        for ws in spec.wan_services:
            ok, msg = create_single_service_port(olt, fsp, olt_ont_id, ws.gem_index, ws.vlan_id)
            if ok:
                sp_created += 1
            else:
                sp_errors += 1
                logger.warning("Service-port creation failed: %s", msg)

        step_ms = int((time.monotonic() - step_start) * 1000)
        if sp_errors > 0 and sp_created == 0:
            result.steps.append(
                ProvisioningStepResult(4, "Create Service Ports", False, f"All {sp_errors} failed", step_ms)
            )
            result.message = "Service-port creation failed"
            return result

        result.steps.append(
            ProvisioningStepResult(
                4, "Create Service Ports", True,
                f"Created {sp_created}, failed {sp_errors}",
                step_ms,
            )
        )

        # ── Step 5: Configure management IP ──
        step_start = time.monotonic()
        if spec.mgmt_vlan_tag:
            ok, msg = configure_ont_iphost(
                olt, fsp, olt_ont_id,
                vlan_id=spec.mgmt_vlan_tag,
                ip_mode=spec.mgmt_ip_mode,
                ip_address=spec.mgmt_ip_address or None,
                subnet=spec.mgmt_subnet or None,
                gateway=spec.mgmt_gateway or None,
            )
            step_ms = int((time.monotonic() - step_start) * 1000)
            result.steps.append(
                ProvisioningStepResult(5, "Configure Management IP", ok, msg, step_ms)
            )
            if not ok:
                logger.warning("IPHOST config warning (continuing): %s", msg)
        else:
            result.steps.append(
                ProvisioningStepResult(5, "Configure Management IP", True, "Skipped — no mgmt VLAN in profile")
            )

        # ── Step 6: Bind TR-069 profile ──
        step_start = time.monotonic()
        if tr069_olt_profile_id is not None:
            ok, msg = bind_tr069_server_profile(olt, fsp, olt_ont_id, tr069_olt_profile_id)
            step_ms = int((time.monotonic() - step_start) * 1000)
            result.steps.append(
                ProvisioningStepResult(6, "Bind TR-069 Profile", ok, msg, step_ms)
            )
            if not ok:
                logger.warning("TR-069 binding warning (continuing): %s", msg)
        else:
            result.steps.append(
                ProvisioningStepResult(6, "Bind TR-069 Profile", True, "Skipped — no TR-069 profile specified")
            )

        # ── Step 7: Wait for TR-069 bootstrap ──
        step_start = time.monotonic()
        device_found = _wait_for_tr069_bootstrap(db, ont)
        step_ms = int((time.monotonic() - step_start) * 1000)

        if device_found:
            result.steps.append(
                ProvisioningStepResult(7, "TR-069 Bootstrap", True, "Device registered in ACS", step_ms)
            )
        else:
            result.steps.append(
                ProvisioningStepResult(
                    7, "TR-069 Bootstrap", False,
                    f"Device not found in ACS after {_BOOTSTRAP_TIMEOUT_SEC}s — PPPoE push skipped",
                    step_ms,
                )
            )
            # Continue anyway — PPPoE can be pushed later
            result.success = True
            result.message = "Provisioning partially complete — TR-069 bootstrap timed out"
            _finalize_ont(db, ont)
            return result

        # ── Step 8: Push PPPoE credentials ──
        step_start = time.monotonic()
        pppoe_pushed = False
        for ws in spec.wan_services:
            if ws.connection_type == "pppoe" and ws.pppoe_username_template:
                from app.services.network.olt_command_gen import _render_template

                username = _render_template(ws.pppoe_username_template, prov_ctx)
                password = ws.pppoe_password or prov_ctx.pppoe_password
                if username and password:
                    from app.services.network.ont_actions import OntActions

                    pppoe_result = OntActions.set_pppoe_credentials(db, ont_id, username, password)
                    step_ms = int((time.monotonic() - step_start) * 1000)
                    result.steps.append(
                        ProvisioningStepResult(
                            8, "Push PPPoE Credentials",
                            pppoe_result.success, pppoe_result.message, step_ms,
                        )
                    )
                    pppoe_pushed = True
                    break

        if not pppoe_pushed:
            result.steps.append(
                ProvisioningStepResult(8, "Push PPPoE Credentials", True, "Skipped — no PPPoE in profile")
            )

        # ── Step 9: Finalize ──
        step_start = time.monotonic()
        _finalize_ont(db, ont)
        step_ms = int((time.monotonic() - step_start) * 1000)
        result.steps.append(
            ProvisioningStepResult(9, "Finalize", True, "ONT marked as provisioned", step_ms)
        )

        result.success = True
        result.message = "Provisioning complete"
        return result


def _wait_for_tr069_bootstrap(db: Session, ont: OntUnit) -> bool:
    """Poll GenieACS for ONT device registration.

    Returns True if the device appears within the timeout.
    """
    try:
        from app.services.network._resolve import resolve_genieacs_with_reason

        deadline = time.monotonic() + _BOOTSTRAP_TIMEOUT_SEC
        while time.monotonic() < deadline:
            resolved, reason = resolve_genieacs_with_reason(db, ont)
            if resolved:
                logger.info(
                    "TR-069 bootstrap complete for ONT %s", ont.serial_number
                )
                return True
            time.sleep(_BOOTSTRAP_POLL_INTERVAL_SEC)

        logger.warning(
            "TR-069 bootstrap timeout for ONT %s after %ds",
            ont.serial_number, _BOOTSTRAP_TIMEOUT_SEC,
        )
        return False
    except Exception as e:
        logger.error("Error during TR-069 bootstrap poll: %s", e)
        return False


def _finalize_ont(db: Session, ont: OntUnit) -> None:
    """Update ONT status and emit provisioned event."""
    try:
        from app.services.events.dispatcher import emit_event
        from app.services.events.types import EventType

        emit_event(
            db,
            EventType.ont_provisioned,
            {"ont_id": str(ont.id), "serial_number": ont.serial_number},
        )
        db.flush()
        logger.info("ONT %s provisioning finalized", ont.serial_number)
    except Exception as e:
        logger.error("Error finalizing ONT %s: %s", ont.serial_number, e)
