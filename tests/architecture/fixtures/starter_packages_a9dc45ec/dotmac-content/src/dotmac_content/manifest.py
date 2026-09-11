"""Tenant-only module declaration for the editorial content owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_content.models import TENANT_TABLES

module = ModuleManifest(
    code="content",
    version="0.1.0a1",
    core=False,
    short_code="content",
    migration_prefix="ct",
    migration_branch="content",
    tables=TENANT_TABLES,
    platform_tables=(),
    requires=(
        TENANT_SCOPE_CATALOG_V1.name,
        MODULE_DATABASE_ROLES_V1.name,
    ),
)

__all__ = ["module"]
