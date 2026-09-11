"""Installable declaration for the tenant procurement decision owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_procurement.models import TABLES

module = ModuleManifest(
    code="procurement",
    version="0.1.0a1",
    core=False,
    short_code="procurement",
    migration_prefix="pc",
    migration_branch="procurement",
    tables=TABLES,
    platform_tables=(),
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
