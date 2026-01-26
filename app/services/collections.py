"""Collections services - backwards compatibility wrapper.

This module provides backwards compatibility for code that imports from
`app.services.collections`. The actual implementation is in the
`app.services.collections` package (collections/_core.py).

For new code, prefer importing from the package:
    from app.services.collections import dunning_cases, prepaid_enforcement
"""

# Re-export everything from the package for backwards compatibility
from app.services.collections._core import (
    DunningCases,
    DunningActionLogs,
    DunningWorkflow,
    PrepaidEnforcement,
    dunning_cases,
    dunning_action_logs,
    dunning_workflow,
    prepaid_enforcement,
    restore_account_services,
)

__all__ = [
    "DunningCases",
    "DunningActionLogs",
    "DunningWorkflow",
    "PrepaidEnforcement",
    "dunning_cases",
    "dunning_action_logs",
    "dunning_workflow",
    "prepaid_enforcement",
    "restore_account_services",
]
