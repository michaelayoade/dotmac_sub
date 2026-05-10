"""ONT provisioning helpers.

NOTE: The OLT-side authorization baseline is now part of authorize_ont().
After authorization, the ONT has service-port/VLAN/GEM plumbing and TR-069
reachability. Customer CPE service config is pushed separately through ACS.

This package provides supporting functionality:

- context: OLT context resolution (ONT -> OLT + FSP + ONT-ID)
- preflight: Pre-provisioning validation checks
- credentials: PPPoE credential masking
- result: StepResult dataclass for operation outcomes
- optical_budget: Optical power validation
- vlan_validator: VLAN trunk verification
"""

# Re-export commonly used items for convenience
from app.services.network.ont_provisioning.context import (
    OltContext,
    resolve_olt_context,
)
from app.services.network.ont_provisioning.credentials import mask_credentials
from app.services.network.ont_provisioning.optical_budget import (
    OpticalBudgetResult,
    check_optical_budget_for_provisioning,
    validate_optical_budget,
)
from app.services.network.ont_provisioning.preflight import validate_prerequisites
from app.services.network.ont_provisioning.result import StepResult
from app.services.network.ont_provisioning.vlan_validator import (
    VlanValidationResult,
    validate_management_vlan_trunked,
    validate_service_port_vlans,
    validate_vlan_exists,
)

__all__ = [
    # Context
    "OltContext",
    "resolve_olt_context",
    # Preflight
    "validate_prerequisites",
    # Credentials
    "mask_credentials",
    # Result
    "StepResult",
    # Optical budget
    "OpticalBudgetResult",
    "validate_optical_budget",
    "check_optical_budget_for_provisioning",
    # VLAN validator
    "VlanValidationResult",
    "validate_vlan_exists",
    "validate_management_vlan_trunked",
    "validate_service_port_vlans",
]
