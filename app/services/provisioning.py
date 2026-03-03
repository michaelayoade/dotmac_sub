"""Provisioning services compatibility module.

Re-exports helper functions, manager classes, and singleton service instances.
"""

from app.services.provisioning_helpers import (
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

__all__ = [
    "ensure_ip_assignments_for_subscription",
    "resolve_workflow_for_service_order",
    # Manager classes
    "InstallAppointments",
    "ProvisioningRuns",
    "ProvisioningSteps",
    "ProvisioningTasks",
    "ProvisioningWorkflows",
    "ServiceOrders",
    "ServiceStateTransitions",
    # Singleton instances
    "install_appointments",
    "provisioning_runs",
    "provisioning_steps",
    "provisioning_tasks",
    "provisioning_workflows",
    "service_orders",
    "service_state_transitions",
]
