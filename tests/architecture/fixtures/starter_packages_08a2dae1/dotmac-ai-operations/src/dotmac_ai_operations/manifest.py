"""Installable declaration for tenant AI Operations."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_ai_operations.models import TENANT_TABLES

module = ModuleManifest(
    code="ai_operations",
    version="0.1.0a1",
    core=False,
    short_code="aiops",
    migration_prefix="ao",
    migration_branch="ai_operations",
    tables=TENANT_TABLES,
    platform_tables=(),
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)
__all__ = ["module"]
