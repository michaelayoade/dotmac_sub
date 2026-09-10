"""Tenant-only module declaration for managed records."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_records.models import TABLES

module = ModuleManifest(
    code="records",
    version="0.1.0a1",
    core=False,
    short_code="records",
    migration_prefix="re",
    migration_branch="records",
    tables=TABLES,
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
