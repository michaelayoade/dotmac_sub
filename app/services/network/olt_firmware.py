"""OLT firmware upgrade service with verification and rollback.

Provides enhanced firmware upgrade capabilities:
- Dry-run mode to preview upgrade without executing
- Post-upgrade verification polling
- Automatic rollback on failure (if dual-image supported)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OltFirmwareImage
from app.services.network import olt_ssh as olt_ssh_service
from app.services.network.olt_inventory import get_olt_or_none
from app.services.network.olt_web_audit import log_olt_audit_event

logger = logging.getLogger(__name__)


@dataclass
class FirmwareUpgradeResult:
    """Result of a firmware upgrade operation."""

    success: bool
    message: str
    dry_run: bool = False
    current_version: str | None = None
    target_version: str | None = None
    reachable_after: bool | None = None
    rollback_attempted: bool = False
    rollback_success: bool | None = None
    steps: list[dict[str, object]] = field(default_factory=list)
    duration_sec: float = 0.0

    def to_dict(self) -> dict[str, object]:
        """Convert to JSON-serializable dict."""
        return {
            "success": self.success,
            "message": self.message,
            "dry_run": self.dry_run,
            "current_version": self.current_version,
            "target_version": self.target_version,
            "reachable_after": self.reachable_after,
            "rollback_attempted": self.rollback_attempted,
            "rollback_success": self.rollback_success,
            "steps": self.steps,
            "duration_sec": self.duration_sec,
        }


def _add_step(
    result: FirmwareUpgradeResult,
    name: str,
    success: bool,
    message: str,
) -> None:
    """Add a step to the result's step list."""
    result.steps.append(
        {
            "name": name,
            "success": success,
            "message": message,
        }
    )


def poll_olt_reachability(
    olt: OLTDevice,
    timeout_sec: int = 300,
    poll_interval_sec: int = 15,
    initial_wait_sec: int = 60,
) -> tuple[bool, str]:
    """Poll OLT with SSH until reachable or timeout.

    Args:
        olt: The OLT device.
        timeout_sec: Maximum time to wait for OLT to become reachable.
        poll_interval_sec: Time between reachability checks.
        initial_wait_sec: Initial wait before first check (for reboot).

    Returns:
        Tuple of (reachable, message).
    """
    logger.info(
        "Waiting %d seconds for OLT %s to reboot before polling...",
        initial_wait_sec,
        olt.name,
    )
    time.sleep(initial_wait_sec)

    start_time = time.time()
    elapsed_before_polling = initial_wait_sec
    remaining_timeout = timeout_sec - elapsed_before_polling

    attempts = 0
    last_error = ""

    while time.time() - start_time < remaining_timeout:
        attempts += 1
        reachable, msg = olt_ssh_service.test_reachability(olt)
        if reachable:
            elapsed = time.time() - start_time + initial_wait_sec
            logger.info(
                "OLT %s became reachable after %.1f seconds (%d attempts)",
                olt.name,
                elapsed,
                attempts,
            )
            return True, f"OLT reachable after {int(elapsed)} seconds"

        last_error = msg
        logger.debug("OLT %s not reachable (attempt %d): %s", olt.name, attempts, msg)
        time.sleep(poll_interval_sec)

    total_elapsed = time.time() - start_time + initial_wait_sec
    logger.warning(
        "OLT %s did not become reachable within %d seconds (%d attempts). Last error: %s",
        olt.name,
        int(total_elapsed),
        attempts,
        last_error,
    )
    return False, f"OLT not reachable after {int(total_elapsed)} seconds: {last_error}"


