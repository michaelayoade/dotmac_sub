"""Installable declaration for the tenant payables owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_payables.models import TABLES

module = ModuleManifest(
    code="payables",
    version="0.1.0a1",
    core=False,
    short_code="payables",
    migration_prefix="pa",
    migration_branch="payables",
    tables=TABLES,
    requires=(
        TENANT_SCOPE_CATALOG_V1.name,
        MODULE_DATABASE_ROLES_V1.name,
        IDEMPOTENCY_LEDGER_V1.name,
    ),
)

__all__ = ["module"]
