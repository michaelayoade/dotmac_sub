"""Installable module declaration for the sales owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    OUTBOX_RELAY_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_sales.models import TENANT_TABLES

module = ModuleManifest(
    code="sales",
    version="0.1.0a1",
    core=False,
    short_code="sales",
    migration_prefix="sa",
    migration_branch="sales",
    tables=TENANT_TABLES,
    requires=(
        TENANT_SCOPE_CATALOG_V1.name,
        MODULE_DATABASE_ROLES_V1.name,
        IDEMPOTENCY_LEDGER_V1.name,
        OUTBOX_RELAY_V1.name,
    ),
)

__all__ = ["module"]
