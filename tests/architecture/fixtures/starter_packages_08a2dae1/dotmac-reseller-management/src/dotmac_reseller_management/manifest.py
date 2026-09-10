"""Installable declaration for the tenant reseller owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    OUTBOX_RELAY_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_reseller_management.models import TABLES

module = ModuleManifest(
    code="reseller_management",
    version="0.1.0a1",
    core=False,
    short_code="reseller",
    migration_prefix="rm",
    migration_branch="reseller_management",
    tables=TABLES,
    platform_tables=(),
    requires=(
        TENANT_SCOPE_CATALOG_V1.name,
        MODULE_DATABASE_ROLES_V1.name,
        OUTBOX_RELAY_V1.name,
    ),
)

__all__ = ["module"]
