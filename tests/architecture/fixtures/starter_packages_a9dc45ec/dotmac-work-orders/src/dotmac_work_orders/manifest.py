"""Installable declaration for tenant-scoped physical work execution."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_work_orders.models import TENANT_TABLES

module = ModuleManifest(
    code="work_orders",
    version="0.1.0a1",
    core=False,
    short_code="workorders",
    migration_prefix="wo",
    migration_branch="work_orders",
    tables=TENANT_TABLES,
    requires=(
        TENANT_SCOPE_CATALOG_V1.name,
        MODULE_DATABASE_ROLES_V1.name,
        IDEMPOTENCY_LEDGER_V1.name,
    ),
)

__all__ = ["module"]
