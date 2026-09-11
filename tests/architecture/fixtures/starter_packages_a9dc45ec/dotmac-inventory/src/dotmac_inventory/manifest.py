"""Installable manifest for the tenant-only inventory owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_inventory.models import TENANT_TABLES

module = ModuleManifest(
    code="inventory",
    version="0.1.0a1",
    core=False,
    short_code="inventory",
    migration_prefix="iv",
    migration_branch="inventory",
    tables=TENANT_TABLES,
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
