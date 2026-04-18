"""ONT provisioning executor with compensation-based rollback.

This module executes provisioning deltas using batched SSH commands
and maintains a compensation log for rollback on failure.

Key concepts:
- Each step registers its undo action (compensation) before execution
- On failure, compensation actions are executed in reverse order
- Single SSH session for all commands (avoids connection exhaustion)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.services.network.olt_ssh_session import (
    CliMode,
    OltSession,
    olt_session,
)
from app.services.network.ont_provisioning.state import (
    DesiredOntState,
    DesiredServicePort,
    ProvisioningAction,
    ProvisioningDelta,
)

if TYPE_CHECKING:
    from app.models.network import OLTDevice
    from app.services.network.ont_provisioning.state import DesiredManagementConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FSP Validation
# ---------------------------------------------------------------------------

_FSP_PATTERN = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{1,3})$")
_FSP_PREFIX_RE = re.compile(r"^(?:x?g?pon|epon|port|gei|ge|eth)[-_]?", re.IGNORECASE)


def _normalize_fsp(fsp: str) -> str:
    """Normalize FSP by stripping common port name prefixes like 'pon-'."""
    if not fsp:
        return fsp
    return _FSP_PREFIX_RE.sub("", fsp.strip())


def _validate_fsp(fsp: str) -> tuple[bool, str]:
    """Validate Frame/Slot/Port format (e.g., '0/2/1').

    Returns:
        Tuple of (is_valid, error_message).
    """
    check_fsp = _normalize_fsp(fsp)
    if not _FSP_PATTERN.match(check_fsp):
        return False, f"Invalid FSP format: {fsp!r} (expected digits/digits/digits)"
    return True, ""


def _get_interface_path(fsp: str) -> str:
    """Extract board/slot from FSP for interface command.

    Args:
        fsp: Frame/Slot/Port (e.g., "0/2/1" or "pon-0/2/1")

    Returns:
        Board/slot portion (e.g., "0/2")

    Raises:
        ValueError: If FSP format is invalid.
    """
    normalized = _normalize_fsp(fsp)
    match = _FSP_PATTERN.match(normalized)
    if not match:
        raise ValueError(f"Invalid FSP format: {fsp!r}")
    return f"{match.group(1)}/{match.group(2)}"


# ---------------------------------------------------------------------------
# Compensation Log
# ---------------------------------------------------------------------------


@dataclass
class CompensationEntry:
    """A single compensation action for rollback.

    Attributes:
        step_name: Name of the step that registered this compensation.
        undo_commands: List of OLT CLI commands to undo the change (executed in order).
        description: Human-readable description of what will be undone.
        resource_id: Optional identifier for the resource (e.g., service-port index).
        interface_path: Optional interface path to enter before running commands.
    """

    step_name: str
    undo_commands: list[str]
    description: str
    resource_id: str | None = None
    interface_path: str | None = None


@dataclass
class ProvisioningExecutionResult:
    """Result of executing a provisioning delta.

    Attributes:
        success: True if all steps completed successfully.
        message: Summary message.
        steps_completed: List of step names that completed successfully.
        steps_failed: List of step names that failed.
        compensation_log: List of compensation entries for rollback.
        errors: List of error messages from failed steps.
    """

    success: bool
    message: str
    steps_completed: list[str] = field(default_factory=list)
    steps_failed: list[str] = field(default_factory=list)
    compensation_log: list[CompensationEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def rollback(
        self,
        olt: OLTDevice,
        ont_unit_id: str | None = None,
        db: Session | None = None,
    ) -> list[tuple[str, bool, str]]:
        """Execute compensation actions in reverse order.

        Args:
            olt: The OLT device to connect to.
            ont_unit_id: Optional ONT unit ID for failure persistence.
            db: Optional database session for persisting failures.

        Returns:
            List of (step_name, success, message) tuples for each compensation.
        """
        if not self.compensation_log:
            return []

        results: list[tuple[str, bool, str]] = []
        failed_entries: list[tuple[CompensationEntry, str]] = []

        try:
            with olt_session(olt) as session:
                # Execute compensation in reverse order
                for entry in reversed(self.compensation_log):
                    try:
                        step_success = _execute_compensation_entry(session, entry)
                        results.append(
                            (entry.step_name, step_success, entry.description)
                        )
                        if step_success:
                            logger.info(
                                "Rollback: %s - %s", entry.step_name, entry.description
                            )
                        else:
                            logger.error(
                                "Rollback failed: %s - %s",
                                entry.step_name,
                                entry.description,
                                extra={
                                    "event": "provisioning_compensation_failed",
                                    "step": entry.step_name,
                                    "resource_id": entry.resource_id,
                                },
                            )
                            failed_entries.append(
                                (entry, "Compensation command returned failure")
                            )
                    except Exception as exc:
                        logger.error(
                            "Rollback error: %s - %s: %s",
                            entry.step_name,
                            entry.description,
                            exc,
                            extra={
                                "event": "provisioning_compensation_error",
                                "step": entry.step_name,
                                "resource_id": entry.resource_id,
                            },
                        )
                        results.append((entry.step_name, False, str(exc)))
                        failed_entries.append((entry, str(exc)))

        except Exception as exc:
            logger.error(
                "Failed to establish rollback connection: %s",
                exc,
                extra={"event": "provisioning_compensation_connection_failed"},
            )
            # Return error for all remaining entries
            for entry in self.compensation_log:
                if not any(r[0] == entry.step_name for r in results):
                    results.append(
                        (entry.step_name, False, f"Connection failed: {exc}")
                    )
                    failed_entries.append((entry, f"Connection failed: {exc}"))

        # Persist failed compensation entries and emit alert
        if failed_entries and db is not None:
            _persist_compensation_failures(
                db, olt, ont_unit_id, failed_entries, operation_type="provisioning"
            )
            _emit_compensation_failure_alert(db, olt, ont_unit_id, failed_entries)

        return results


def _execute_compensation_entry(session: OltSession, entry: CompensationEntry) -> bool:
    """Execute a single compensation entry with proper interface handling.

    Args:
        session: The OLT SSH session.
        entry: The compensation entry to execute.

    Returns:
        True if all commands succeeded, False otherwise.
    """
    try:
        # Enter interface mode if needed
        if entry.interface_path:
            session.run_command(
                f"interface gpon {entry.interface_path}", require_mode=CliMode.CONFIG
            )

        # Execute each undo command
        all_success = True
        for cmd in entry.undo_commands:
            result = session.run_command(cmd)
            if not (result.success or result.is_idempotent_success):
                logger.warning(
                    "Compensation command failed: %s -> %s", cmd, result.message
                )
                all_success = False

        # Exit interface mode if we entered it
        if entry.interface_path:
            session.run_command("quit")

        return all_success
    except Exception as exc:
        logger.error("Error executing compensation: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Execution Functions
# ---------------------------------------------------------------------------


def execute_delta(
    olt: OLTDevice,
    delta: ProvisioningDelta,
    desired: DesiredOntState,
    *,
    dry_run: bool = False,
) -> ProvisioningExecutionResult:
    """Execute provisioning changes with compensation registration.

    Uses a single SSH session for all commands and registers compensation
    actions before each change so rollback is possible on failure.

    Args:
        olt: The OLT device.
        delta: The validated provisioning delta.
        desired: The desired ONT state.
        dry_run: If True, don't execute commands, just return what would be done.

    Returns:
        ProvisioningExecutionResult with outcome and compensation log.
    """
    result = ProvisioningExecutionResult(
        success=False,
        message="",
    )

    # Validate FSP format
    fsp_valid, fsp_err = _validate_fsp(desired.fsp)
    if not fsp_valid:
        result.message = f"Invalid FSP: {fsp_err}"
        result.errors.append(fsp_err)
        return result

    # Check validations passed
    if not delta.is_valid:
        validation_errors = []
        if not delta.optical_budget_ok:
            validation_errors.append(f"Optical: {delta.optical_budget_message}")
        if not delta.mgmt_vlan_trunked:
            validation_errors.append(f"VLAN: {delta.mgmt_vlan_message}")
        if not delta.service_vlans_ok:
            validation_errors.append(f"Service VLANs: {delta.service_vlans_message}")
        if not delta.ip_index_valid:
            validation_errors.append(f"IP Index: {delta.ip_index_message}")

        result.message = f"Validation failed: {'; '.join(validation_errors)}"
        result.errors = validation_errors
        return result

    # Check if there are any changes to make
    if not delta.has_changes:
        result.success = True
        result.message = "No changes needed - ONT already matches desired state"
        return result

    if dry_run:
        return _dry_run_delta(delta, desired)

    # Get interface path for commands that need it
    interface_path = _get_interface_path(desired.fsp)

    # Execute with single SSH session
    try:
        with olt_session(olt) as session:
            # Track successfully deleted indices for dependency verification
            deleted_indices: set[int] = set()

            # Execute service port changes
            for idx, sp_delta in enumerate(delta.service_port_deltas):
                if sp_delta.action == ProvisioningAction.NOOP:
                    continue

                if sp_delta.action == ProvisioningAction.DELETE:
                    if not sp_delta.actual:
                        continue
                    step_result = _execute_delete_service_port(
                        session, sp_delta.actual.index, result
                    )
                    if not step_result:
                        result.message = (
                            f"Failed to delete service-port {sp_delta.actual.index}"
                        )
                        return result
                    # Track successful deletion for dependency verification
                    deleted_indices.add(idx)

                elif sp_delta.action == ProvisioningAction.CREATE:
                    if not sp_delta.desired:
                        continue
                    # Verify dependent DELETE succeeded before CREATE
                    if sp_delta.depends_on_delete_index is not None:
                        if sp_delta.depends_on_delete_index not in deleted_indices:
                            result.message = (
                                f"Cannot create service-port VLAN {sp_delta.desired.vlan_id}: "
                                f"required DELETE at index {sp_delta.depends_on_delete_index} did not complete"
                            )
                            result.steps_failed.append(
                                f"create_service_port_vlan_{sp_delta.desired.vlan_id}"
                            )
                            result.errors.append(result.message)
                            return result
                        # Small delay after dependent DELETE for OLT state sync
                        import time

                        time.sleep(0.5)

                    step_result = _execute_create_service_port(
                        session,
                        desired.fsp,
                        desired.olt_ont_id,
                        sp_delta.desired,
                        result,
                    )
                    if not step_result:
                        result.message = f"Failed to create service-port VLAN {sp_delta.desired.vlan_id}"
                        return result

            # Execute management IP configuration if needed
            if delta.needs_mgmt_ip_config and desired.management:
                step_result = _execute_management_ip_config(
                    session,
                    interface_path,
                    desired.olt_ont_id,
                    desired.management,
                    result,
                )
                if not step_result:
                    result.message = "Failed to configure management IP"
                    return result

            # Execute internet-config if needed
            if (
                delta.needs_internet_config
                and desired.internet_config_ip_index is not None
            ):
                step_result = _execute_internet_config(
                    session,
                    interface_path,
                    desired.olt_ont_id,
                    desired.internet_config_ip_index,
                    result,
                )
                if not step_result:
                    result.message = "Failed to activate internet-config"
                    return result

            # Execute wan-config if needed
            if delta.needs_wan_config and desired.wan_config_profile_id is not None:
                step_result = _execute_wan_config(
                    session,
                    interface_path,
                    desired.olt_ont_id,
                    desired.internet_config_ip_index or 0,
                    desired.wan_config_profile_id,
                    result,
                )
                if not step_result:
                    result.message = "Failed to configure WAN mode"
                    return result

            # Execute TR-069 binding if needed
            if delta.needs_tr069_bind and desired.tr069:
                step_result = _execute_tr069_bind(
                    session,
                    interface_path,
                    desired.olt_ont_id,
                    desired.tr069.olt_profile_id,
                    result,
                )
                if not step_result:
                    result.message = "Failed to bind TR-069 profile"
                    return result

    except Exception as exc:
        logger.error("Provisioning execution failed: %s", exc)
        result.message = f"Execution error: {exc}"
        result.errors.append(str(exc))
        return result

    result.success = True
    result.message = (
        f"Provisioning complete: {len(result.steps_completed)} step(s) executed"
    )
    return result


def _dry_run_delta(
    delta: ProvisioningDelta,
    desired: DesiredOntState,
) -> ProvisioningExecutionResult:
    """Generate dry-run result showing what would be executed.

    Args:
        delta: The provisioning delta.
        desired: The desired state.

    Returns:
        ProvisioningExecutionResult with planned steps (not executed).
    """
    result = ProvisioningExecutionResult(
        success=True,
        message="Dry run - no changes made",
    )

    # List service port operations
    for sp_delta in delta.service_port_deltas:
        if sp_delta.action == ProvisioningAction.CREATE and sp_delta.desired:
            result.steps_completed.append(
                f"[DRY RUN] Create service-port VLAN {sp_delta.desired.vlan_id} GEM {sp_delta.desired.gem_index}"
            )
        elif sp_delta.action == ProvisioningAction.DELETE and sp_delta.actual:
            result.steps_completed.append(
                f"[DRY RUN] Delete service-port index {sp_delta.actual.index}"
            )

    if delta.needs_mgmt_ip_config:
        result.steps_completed.append("[DRY RUN] Configure management IP")

    if delta.needs_internet_config:
        result.steps_completed.append("[DRY RUN] Activate internet-config")

    if delta.needs_wan_config:
        result.steps_completed.append("[DRY RUN] Configure WAN mode")

    if delta.needs_tr069_bind:
        result.steps_completed.append("[DRY RUN] Bind TR-069 profile")

    return result


# ---------------------------------------------------------------------------
# Individual Step Executors
# ---------------------------------------------------------------------------


def _parse_service_port_index(output: str) -> int | None:
    """Parse service-port index from OLT command output.

    Huawei OLTs typically return the assigned index in the success message.

    Args:
        output: Command output from service-port creation.

    Returns:
        The service-port index if found, None otherwise.
    """
    # Pattern: "service-port 123" or "Index: 123" or similar
    patterns = [
        r"service-port\s+(\d+)",
        r"index[:\s]+(\d+)",
        r"port\s+(\d+)\s+created",
    ]
    for pattern in patterns:
        match = re.search(pattern, output, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _execute_create_service_port(
    session: OltSession,
    fsp: str,
    olt_ont_id: int,
    desired_port: DesiredServicePort,
    result: ProvisioningExecutionResult,
) -> bool:
    """Execute service-port creation with compensation registration.

    Returns True on success, False on failure.
    """
    from app.services.network.olt_command_gen import build_service_port_command

    # Build the creation command
    cmd = build_service_port_command(
        fsp=fsp,
        ont_id=olt_ont_id,
        gem_index=desired_port.gem_index,
        vlan_id=desired_port.vlan_id,
        user_vlan=desired_port.user_vlan,
        tag_transform=desired_port.tag_transform,
    )

    # Execute command
    cmd_result = session.run_config_command(cmd)

    if cmd_result.success or cmd_result.is_idempotent_success:
        step_name = f"create_service_port_vlan_{desired_port.vlan_id}"
        result.steps_completed.append(step_name)

        # Try to parse the assigned service-port index for reliable rollback
        sp_index = _parse_service_port_index(cmd_result.output)

        # Register compensation using index-based deletion (preferred) or fallback
        if sp_index is not None:
            undo_cmd = f"undo service-port {sp_index}"
            resource_id = str(sp_index)
        else:
            # Fallback: query and delete by matching criteria
            # This is less reliable but works when index isn't returned
            undo_cmd = (
                f"undo service-port {sp_index}"
                if sp_index
                else f"undo service-port port {fsp} ont {olt_ont_id}"
            )
            resource_id = f"vlan_{desired_port.vlan_id}_gem_{desired_port.gem_index}"
            logger.debug(
                "Could not parse service-port index from output, using fallback deletion"
            )

        result.compensation_log.append(
            CompensationEntry(
                step_name=step_name,
                undo_commands=[undo_cmd],
                description=f"Delete service-port VLAN {desired_port.vlan_id} GEM {desired_port.gem_index}",
                resource_id=resource_id,
            )
        )

        logger.info(
            "Created service-port VLAN %d GEM %d for ONT %d (index: %s)",
            desired_port.vlan_id,
            desired_port.gem_index,
            olt_ont_id,
            sp_index or "unknown",
        )
        return True

    # Failure
    result.steps_failed.append(f"create_service_port_vlan_{desired_port.vlan_id}")
    result.errors.append(
        f"Failed to create service-port VLAN {desired_port.vlan_id}: {cmd_result.message}"
    )
    logger.error(
        "Failed to create service-port VLAN %d GEM %d: %s",
        desired_port.vlan_id,
        desired_port.gem_index,
        cmd_result.message,
    )
    return False


def _execute_delete_service_port(
    session: OltSession,
    index: int,
    result: ProvisioningExecutionResult,
) -> bool:
    """Execute service-port deletion.

    Note: Deletion is typically not compensated (we don't recreate deleted ports).
    """
    cmd = f"undo service-port {index}"
    cmd_result = session.run_config_command(cmd)

    if cmd_result.success or cmd_result.is_idempotent_success:
        result.steps_completed.append(f"delete_service_port_{index}")
        logger.info("Deleted service-port index %d", index)
        return True

    result.steps_failed.append(f"delete_service_port_{index}")
    result.errors.append(f"Failed to delete service-port {index}: {cmd_result.message}")
    logger.error("Failed to delete service-port %d: %s", index, cmd_result.message)
    return False


def _execute_management_ip_config(
    session: OltSession,
    interface_path: str,
    olt_ont_id: int,
    mgmt_config: DesiredManagementConfig,
    result: ProvisioningExecutionResult,
) -> bool:
    """Execute management IP (IPHOST) configuration."""
    # Build IPHOST command
    # Format: ont iphost {olt_ont_id} ip-index 0 ip-address dhcp vlan {vlan_tag}
    if mgmt_config.ip_mode == "dhcp":
        cmd = f"ont iphost {olt_ont_id} ip-index 0 ip-address dhcp vlan {mgmt_config.vlan_tag}"
    else:
        missing = [
            name
            for name, value in {
                "ip_address": mgmt_config.ip_address,
                "subnet": mgmt_config.subnet,
                "gateway": mgmt_config.gateway,
            }.items()
            if not value
        ]
        if missing:
            message = "Static management IP config is incomplete: " + ", ".join(missing)
            result.steps_failed.append("configure_management_ip")
            result.errors.append(message)
            logger.error(message)
            return False
        cmd = f"ont iphost {olt_ont_id} ip-index 0 ip-address {mgmt_config.ip_address} mask {mgmt_config.subnet} gateway {mgmt_config.gateway} vlan {mgmt_config.vlan_tag}"

    # Enter interface mode
    session.run_command(f"interface gpon {interface_path}", require_mode=CliMode.CONFIG)

    cmd_result = session.run_command(cmd)

    # Exit interface mode
    session.run_command("quit")

    if cmd_result.success or cmd_result.is_idempotent_success:
        result.steps_completed.append("configure_management_ip")
        result.compensation_log.append(
            CompensationEntry(
                step_name="configure_management_ip",
                undo_commands=[f"undo ont iphost {olt_ont_id} ip-index 0"],
                description="Remove management IP configuration",
                interface_path=interface_path,
            )
        )
        logger.info("Configured management IP for ONT %d", olt_ont_id)
        return True

    result.steps_failed.append("configure_management_ip")
    result.errors.append(f"Failed to configure management IP: {cmd_result.message}")
    return False


def _execute_internet_config(
    session: OltSession,
    interface_path: str,
    olt_ont_id: int,
    ip_index: int,
    result: ProvisioningExecutionResult,
) -> bool:
    """Execute internet-config (TCP stack activation)."""
    # Enter interface mode
    session.run_command(f"interface gpon {interface_path}", require_mode=CliMode.CONFIG)

    cmd = f"ont internet-config {olt_ont_id} ip-index {ip_index}"
    cmd_result = session.run_command(cmd)

    session.run_command("quit")

    if cmd_result.success or cmd_result.is_idempotent_success:
        result.steps_completed.append("activate_internet_config")
        result.compensation_log.append(
            CompensationEntry(
                step_name="activate_internet_config",
                undo_commands=[
                    f"undo ont internet-config {olt_ont_id} ip-index {ip_index}"
                ],
                description="Deactivate internet-config",
                interface_path=interface_path,
            )
        )
        logger.info(
            "Activated internet-config for ONT %d ip-index %d", olt_ont_id, ip_index
        )
        return True

    result.steps_failed.append("activate_internet_config")
    result.errors.append(f"Failed to activate internet-config: {cmd_result.message}")
    return False


def _execute_wan_config(
    session: OltSession,
    interface_path: str,
    olt_ont_id: int,
    ip_index: int,
    profile_id: int,
    result: ProvisioningExecutionResult,
) -> bool:
    """Execute wan-config (route+NAT mode)."""
    session.run_command(f"interface gpon {interface_path}", require_mode=CliMode.CONFIG)

    cmd = f"ont wan-config {olt_ont_id} ip-index {ip_index} profile-id {profile_id}"
    cmd_result = session.run_command(cmd)

    session.run_command("quit")

    if cmd_result.success or cmd_result.is_idempotent_success:
        result.steps_completed.append("configure_wan")
        result.compensation_log.append(
            CompensationEntry(
                step_name="configure_wan",
                undo_commands=[f"undo ont wan-config {olt_ont_id} ip-index {ip_index}"],
                description="Remove WAN configuration",
                interface_path=interface_path,
            )
        )
        logger.info(
            "Configured WAN mode for ONT %d ip-index %d profile %d",
            olt_ont_id,
            ip_index,
            profile_id,
        )
        return True

    result.steps_failed.append("configure_wan")
    result.errors.append(f"Failed to configure WAN: {cmd_result.message}")
    return False


def _execute_tr069_bind(
    session: OltSession,
    interface_path: str,
    olt_ont_id: int,
    profile_id: int,
    result: ProvisioningExecutionResult,
) -> bool:
    """Execute TR-069 server profile binding."""
    session.run_command(f"interface gpon {interface_path}", require_mode=CliMode.CONFIG)

    cmd = f"ont tr069-server-config {olt_ont_id} profile-id {profile_id}"
    cmd_result = session.run_command(cmd)

    session.run_command("quit")

    if cmd_result.success or cmd_result.is_idempotent_success:
        result.steps_completed.append("bind_tr069")
        result.compensation_log.append(
            CompensationEntry(
                step_name="bind_tr069",
                undo_commands=[f"undo ont tr069-server-config {olt_ont_id}"],
                description="Unbind TR-069 server profile",
                interface_path=interface_path,
            )
        )
        logger.info("Bound TR-069 profile %d to ONT %d", profile_id, olt_ont_id)
        return True

    result.steps_failed.append("bind_tr069")
    result.errors.append(f"Failed to bind TR-069 profile: {cmd_result.message}")
    return False


# ---------------------------------------------------------------------------
# Compensation Failure Persistence
# ---------------------------------------------------------------------------


def _persist_compensation_failures(
    db: Session,
    olt: OLTDevice,
    ont_unit_id: str | None,
    failed_entries: list[tuple[CompensationEntry, str]],
    operation_type: str = "provisioning",
) -> None:
    """Persist failed compensation entries to database for manual resolution.

    Args:
        db: Database session.
        olt: The OLT device.
        ont_unit_id: Optional ONT unit ID.
        failed_entries: List of (CompensationEntry, error_message) tuples.
        operation_type: Type of operation (provisioning, deprovision, reconciliation).
    """
    from app.models.compensation_failure import CompensationFailure, CompensationStatus

    for entry, error_message in failed_entries:
        failure = CompensationFailure(
            ont_unit_id=ont_unit_id,
            olt_device_id=str(olt.id),
            operation_type=operation_type,
            step_name=entry.step_name,
            undo_commands=entry.undo_commands,
            description=entry.description,
            resource_id=entry.resource_id,
            interface_path=entry.interface_path,
            error_message=error_message,
            status=CompensationStatus.pending,
        )
        db.add(failure)

    try:
        db.flush()
        logger.info(
            "Persisted %d compensation failure(s) for ONT %s on OLT %s",
            len(failed_entries),
            ont_unit_id,
            olt.name,
        )
    except Exception as exc:
        logger.error(
            "Failed to persist compensation failures: %s",
            exc,
            extra={"event": "compensation_failure_persistence_error"},
        )


def _emit_compensation_failure_alert(
    db: Session,
    olt: OLTDevice,
    ont_unit_id: str | None,
    failed_entries: list[tuple[CompensationEntry, str]],
) -> None:
    """Emit event for compensation failures to alert operators.

    Args:
        db: Database session.
        olt: The OLT device.
        ont_unit_id: Optional ONT unit ID.
        failed_entries: List of (CompensationEntry, error_message) tuples.
    """
    from app.services.events import emit_event
    from app.services.events.types import EventType

    step_names = [entry.step_name for entry, _ in failed_entries]
    emit_event(
        db,
        EventType.network_alert,
        {
            "alert_type": "compensation_failure",
            "olt_id": str(olt.id),
            "olt_name": olt.name,
            "ont_unit_id": ont_unit_id,
            "failed_steps": step_names,
            "failure_count": len(failed_entries),
            "message": f"Provisioning rollback failed for {len(failed_entries)} step(s): {', '.join(step_names)}",
        },
        actor="system",
    )
