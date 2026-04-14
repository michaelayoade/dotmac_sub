"""VLAN validation for ONT provisioning.

Validates that required VLANs are properly configured on the OLT before
provisioning service ports. This prevents silent failures where commands
succeed but traffic doesn't flow because the VLAN isn't trunked to upstream.

Key validations:
1. VLAN exists in the database for this OLT
2. Management VLAN is properly trunked (when applicable)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.models.network import OLTDevice

logger = logging.getLogger(__name__)


@dataclass
class VlanValidationResult:
    """Result of VLAN validation.

    Attributes:
        is_valid: True if all required VLANs are valid.
        vlan_id: The VLAN that was validated.
        message: Human-readable description of the result.
        exists_in_db: Whether the VLAN exists in the database.
        is_active: Whether the VLAN is marked active.
    """

    is_valid: bool
    vlan_id: int
    message: str = ""
    exists_in_db: bool = False
    is_active: bool = False


@dataclass
class VlanSetValidationResult:
    """Result of validating multiple VLANs.

    Attributes:
        is_valid: True if all VLANs are valid.
        results: Individual validation results per VLAN.
        message: Summary message.
    """

    is_valid: bool
    results: list[VlanValidationResult] = field(default_factory=list)
    message: str = ""

    @property
    def invalid_vlans(self) -> list[int]:
        """Return list of invalid VLAN IDs."""
        return [r.vlan_id for r in self.results if not r.is_valid]


def validate_vlan_exists(
    db: Session,
    vlan_id: int,
    olt: OLTDevice | None = None,
) -> VlanValidationResult:
    """Validate that a VLAN exists in the database.

    Checks for VLAN records that either:
    - Are global (no olt_device_id)
    - Are scoped to the specific OLT

    Args:
        db: Database session.
        vlan_id: The VLAN tag to validate.
        olt: Optional OLT to scope the check.

    Returns:
        VlanValidationResult with validation outcome.
    """
    from app.models.network import Vlan

    # Build query for VLAN existence
    stmt = select(Vlan).where(Vlan.tag == vlan_id)

    if olt:
        # Check for global VLAN or OLT-specific VLAN
        stmt = stmt.where(
            (Vlan.olt_device_id == olt.id) | (Vlan.olt_device_id.is_(None))
        )

    vlan = db.scalars(stmt).first()

    if not vlan:
        return VlanValidationResult(
            is_valid=False,
            vlan_id=vlan_id,
            message=f"VLAN {vlan_id} not found in database"
            + (f" for OLT {olt.name}" if olt else ""),
            exists_in_db=False,
            is_active=False,
        )

    if not getattr(vlan, "is_active", True):
        return VlanValidationResult(
            is_valid=False,
            vlan_id=vlan_id,
            message=f"VLAN {vlan_id} exists but is not active",
            exists_in_db=True,
            is_active=False,
        )

    return VlanValidationResult(
        is_valid=True,
        vlan_id=vlan_id,
        message=f"VLAN {vlan_id} is valid",
        exists_in_db=True,
        is_active=True,
    )


def validate_vlan_set(
    db: Session,
    vlan_ids: list[int],
    olt: OLTDevice | None = None,
) -> VlanSetValidationResult:
    """Validate multiple VLANs at once.

    Args:
        db: Database session.
        vlan_ids: List of VLAN tags to validate.
        olt: Optional OLT to scope the check.

    Returns:
        VlanSetValidationResult with per-VLAN results.
    """
    results = [validate_vlan_exists(db, vid, olt) for vid in vlan_ids]
    all_valid = all(r.is_valid for r in results)
    invalid = [r.vlan_id for r in results if not r.is_valid]

    if all_valid:
        message = f"All {len(vlan_ids)} VLAN(s) validated successfully"
    else:
        message = f"Invalid VLANs: {invalid}"

    return VlanSetValidationResult(
        is_valid=all_valid,
        results=results,
        message=message,
    )


def validate_management_vlan_trunked(
    db: Session,
    mgmt_vlan_tag: int,
    olt: OLTDevice,
) -> VlanValidationResult:
    """Validate that the management VLAN is properly configured for the OLT.

    Management VLANs require additional validation because:
    1. They must exist and be active
    2. They should be trunked to the upstream network
    3. They may need DHCP relay or static routing configured

    This function performs the basic existence check. Future enhancements
    could include OLT-level trunk verification via SSH.

    Args:
        db: Database session.
        mgmt_vlan_tag: The management VLAN tag.
        olt: The OLT device.

    Returns:
        VlanValidationResult with validation outcome.
    """
    result = validate_vlan_exists(db, mgmt_vlan_tag, olt)

    if not result.is_valid:
        return VlanValidationResult(
            is_valid=False,
            vlan_id=mgmt_vlan_tag,
            message=f"Management VLAN {mgmt_vlan_tag} not configured for OLT {olt.name}. "
            f"Create it at /admin/network/vlans.",
            exists_in_db=result.exists_in_db,
            is_active=result.is_active,
        )

    return VlanValidationResult(
        is_valid=True,
        vlan_id=mgmt_vlan_tag,
        message=f"Management VLAN {mgmt_vlan_tag} is configured for OLT {olt.name}",
        exists_in_db=True,
        is_active=True,
    )


def validate_service_port_vlans(
    db: Session,
    vlan_ids: list[int],
    olt: OLTDevice,
) -> VlanSetValidationResult:
    """Validate all VLANs needed for service ports.

    Args:
        db: Database session.
        vlan_ids: List of service-port VLAN IDs.
        olt: The OLT device.

    Returns:
        VlanSetValidationResult with validation outcome.
    """
    result = validate_vlan_set(db, vlan_ids, olt)

    if not result.is_valid:
        invalid = result.invalid_vlans
        result.message = (
            f"Service port VLANs not configured on OLT {olt.name}: {invalid}. "
            f"Create them at /admin/network/vlans."
        )

    return result


def get_missing_vlans_for_provisioning(
    db: Session,
    desired_vlans: list[int],
    olt: OLTDevice,
) -> list[int]:
    """Return list of VLANs that need to be created before provisioning.

    This is a convenience function for preflight checks.

    Args:
        db: Database session.
        desired_vlans: List of VLAN IDs needed for provisioning.
        olt: The OLT device.

    Returns:
        List of VLAN IDs that don't exist or aren't active.
    """
    result = validate_vlan_set(db, desired_vlans, olt)
    return result.invalid_vlans
