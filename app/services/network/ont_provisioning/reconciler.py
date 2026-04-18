"""ONT provisioning state reconciliation.

This module computes the delta between desired and actual ONT state,
then validates the delta before execution. The reconciliation approach
provides idempotency: existing matching resources result in NOOP, not error.

Key functions:
- compute_delta(): Compare desired vs actual, produce ProvisioningDelta
- validate_delta(): Run preflight validations on the delta
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.services.network.ont_provisioning.state import (
    ActualOntState,
    ActualServicePort,
    DesiredOntState,
    DesiredServicePort,
    ProvisioningAction,
    ProvisioningDelta,
    ServicePortDelta,
    ServicePortMatchResult,
)

if TYPE_CHECKING:
    from app.models.network import OLTDevice, OntUnit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Delta Computation
# ---------------------------------------------------------------------------


def compute_delta(
    desired: DesiredOntState,
    actual: ActualOntState,
) -> ProvisioningDelta:
    """Compare desired vs actual state and compute required changes.

    Service ports are matched by (vlan_id, gem_index):
    - Exact match (same VLAN, GEM, tag_transform) = NOOP
    - Partial match (same VLAN, GEM, different tag_transform) = DELETE + CREATE
    - Not found in actual = CREATE
    - In actual but not in desired = DELETE (orphaned)

    This matching strategy provides idempotency: re-running provisioning
    on an already-provisioned ONT results in all NOOPs.

    Args:
        desired: The desired ONT state from profile.
        actual: The current ONT state from OLT.

    Returns:
        ProvisioningDelta describing all required changes.
    """
    delta = ProvisioningDelta()

    # Track which actual ports have been matched
    matched_actual_indices: set[int] = set()

    # Compare each desired service port against actual
    for desired_port in desired.service_ports:
        match_result, matched_actual = _find_matching_actual_port(
            desired_port, actual.service_ports
        )

        if match_result == ServicePortMatchResult.EXACT_MATCH:
            # Port already exists with correct configuration
            delta.service_port_deltas.append(
                ServicePortDelta(
                    action=ProvisioningAction.NOOP,
                    desired=desired_port,
                    actual=matched_actual,
                    message=f"Service-port VLAN {desired_port.vlan_id} GEM {desired_port.gem_index} already exists",
                )
            )
            if matched_actual:
                matched_actual_indices.add(matched_actual.index)

        elif match_result == ServicePortMatchResult.PARTIAL_MATCH:
            # Port exists but with different tag_transform - needs recreation
            # Note: This is a rare case; typically tag_transform matches
            # Track the DELETE delta index so CREATE can verify DELETE succeeded
            delete_delta_index = len(delta.service_port_deltas)
            delta.service_port_deltas.append(
                ServicePortDelta(
                    action=ProvisioningAction.DELETE,
                    desired=None,
                    actual=matched_actual,
                    message=f"Service-port index {matched_actual.index if matched_actual else 'unknown'} "
                    f"has wrong tag_transform, will recreate",
                )
            )
            delta.service_port_deltas.append(
                ServicePortDelta(
                    action=ProvisioningAction.CREATE,
                    desired=desired_port,
                    actual=None,
                    message=f"Create service-port VLAN {desired_port.vlan_id} GEM {desired_port.gem_index}",
                    depends_on_delete_index=delete_delta_index,
                )
            )
            if matched_actual:
                matched_actual_indices.add(matched_actual.index)

        else:
            # Port doesn't exist - needs creation
            delta.service_port_deltas.append(
                ServicePortDelta(
                    action=ProvisioningAction.CREATE,
                    desired=desired_port,
                    actual=None,
                    message=f"Create service-port VLAN {desired_port.vlan_id} GEM {desired_port.gem_index}",
                )
            )

    # Check for orphaned actual ports (exist on OLT but not in desired)
    # Note: We don't automatically delete orphaned ports - that's a policy decision
    # Instead, we mark them for potential cleanup
    for actual_port in actual.service_ports:
        if actual_port.index not in matched_actual_indices:
            # This port exists on OLT but isn't in desired state
            # We log it but don't delete by default (conservative approach)
            logger.debug(
                "Orphaned service-port detected: index=%d vlan=%d gem=%d ont=%d",
                actual_port.index,
                actual_port.vlan_id,
                actual_port.gem_index,
                actual_port.ont_id,
            )

    # Determine if management IP configuration is needed
    if desired.management:
        # Check if management is already configured
        # For now, we always include it if desired; future enhancement could check actual
        delta.needs_mgmt_ip_config = True

    # Determine if TR-069 binding is needed
    if desired.tr069:
        # Check if TR-069 is already bound
        if actual.tr069_profile_id is None:
            delta.needs_tr069_bind = True
        # Note: We could also check if profile IDs match

    # Determine if internet-config is needed
    if desired.internet_config_ip_index is not None:
        delta.needs_internet_config = True

    # Determine if wan-config is needed
    if desired.wan_config_profile_id is not None:
        delta.needs_wan_config = True

    return delta


def _find_matching_actual_port(
    desired: DesiredServicePort,
    actual_ports: tuple[ActualServicePort, ...],
) -> tuple[ServicePortMatchResult, ActualServicePort | None]:
    """Find an actual port matching the desired port.

    Matches by (vlan_id, gem_index), then checks tag_transform.

    Returns:
        Tuple of (match_result, matched_actual_port or None).
    """
    for actual in actual_ports:
        result = desired.matches(actual)
        if result != ServicePortMatchResult.NOT_FOUND:
            return result, actual
    return ServicePortMatchResult.NOT_FOUND, None


# ---------------------------------------------------------------------------
# Delta Validation
# ---------------------------------------------------------------------------


def validate_delta(
    db: Session,
    delta: ProvisioningDelta,
    olt: OLTDevice,
    ont: OntUnit,
    desired: DesiredOntState,
) -> ProvisioningDelta:
    """Run validations on the provisioning delta.

    Validates:
    1. Optical budget (signal strength within acceptable range)
    2. Management VLAN is trunked (if management IP is being configured)
    3. Service port VLANs exist in the database
    4. internet_config_ip_index is within ONU type limits

    Args:
        db: Database session.
        delta: The computed delta to validate.
        olt: The OLT device.
        ont: The ONT being provisioned.
        desired: The desired state.

    Returns:
        The same delta with validation results populated.
    """
    from app.services.network.ont_provisioning.optical_budget import (
        validate_optical_budget,
    )
    from app.services.network.ont_provisioning.vlan_validator import (
        validate_management_vlan_trunked,
        validate_service_port_vlans,
    )

    # 1. Optical budget validation
    optical_result = validate_optical_budget(ont)
    delta.optical_budget_ok = optical_result.is_valid
    delta.optical_budget_message = optical_result.message

    if not optical_result.is_valid:
        logger.warning(
            "Optical budget validation failed for ONT %s: %s",
            ont.serial_number,
            optical_result.message,
        )

    # 2. Management VLAN validation (if configuring management IP)
    if delta.needs_mgmt_ip_config and desired.management:
        mgmt_result = validate_management_vlan_trunked(
            db, desired.management.vlan_tag, olt
        )
        delta.mgmt_vlan_trunked = mgmt_result.is_valid
        delta.mgmt_vlan_message = mgmt_result.message

        if not mgmt_result.is_valid:
            logger.warning(
                "Management VLAN validation failed for ONT %s: %s",
                ont.serial_number,
                mgmt_result.message,
            )

    # 3. Service port VLAN validation
    create_deltas = [
        d for d in delta.service_port_deltas if d.action == ProvisioningAction.CREATE
    ]
    if create_deltas:
        vlan_ids = [d.desired.vlan_id for d in create_deltas if d.desired]
        if vlan_ids:
            vlan_result = validate_service_port_vlans(db, vlan_ids, olt)
            if not vlan_result.is_valid:
                delta.service_vlans_ok = False
                delta.service_vlans_message = vlan_result.message
                logger.warning(
                    "Service port VLAN validation failed for ONT %s: %s",
                    ont.serial_number,
                    vlan_result.message,
                )
                # Mark invalid deltas
                for d in create_deltas:
                    if d.desired and d.desired.vlan_id in vlan_result.invalid_vlans:
                        d.message = f"VLAN {d.desired.vlan_id} not configured on OLT"
            else:
                delta.service_vlans_ok = True
                delta.service_vlans_message = vlan_result.message

    # 4. internet_config_ip_index bounds validation
    if desired.internet_config_ip_index is not None:
        ip_index_result = _validate_ip_index(ont, desired.internet_config_ip_index)
        delta.ip_index_valid = ip_index_result[0]
        delta.ip_index_message = ip_index_result[1]

        if not ip_index_result[0]:
            logger.warning(
                "IP index validation failed for ONT %s: %s",
                ont.serial_number,
                ip_index_result[1],
            )

    return delta


def _validate_ip_index(ont: OntUnit, ip_index: int) -> tuple[bool, str]:
    """Validate internet_config_ip_index is within ONU type limits.

    Different ONT models support different numbers of IP interfaces.
    This validation checks that the requested index is within bounds.

    Args:
        ont: The ONT to validate against.
        ip_index: The requested IP index.

    Returns:
        Tuple of (is_valid, message).
    """
    # Default limits for common ONT types
    # These could be moved to a config table for extensibility
    DEFAULT_MAX_IP_INDEX = 8  # Most ONTs support at least 8 IP interfaces
    MIN_IP_INDEX = 0

    # Get ONT model for specific limits
    model = getattr(ont, "model", None) or getattr(ont, "onu_model", None) or ""
    model_lower = model.lower()

    # Model-specific limits (expandable)
    max_index = DEFAULT_MAX_IP_INDEX
    if "eg8145" in model_lower:
        max_index = 4  # HG8145 typically has 4 IP interfaces
    elif "hg8546" in model_lower:
        max_index = 8
    elif "hg8245" in model_lower:
        max_index = 8

    if ip_index < MIN_IP_INDEX:
        return False, f"IP index {ip_index} is below minimum ({MIN_IP_INDEX})"

    if ip_index > max_index:
        return (
            False,
            f"IP index {ip_index} exceeds maximum ({max_index}) for ONT model {model or 'unknown'}",
        )

    return True, f"IP index {ip_index} is valid (max: {max_index})"


# ---------------------------------------------------------------------------
# Convenience Functions
# ---------------------------------------------------------------------------


def reconcile_ont_state(
    db: Session,
    ont_id: str,
    profile_id: str | None = None,
    tr069_olt_profile_id: int | None = None,
) -> tuple[ProvisioningDelta | None, str]:
    """Full reconciliation: build desired, read actual, compute and validate delta.

    This is the main entry point for the reconciliation process.

    Args:
        db: Database session.
        ont_id: The ONT ID to reconcile.
        profile_id: Optional explicit profile ID.
        tr069_olt_profile_id: Optional explicit OLT-local TR-069 profile ID.

    Returns:
        Tuple of (ProvisioningDelta or None, error message).
    """
    from app.models.network import OntProvisioningProfile
    from app.services.network.ont_provisioning.context import resolve_olt_context
    from app.services.network.ont_provisioning.profiles import resolve_profile
    from app.services.network.ont_provisioning.state import (
        build_desired_state_from_profile,
        read_actual_state,
    )

    # Resolve context
    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return None, err

    # Resolve profile
    profile = None
    if profile_id:
        profile = db.get(OntProvisioningProfile, profile_id)
    else:
        profile = resolve_profile(db, ctx.ont)

    # Build desired state
    desired, err = build_desired_state_from_profile(
        db,
        ont_id,
        profile,
        tr069_olt_profile_id=tr069_olt_profile_id,
    )
    if not desired:
        return None, err

    # Read actual state
    actual, err = read_actual_state(ctx.olt, ctx.fsp, ctx.olt_ont_id)
    if not actual:
        return None, err

    # Compute delta
    delta = compute_delta(desired, actual)

    # Validate delta
    delta = validate_delta(db, delta, ctx.olt, ctx.ont, desired)

    return delta, ""


def get_delta_summary(delta: ProvisioningDelta) -> dict:
    """Get a summary of the provisioning delta for display.

    Args:
        delta: The computed delta.

    Returns:
        Dictionary with summary information.
    """
    create_count = sum(
        1 for d in delta.service_port_deltas if d.action == ProvisioningAction.CREATE
    )
    delete_count = sum(
        1 for d in delta.service_port_deltas if d.action == ProvisioningAction.DELETE
    )
    noop_count = sum(
        1 for d in delta.service_port_deltas if d.action == ProvisioningAction.NOOP
    )

    return {
        "has_changes": delta.has_changes,
        "is_valid": delta.is_valid,
        "service_ports": {
            "create": create_count,
            "delete": delete_count,
            "noop": noop_count,
        },
        "needs_mgmt_ip_config": delta.needs_mgmt_ip_config,
        "needs_tr069_bind": delta.needs_tr069_bind,
        "needs_internet_config": delta.needs_internet_config,
        "needs_wan_config": delta.needs_wan_config,
        "validations": {
            "optical_budget_ok": delta.optical_budget_ok,
            "optical_budget_message": delta.optical_budget_message,
            "mgmt_vlan_trunked": delta.mgmt_vlan_trunked,
            "mgmt_vlan_message": delta.mgmt_vlan_message,
            "service_vlans_ok": delta.service_vlans_ok,
            "service_vlans_message": delta.service_vlans_message,
            "ip_index_valid": delta.ip_index_valid,
            "ip_index_message": delta.ip_index_message,
        },
    }
