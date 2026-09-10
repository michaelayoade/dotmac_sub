"""Tenant-only module declaration for tax."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_tax.models import TENANT_TABLES

module = ModuleManifest(
    code="tax",
    version="0.1.0a4",
    core=False,
    short_code="tax",
    migration_prefix="tx",
    migration_branch="tax",
    tables=TENANT_TABLES,
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
