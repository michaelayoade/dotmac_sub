"""Collections services package.

This package provides services for managing collections and dunning processes:
- Dunning cases and workflows
- Prepaid enforcement
- Account actions (suspend/restore)

The package supports both modular imports (from subpackages) and direct imports
for backwards compatibility.
"""

# Re-export from core module
from app.services.collections._core import (
    DunningActionLogs,
    BillingEnforcementReconciler,
    # Classes
    DunningCases,
    DunningWorkflow,
    PrepaidEnforcement,
    dunning_action_logs,
    billing_enforcement_reconciler,
    # Service instances
    dunning_cases,
    dunning_workflow,
    get_available_balance,
    has_overdue_balance,
    prepaid_enforcement,
    reconcile_retired_enforcement_locks,
    # Public functions
    restore_account_services,
)

__all__ = [
    # Classes
    "DunningCases",
    "DunningActionLogs",
    "BillingEnforcementReconciler",
    "DunningWorkflow",
    "PrepaidEnforcement",
    # Service instances
    "dunning_cases",
    "dunning_action_logs",
    "billing_enforcement_reconciler",
    "dunning_workflow",
    "prepaid_enforcement",
    # Public functions
    "get_available_balance",
    "has_overdue_balance",
    "reconcile_retired_enforcement_locks",
    "restore_account_services",
]
