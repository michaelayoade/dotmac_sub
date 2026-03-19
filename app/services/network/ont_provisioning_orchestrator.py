"""End-to-end ONT provisioning orchestrator.

Coordinates the 13-step provisioning sequence:
1. Resolve context (ONT, OLT, assignment, subscriber, subscription, profile)
2. Generate all OLT commands via HuaweiCommandGenerator
3. If dry_run: return commands without executing
4. Execute service-port commands via SSH
5. Execute IPHOST management IP command via SSH
6. Activate internet-config (TCP stack) via SSH
7. Set WAN route+NAT mode via SSH
8. Bind TR-069 server profile via SSH
9. Wait for TR-069 bootstrap (poll GenieACS, 120s timeout)
10. Set connection request credentials via TR-069
11. Configure PPPoE via OMCI (OLT-side)
12. Push PPPoE credentials via TR-069
13. Update OntUnit status, emit ont_provisioned event
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntProvisioningProfile, OntProvisioningStatus, OntUnit
from app.services.credential_crypto import decrypt_credential
from app.services.network.olt_command_gen import (
    HuaweiCommandGenerator,
    OltCommandSet,
    OntProvisioningContext,
    WanServiceSpec,
    build_spec_from_profile,
)
from app.services.network.olt_ssh_ont import (
    bind_tr069_server_profile,
    configure_ont_internet_config,
    configure_ont_iphost,
    configure_ont_pppoe_omci,
    configure_ont_wan_config,
)
from app.services.network.olt_ssh_service_ports import (
    create_single_service_port,
    delete_service_port,
    get_service_ports_for_ont,
)
from app.services.web_network_service_ports import _resolve_ont_olt_context

logger = logging.getLogger(__name__)

_CREDENTIAL_KEYWORDS = ("password", "secret", "Password")


def _mask_credentials(cmd: str) -> str:
    """Mask credential values in OLT CLI command strings for safe logging/display."""
    for kw in _CREDENTIAL_KEYWORDS:
        idx = cmd.find(f" {kw} ")
        if idx != -1:
            prefix = cmd[: idx + len(kw) + 2]
            rest = cmd[idx + len(kw) + 2 :]
            # Mask until next space or end of string
            next_space = rest.find(" ")
            if next_space == -1:
                cmd = prefix + "********"
            else:
                cmd = prefix + "********" + rest[next_space:]
    return cmd


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
                {
                    "step": cs.step,
                    "commands": [_mask_credentials(c) for c in cs.commands],
                    "description": cs.description,
                }
                for cs in self.command_sets
            ],
        }


def _rollback_service_ports(
    olt: Any,
    fsp: str,
    olt_ont_id: int,
) -> tuple[int, int]:
    """Attempt to remove service-ports created for an ONT during provisioning.

    Returns (deleted, errors) counts.
    """
    ok, _msg, ports = get_service_ports_for_ont(olt, fsp, olt_ont_id)
    if not ok or not ports:
        return 0, 0

    deleted = 0
    errors = 0
    for port in ports:
        ok, msg = delete_service_port(olt, port.index)
        if ok:
            deleted += 1
        else:
            errors += 1
            logger.warning("Rollback: failed to delete service-port %d: %s", port.index, msg)
    return deleted, errors


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

        # ── Step 3b: Validate VLAN chain ──
        if spec.wan_services:
            from app.models.network import Vlan

            wan_vlans = {ws.vlan_id for ws in spec.wan_services if ws.vlan_id}
            known_tags_stmt = select(Vlan.tag).where(Vlan.tag.in_(wan_vlans))
            known_tags = set(db.scalars(known_tags_stmt).all())
            missing_vlans = wan_vlans - known_tags
            if missing_vlans:
                logger.warning(
                    "VLAN validation: tags %s not found in VLAN table (may exist on OLT but not in app)",
                    sorted(missing_vlans),
                )

        # ── Step 4: Execute service-port commands ──
        step_start = time.monotonic()
        sp_errors = 0
        sp_created = 0
        for ws in spec.wan_services:
            ok, msg = create_single_service_port(
                olt,
                fsp,
                olt_ont_id,
                ws.gem_index,
                ws.vlan_id,
                user_vlan=ws.user_vlan,
                tag_transform=ws.tag_transform,
            )
            if ok:
                sp_created += 1
            else:
                sp_errors += 1
                logger.warning("Service-port creation failed: %s", msg)

        step_ms = int((time.monotonic() - step_start) * 1000)
        if sp_errors > 0:
            message = (
                f"Created {sp_created}, failed {sp_errors}"
                if sp_created > 0
                else f"All {sp_errors} failed"
            )
            result.steps.append(
                ProvisioningStepResult(4, "Create Service Ports", False, message, step_ms)
            )
            if sp_created == 0:
                result.message = "Service-port creation failed"
                _finalize_ont(db, ont, profile_id=profile_id, success=False)
                return result
        else:
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

        # ── Step 6: Activate internet-config ──
        step_start = time.monotonic()
        if spec.internet_config_ip_index is not None:
            ok, msg = configure_ont_internet_config(
                olt, fsp, olt_ont_id, ip_index=spec.internet_config_ip_index,
            )
            step_ms = int((time.monotonic() - step_start) * 1000)
            result.steps.append(
                ProvisioningStepResult(6, "Activate Internet Config", ok, msg, step_ms)
            )
            if not ok:
                logger.warning("Internet config warning (continuing): %s", msg)
        else:
            result.steps.append(
                ProvisioningStepResult(6, "Activate Internet Config", True, "Skipped — not configured in profile")
            )

        # ── Step 7: Set WAN route+NAT mode ──
        step_start = time.monotonic()
        if spec.wan_config_profile_id is not None:
            ok, msg = configure_ont_wan_config(
                olt, fsp, olt_ont_id,
                ip_index=spec.internet_config_ip_index or 0,
                profile_id=spec.wan_config_profile_id,
            )
            step_ms = int((time.monotonic() - step_start) * 1000)
            result.steps.append(
                ProvisioningStepResult(7, "Set WAN Route+NAT Mode", ok, msg, step_ms)
            )
            if not ok:
                logger.warning("WAN config warning (continuing): %s", msg)
        else:
            result.steps.append(
                ProvisioningStepResult(7, "Set WAN Route+NAT Mode", True, "Skipped — not configured in profile")
            )

        # ── Step 8: Bind TR-069 profile ──
        step_start = time.monotonic()
        if tr069_olt_profile_id is not None:
            ok, msg = bind_tr069_server_profile(olt, fsp, olt_ont_id, tr069_olt_profile_id)
            step_ms = int((time.monotonic() - step_start) * 1000)
            result.steps.append(
                ProvisioningStepResult(8, "Bind TR-069 Profile", ok, msg, step_ms)
            )
            if not ok:
                logger.warning("TR-069 binding warning (continuing): %s", msg)
        else:
            result.steps.append(
                ProvisioningStepResult(8, "Bind TR-069 Profile", True, "Skipped — no TR-069 profile specified")
            )

        tr069_enabled = tr069_olt_profile_id is not None
        device_found = False
        if tr069_enabled:
            # ── Step 9: Wait for TR-069 bootstrap ──
            step_start = time.monotonic()
            device_found = _wait_for_tr069_bootstrap(db, ont)
            step_ms = int((time.monotonic() - step_start) * 1000)

            if device_found:
                result.steps.append(
                    ProvisioningStepResult(9, "TR-069 Bootstrap", True, "Device registered in ACS", step_ms)
                )
            else:
                result.steps.append(
                    ProvisioningStepResult(
                        9, "TR-069 Bootstrap", False,
                        f"Device not found in ACS after {_BOOTSTRAP_TIMEOUT_SEC}s — remaining steps skipped",
                        step_ms,
                    )
                )
                result.success = False
                result.message = "Provisioning incomplete — TR-069 bootstrap timed out"
                _finalize_ont(db, ont, profile_id=profile_id, success=False)
                return result
        else:
            result.steps.append(
                ProvisioningStepResult(
                    9, "TR-069 Bootstrap", True, "Skipped — no TR-069 profile specified",
                )
            )

        # ── Step 10: Set connection request credentials ──
        step_start = time.monotonic()
        if tr069_enabled and device_found:
            from app.services.network.ont_actions import OntActions

            cr_user = getattr(profile, "cr_username", None) or ""
            cr_pass = getattr(profile, "cr_password", None) or ""
            if not cr_user:
                cr_user = "acs"  # noqa: S105
            if not cr_pass:
                cr_pass = cr_user  # noqa: S105
            cr_result = OntActions.set_connection_request_credentials(
                db, ont_id, username=cr_user, password=cr_pass,
            )
            step_ms = int((time.monotonic() - step_start) * 1000)
            result.steps.append(
                ProvisioningStepResult(10, "Set Connection Request Credentials", cr_result.success, cr_result.message, step_ms)
            )
            if not cr_result.success:
                logger.warning("Connection request credentials warning (continuing): %s", cr_result.message)
        else:
            result.steps.append(
                ProvisioningStepResult(
                    10, "Set Connection Request Credentials", True,
                    "Skipped — TR-069 not active",
                )
            )

        # ── Step 11: Configure PPPoE via OMCI ──
        step_start = time.monotonic()
        pppoe_services = [ws for ws in spec.wan_services if ws.connection_type == "pppoe"]
        if spec.pppoe_omci_vlan and pppoe_services:
            omci_errors = 0
            omci_ok = 0
            for i, ws in enumerate(pppoe_services, start=1):
                username, password, reason = _resolve_pppoe_service_credentials(
                    db, assignment, ws, prov_ctx,
                )
                if not username or not password:
                    logger.warning("PPPoE OMCI skipped for service #%d: %s", i, reason)
                    omci_errors += 1
                    continue
                ok, msg = configure_ont_pppoe_omci(
                    olt, fsp, olt_ont_id,
                    ip_index=i,
                    vlan_id=spec.pppoe_omci_vlan,
                    username=username,
                    password=password,
                )
                if ok:
                    omci_ok += 1
                else:
                    omci_errors += 1
                    logger.warning("PPPoE OMCI failed for service #%d: %s", i, msg)

            step_ms = int((time.monotonic() - step_start) * 1000)
            omci_success = omci_ok > 0
            omci_msg = f"Configured {omci_ok}, failed {omci_errors}"
            result.steps.append(
                ProvisioningStepResult(11, "Configure PPPoE via OMCI", omci_success, omci_msg, step_ms)
            )
        else:
            result.steps.append(
                ProvisioningStepResult(11, "Configure PPPoE via OMCI", True, "Skipped — no OMCI VLAN or no PPPoE services")
            )

        # ── Step 12: Push PPPoE credentials via TR-069 ──
        step_start = time.monotonic()
        if not pppoe_services:
            result.steps.append(
                ProvisioningStepResult(12, "Push PPPoE Credentials", True, "Skipped — no PPPoE in profile")
            )
        elif not tr069_enabled or not device_found:
            result.steps.append(
                ProvisioningStepResult(
                    12, "Push PPPoE Credentials", False,
                    "PPPoE push requires a TR-069 profile and successful bootstrap",
                )
            )
        else:
            from app.services.network.ont_actions import OntActions

            pppoe_results: list[ProvisioningStepResult] = []
            pppoe_index = 0
            for ws in pppoe_services:
                pppoe_index += 1
                username, password, reason = _resolve_pppoe_service_credentials(
                    db, assignment, ws, prov_ctx,
                )
                step_ms = int((time.monotonic() - step_start) * 1000)
                if not username or not password:
                    pppoe_results.append(
                        ProvisioningStepResult(
                            12, "Push PPPoE Credentials", False,
                            reason or f"Missing PPPoE credentials for service #{pppoe_index}",
                            step_ms,
                        )
                    )
                    continue

                pppoe_result = OntActions.set_pppoe_credentials(
                    db, ont_id, username, password,
                    instance_index=pppoe_index,
                )
                pppoe_results.append(
                    ProvisioningStepResult(
                        12, "Push PPPoE Credentials",
                        pppoe_result.success,
                        f"Service #{pppoe_index}: {pppoe_result.message}",
                        step_ms,
                    )
                )

            result.steps.extend(pppoe_results)

        # ── Step 12.5: Enable IPv6 dual-stack via TR-069 (if profile requests it) ──
        if spec.ipv6_enabled and tr069_enabled and device_found:
            step_start = time.monotonic()
            try:
                from app.services.network.ont_action_network import enable_ipv6_on_wan

                v6_result = enable_ipv6_on_wan(db, str(ont.id))
                step_ms = int((time.monotonic() - step_start) * 1000)
                result.steps.append(
                    ProvisioningStepResult(
                        12, "Enable IPv6 Dual-Stack", v6_result.success,
                        v6_result.message, step_ms,
                    )
                )
            except Exception as exc:
                logger.error("IPv6 dual-stack enable failed for ONT %s: %s", ont.serial_number, exc)
                step_ms = int((time.monotonic() - step_start) * 1000)
                result.steps.append(
                    ProvisioningStepResult(
                        12, "Enable IPv6 Dual-Stack", False,
                        f"IPv6 enable failed: {exc}", step_ms,
                    )
                )

        # Check for any failed required steps
        # Steps 6 (internet-config) and 7 (wan-config) are best-effort — not critical
        critical_steps = {4, 5, 8, 11, 12}
        failed_steps = [s for s in result.steps if not s.success and s.step in critical_steps]

        # ── Step 13: Finalize ──
        step_start = time.monotonic()
        finalize_success = len(failed_steps) == 0
        _finalize_ont(db, ont, profile_id=profile_id, success=finalize_success)
        step_ms = int((time.monotonic() - step_start) * 1000)

        if failed_steps:
            failed_names = ", ".join(s.name for s in failed_steps)
            # Rollback service ports if provisioning failed
            if sp_created > 0:
                rb_deleted, rb_errors = _rollback_service_ports(olt, fsp, olt_ont_id)
                rollback_msg = f"Rollback: removed {rb_deleted} service-port(s)"
                if rb_errors:
                    rollback_msg += f", {rb_errors} failed"
                logger.info("%s for ONT %s", rollback_msg, ont.serial_number)
            else:
                rollback_msg = "No service ports to roll back"
            result.steps.append(
                ProvisioningStepResult(
                    13, "Finalize", False,
                    f"ONT marked as failed ({failed_names}). {rollback_msg}",
                    step_ms,
                )
            )
            result.success = False
            result.message = f"Provisioning incomplete — failed: {failed_names}"
        else:
            result.steps.append(
                ProvisioningStepResult(13, "Finalize", True, "ONT marked as provisioned", step_ms)
            )
            result.success = True
            result.message = "Provisioning complete"

        return result


def _resolve_pppoe_service_credentials(
    db: Session,
    assignment: Any,
    service: WanServiceSpec,
    prov_ctx: OntProvisioningContext,
) -> tuple[str, str, str | None]:
    """Resolve PPPoE credentials for a WAN service based on its password mode."""
    from app.models.catalog import AccessCredential
    from app.services.network.olt_command_gen import _render_template
    from app.services.pppoe_credentials import auto_generate_pppoe_credential

    subscriber_id = getattr(assignment, "subscriber_id", None)
    username = ""
    password = ""

    if service.pppoe_username_template:
        username = _render_template(service.pppoe_username_template, prov_ctx).strip()

    mode = service.pppoe_password_mode or ""
    if not mode and service.pppoe_password:
        mode = "static"

    credential = None
    if subscriber_id and mode in {"from_credential", "generate"}:
        stmt = (
            select(AccessCredential)
            .where(AccessCredential.subscriber_id == subscriber_id)
            .where(AccessCredential.is_active.is_(True))
            .order_by(AccessCredential.updated_at.desc(), AccessCredential.created_at.desc())
        )
        credential = db.scalars(stmt).first()

    if mode == "generate" and credential is None and subscriber_id:
        credential = auto_generate_pppoe_credential(db, str(subscriber_id))

    if mode == "static":
        password = service.pppoe_password or ""
    elif credential is not None:
        username = username or str(getattr(credential, "username", "") or "").strip()
        password = decrypt_credential(getattr(credential, "secret_hash", None)) or ""

    if not username:
        return "", "", "PPPoE username could not be resolved from the profile or subscriber credential"
    if not password:
        return "", "", "PPPoE password could not be resolved for the selected password mode"
    return username, password, None


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


def _finalize_ont(
    db: Session, ont: OntUnit, profile_id: str | None = None, *, success: bool
) -> None:
    """Update ONT provisioning state and emit the success event when applicable."""
    from datetime import UTC, datetime

    try:
        from app.services.events.dispatcher import emit_event
        from app.services.events.types import EventType

        if profile_id:
            from app.services.common import coerce_uuid

            ont.provisioning_profile_id = coerce_uuid(profile_id)

        if success:
            ont.provisioning_status = OntProvisioningStatus.provisioned
            ont.last_provisioned_at = datetime.now(UTC)
            emit_event(
                db,
                EventType.ont_provisioned,
                {"ont_id": str(ont.id), "serial_number": ont.serial_number},
            )
        else:
            ont.provisioning_status = OntProvisioningStatus.failed
            ont.last_provisioned_at = None

        db.flush()
        logger.info("ONT %s provisioning finalized", ont.serial_number)
    except Exception as e:
        logger.error("Error finalizing ONT %s: %s", ont.serial_number, e, exc_info=True)
        raise
