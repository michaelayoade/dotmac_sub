"""Manifest for response obligations.

The `outbox_event_types` are the point of the module's outward edge. A breach
that only writes a row is a report nobody reads at the moment it matters; these
two events commit with the observation, and Messaging/Integrator delivers.

No `permissions` are declared. Starting, pausing and completing a clock are
consequences of decisions other owners already authorized — a ticket was
answered, a conversation was assigned, an agent went offline. Adding a
permission here would put a second authorization check in front of a decision
that has already been made, which is how a legitimate state change ends up
silently not recorded.
"""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_response_obligations.models import TENANT_TABLES

module = ModuleManifest(
    code="response_obligations",
    version="0.1.0a1",
    core=False,
    short_code="sla",
    migration_prefix="ro",
    migration_branch="response_obligations",
    tables=TENANT_TABLES,
    platform_tables=(),
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
    outbox_event_types=(
        "response_obligations.obligation_at_risk.v1",
        "response_obligations.obligation_breached.v1",
    ),
)

__all__ = ["module"]