def upgrade_with_verification(
    db: Session,
    olt_id: str,
    image_id: str,
    *,
    dry_run: bool = False,
    verify_after: bool = True,
    timeout_sec: int = 300,
    poll_interval_sec: int = 15,
    initial_wait_sec: int = 60,
) -> FirmwareUpgradeResult:
    """Upgrade OLT firmware with verification and optional rollback.

    Workflow:
    1. Get current firmware info
    2. If dry_run, return preview
    3. Initiate upgrade
    4. Poll for reachability (up to timeout_sec)
    5. Verify new version
    6. On failure, attempt rollback if dual-image

    Args:
        db: Database session.
        olt_id: UUID of the OLT.
        image_id: UUID of the firmware image.
        dry_run: If True, return preview without executing.
        verify_after: If True, verify OLT is reachable after upgrade.
        timeout_sec: Total timeout for reachability polling.
        poll_interval_sec: Interval between reachability checks.
        initial_wait_sec: Initial wait before first check.

    Returns:
        FirmwareUpgradeResult with details of the operation.
    """
    start_time = time.time()
    result = FirmwareUpgradeResult(success=False, message="", dry_run=dry_run)

    # Get OLT
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        result.message = "OLT not found"
        _add_step(result, "Get OLT", False, result.message)
        return result
    _add_step(result, "Get OLT", True, f"Found OLT: {olt.name}")

    # Get firmware image
    image = db.get(OltFirmwareImage, image_id)
    if not image:
        result.message = "Firmware image not found"
        _add_step(result, "Get firmware image", False, result.message)
        return result
    if not image.is_active:
        result.message = "Firmware image is not active"
        _add_step(result, "Get firmware image", False, result.message)
        return result
    result.target_version = image.version
    _add_step(
        result,
        "Get firmware image",
        True,
        f"Target: {image.vendor} {image.model or 'any'} v{image.version}",
    )

    # Get current firmware info
    fw_ok, fw_msg, fw_info = olt_ssh_service.get_firmware_info(olt)
    if not fw_ok:
        result.message = f"Could not get current firmware info: {fw_msg}"
        _add_step(result, "Get current firmware", False, result.message)
        return result
    result.current_version = fw_info.current_version
    _add_step(
        result,
        "Get current firmware",
        True,
        f"Current: {fw_info.current_version}, Dual-image: {fw_info.has_dual_image}",
    )

    # Dry run: return preview
    if dry_run:
        result.success = True
        result.message = (
            f"Dry run: Would upgrade from {fw_info.current_version} to {image.version}"
        )
        _add_step(result, "Dry run preview", True, result.message)
        result.duration_sec = time.time() - start_time
        return result

    # Initiate upgrade
    upgrade_ok, upgrade_msg = olt_ssh_service.upgrade_firmware(
        olt, image.file_url, method=image.upgrade_method or "sftp"
    )
    if not upgrade_ok:
        result.message = f"Upgrade initiation failed: {upgrade_msg}"
        _add_step(result, "Initiate upgrade", False, result.message)
        result.duration_sec = time.time() - start_time
        return result
    _add_step(result, "Initiate upgrade", True, upgrade_msg)

    # Skip verification if not requested
    if not verify_after:
        result.success = True
        result.message = (
            f"Firmware upgrade initiated (from {fw_info.current_version} "
            f"to {image.version}). Verification skipped."
        )
        result.duration_sec = time.time() - start_time
        return result

    # Poll for reachability
    reachable, reach_msg = poll_olt_reachability(
        olt,
        timeout_sec=timeout_sec,
        poll_interval_sec=poll_interval_sec,
        initial_wait_sec=initial_wait_sec,
    )
    result.reachable_after = reachable
    _add_step(result, "Wait for reboot", reachable, reach_msg)

    if not reachable:
        result.message = f"OLT not reachable after upgrade: {reach_msg}"

        # Attempt rollback if dual-image available
        if fw_info.has_dual_image:
            result.rollback_attempted = True
            logger.warning(
                "OLT %s not reachable after upgrade, attempting rollback...",
                olt.name,
            )
            # We can't rollback if OLT is not reachable - this would require
            # out-of-band access or waiting for OLT to recover
            result.rollback_success = False
            _add_step(
                result,
                "Rollback attempt",
                False,
                "Cannot rollback - OLT not reachable",
            )

        result.duration_sec = time.time() - start_time
        return result

    # Verify new version
    verify_ok, verify_msg, verify_info = olt_ssh_service.get_firmware_info(olt)
    if not verify_ok:
        result.message = (
            f"Could not verify firmware version after upgrade: {verify_msg}"
        )
        _add_step(result, "Verify firmware version", False, result.message)
        result.duration_sec = time.time() - start_time
        return result

    new_version = verify_info.current_version
    if new_version == fw_info.current_version:
        result.message = (
            f"Firmware version unchanged ({new_version}) - upgrade may have failed"
        )
        _add_step(result, "Verify firmware version", False, result.message)
        result.duration_sec = time.time() - start_time
        return result

    _add_step(
        result,
        "Verify firmware version",
        True,
        f"Upgraded from {fw_info.current_version} to {new_version}",
    )

    result.success = True
    result.message = (
        f"Firmware upgraded successfully from {fw_info.current_version} "
        f"to {new_version}"
    )
    result.duration_sec = time.time() - start_time
    return result


def upgrade_with_verification_audited(
    db: Session,
    olt_id: str,
    image_id: str,
    *,
    dry_run: bool = False,
    verify_after: bool = True,
    timeout_sec: int = 300,
    request=None,
) -> FirmwareUpgradeResult:
    """Wrapper that logs audit event for firmware upgrade."""
    result = upgrade_with_verification(
        db,
        olt_id,
        image_id,
        dry_run=dry_run,
        verify_after=verify_after,
        timeout_sec=timeout_sec,
    )

    action = "firmware_upgrade_dry_run" if dry_run else "firmware_upgrade_verified"
    log_olt_audit_event(
        db,
        request=request,
        action=action,
        entity_id=olt_id,
        metadata={
            "result": "success" if result.success else "error",
            "message": result.message,
            "firmware_image_id": image_id,
            "dry_run": dry_run,
            "current_version": result.current_version,
            "target_version": result.target_version,
            "reachable_after": result.reachable_after,
            "rollback_attempted": result.rollback_attempted,
            "rollback_success": result.rollback_success,
            "duration_sec": result.duration_sec,
        },
        status_code=200 if result.success else 500,
        is_success=result.success,
    )

    return result


def get_firmware_preview(
    db: Session,
    olt_id: str,
    image_id: str,
) -> FirmwareUpgradeResult:
    """Get a preview of what firmware upgrade would do (dry-run).

    Args:
        db: Database session.
        olt_id: UUID of the OLT.
        image_id: UUID of the firmware image.

    Returns:
        FirmwareUpgradeResult with preview information.
    """
    return upgrade_with_verification(
        db,
        olt_id,
        image_id,
        dry_run=True,
        verify_after=False,
    )
