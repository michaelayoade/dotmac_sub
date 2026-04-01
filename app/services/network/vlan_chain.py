"""VLAN chain validation — compares profile desired VLANs vs actual OLT service-ports.

Provides advisory warnings (not blocking) to help operators spot mismatches
between what the provisioning profile expects and what is actually configured.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.network import OntAssignment, OntProvisioningProfile, OntUnit

logger = logging.getLogger(__name__)


@dataclass
class VlanChainWarning:
    """A single advisory warning about a VLAN mismatch."""

    level: str  # "info", "warning", "error"
    message: str


@dataclass
class VlanChainResult:
    """Result of VLAN chain validation for an ONT."""

    ont_id: str
    profile_name: str | None = None
    desired_vlans: list[int] = field(default_factory=list)
    actual_vlans: list[int] = field(default_factory=list)
    warnings: list[VlanChainWarning] = field(default_factory=list)
    valid: bool = True


def validate_chain(
    db: Session,
    ont_id: str,
    *,
    actual_service_ports: list[dict] | None = None,
) -> VlanChainResult:
    """Compare profile desired VLANs vs actual OLT service-ports.

    Args:
        db: Database session.
        ont_id: OntUnit ID.
        actual_service_ports: Pre-fetched service-port data (list of dicts with
            'vlan_id' key). If None, validation is limited to profile-only checks.

    Returns:
        VlanChainResult with advisory warnings.
    """
    result = VlanChainResult(ont_id=ont_id)

    ont = db.get(OntUnit, ont_id)
    if not ont:
        result.warnings.append(VlanChainWarning("error", "ONT not found"))
        result.valid = False
        return result

    # Find active assignment to get subscriber/subscription context
    assignment: OntAssignment | None = None
    for a in getattr(ont, "assignments", []):
        if a.active:
            assignment = a
            break

    if not assignment:
        result.warnings.append(
            VlanChainWarning("info", "No active assignment — skipping profile check")
        )
        return result

    # Try to find provisioning profile directly from ONT
    profile: OntProvisioningProfile | None = None
    if ont.provisioning_profile_id:
        profile = db.get(OntProvisioningProfile, str(ont.provisioning_profile_id))

    if not profile:
        result.warnings.append(
            VlanChainWarning(
                "info", "No provisioning profile linked — VLAN check skipped"
            )
        )
        return result

    result.profile_name = profile.name

    # Collect desired VLANs from profile WAN services
    desired: set[int] = set()
    for ws in profile.wan_services:
        if ws.is_active and ws.s_vlan:
            desired.add(ws.s_vlan)
        if ws.is_active and ws.c_vlan:
            desired.add(ws.c_vlan)
    if profile.mgmt_vlan_tag:
        desired.add(profile.mgmt_vlan_tag)

    result.desired_vlans = sorted(desired)

    # Compare with actual service-ports if provided
    if actual_service_ports is not None:
        actual: set[int] = {
            sp["vlan_id"] for sp in actual_service_ports if sp.get("vlan_id")
        }
        result.actual_vlans = sorted(actual)

        missing = desired - actual
        extra = actual - desired

        if missing:
            for v in sorted(missing):
                result.warnings.append(
                    VlanChainWarning(
                        "warning",
                        f"VLAN {v} required by profile but no service-port found on OLT",
                    )
                )
                result.valid = False

        if extra:
            for v in sorted(extra):
                result.warnings.append(
                    VlanChainWarning(
                        "info",
                        f"VLAN {v} has service-port on OLT but is not in provisioning profile",
                    )
                )

        if not missing and not extra:
            result.warnings.append(
                VlanChainWarning("info", "All profile VLANs match OLT service-ports")
            )

    return result
