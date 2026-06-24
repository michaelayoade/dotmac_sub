"""Collections services package.

This package provides services for managing collections and dunning processes:
- Dunning cases and workflows
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
    dunning_action_logs,
    billing_enforcement_reconciler,
    # Service instances
    dunning_cases,
    dunning_workflow,
    get_available_balance,
    has_overdue_balance,
    # Public functions
    restore_account_services,
)

__all__ = [
    # Classes
    "DunningCases",
    "DunningActionLogs",
    "BillingEnforcementReconciler",
    "DunningWorkflow",
    # Service instances
    "dunning_cases",
    "dunning_action_logs",
    "billing_enforcement_reconciler",
    "dunning_workflow",
    # Public functions
    "get_available_balance",
    "has_overdue_balance",
    "restore_account_services",
]
