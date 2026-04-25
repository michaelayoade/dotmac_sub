"""ONT provisioning helpers with state reconciliation support.

This package provides modular ONT provisioning functionality:

- context: OLT context resolution (ONT -> OLT + FSP + ONT-ID)
- preflight: Pre-provisioning validation checks
- credentials: PPPoE credential masking
- result: StepResult dataclass for operation outcomes
- orchestrator: Direct ONT provisioning from OLT defaults + desired config

State Reconciliation (new):
- state: Desired/actual state dataclasses and building functions
- reconciler: Delta computation and validation
- executor: Batched execution with best-effort compensation rollback
- optical_budget: Optical power validation
- vlan_validator: VLAN trunk verification

Usage:
    from app.services.network.ont_provisioning.reconciler import reconcile_ont_state
    from app.services.network.ont_provisioning.executor import execute_delta
"""

# Re-export commonly used items for convenience
from app.services.network.ont_provisioning.context import (
    OltContext,
    resolve_olt_context,
)
from app.services.network.ont_provisioning.credentials import mask_credentials
from app.services.network.ont_provisioning.executor import (
    CompensationEntry,
    ProvisioningExecutionResult,
    execute_delta,
)
from app.services.network.ont_provisioning.optical_budget import (
    OpticalBudgetResult,
    check_optical_budget_for_provisioning,
    validate_optical_budget,
)
from app.services.network.ont_provisioning.preflight import validate_prerequisites
from app.services.network.ont_provisioning.reconciler import (
    compute_delta,
    get_delta_summary,
    reconcile_ont_state,
    validate_delta,
)
from app.services.network.ont_provisioning.result import StepResult
from app.services.network.ont_provisioning.orchestrator import (
    OntProvisioningResult,
    provision_ont_from_desired_config,
)

# State reconciliation exports
from app.services.network.ont_provisioning.state import (
    ActualOntState,
    ActualServicePort,
    DesiredOntState,
    DesiredServicePort,
    ProvisioningAction,
    ProvisioningDelta,
    ServicePortDelta,
    build_desired_state_from_config,
    read_actual_state,
)
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
    # State
    "DesiredOntState",
    "DesiredServicePort",
    "ActualOntState",
    "ActualServicePort",
    "ProvisioningAction",
    "ProvisioningDelta",
    "ServicePortDelta",
    "build_desired_state_from_config",
    "read_actual_state",
    # Reconciler
    "compute_delta",
    "validate_delta",
    "reconcile_ont_state",
    "get_delta_summary",
    # Executor
    "CompensationEntry",
    "ProvisioningExecutionResult",
    "execute_delta",
    # Optical budget
    "OpticalBudgetResult",
    "validate_optical_budget",
    "check_optical_budget_for_provisioning",
    # VLAN validator
    "VlanValidationResult",
    "validate_vlan_exists",
    "validate_management_vlan_trunked",
    "validate_service_port_vlans",
    # Direct orchestration
    "OntProvisioningResult",
    "provision_ont_from_desired_config",
]
