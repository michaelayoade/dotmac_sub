"""Installable tenant-plane declaration for ``dotmac-analytics``."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_analytics.models import TENANT_TABLES

module = ModuleManifest(
    code="analytics",
    version="0.1.0a1",
    core=False,
    short_code="analytics",
    migration_prefix="ay",
    migration_branch="analytics",
    tables=TENANT_TABLES,
    requires=(
        TENANT_SCOPE_CATALOG_V1.name,
        MODULE_DATABASE_ROLES_V1.name,
        IDEMPOTENCY_LEDGER_V1.name,
    ),
)

__all__ = ["module"]
