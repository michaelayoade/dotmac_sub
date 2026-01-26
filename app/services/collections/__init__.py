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
    # Classes
    DunningCases,
    DunningActionLogs,
    DunningWorkflow,
    PrepaidEnforcement,
    # Service instances
    dunning_cases,
    dunning_action_logs,
    dunning_workflow,
    prepaid_enforcement,
    # Public functions
    restore_account_services,
)

__all__ = [
    # Classes
    "DunningCases",
    "DunningActionLogs",
    "DunningWorkflow",
    "PrepaidEnforcement",
    # Service instances
    "dunning_cases",
    "dunning_action_logs",
    "dunning_workflow",
    "prepaid_enforcement",
    # Public functions
    "restore_account_services",
]
