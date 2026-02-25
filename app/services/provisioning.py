"""Provisioning services compatibility module.

Re-exports helper functions, manager classes, and singleton service instances.
"""

from app.services.provisioning_helpers import (
    _ensure_ip_assignment_for_version,
    _ensure_ip_assignments,
    _extend_provisioning_context,
    _find_available_address,
    _get_address_by_id,
    _get_or_create_address_by_value,
    _parse_ip_value,
    _pool_prefix_length,
    _resolve_connector_context,
    _resolve_pool_for_version,
    ensure_ip_assignments_for_subscription,
    resolve_workflow_for_service_order,
)
from app.services.provisioning_managers import (
    InstallAppointments,
    ProvisioningRuns,
    ProvisioningSteps,
    ProvisioningTasks,
    ProvisioningWorkflows,
    ServiceOrders,
    ServiceStateTransitions,
    install_appointments,
    provisioning_runs,
    provisioning_steps,
    provisioning_tasks,
    provisioning_workflows,
    service_orders,
    service_state_transitions,
)

__all__ = [name for name in globals() if not name.startswith("__")]
