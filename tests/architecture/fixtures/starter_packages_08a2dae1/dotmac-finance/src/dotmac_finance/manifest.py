"""Tenant-only module declaration for fixed-asset accounting."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_finance.models import TENANT_TABLES

module = ModuleManifest(
    code="finance",
    version="0.1.0a1",
    core=False,
    short_code="finance",
    migration_prefix="fn",
    migration_branch="finance",
    tables=TENANT_TABLES,
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
