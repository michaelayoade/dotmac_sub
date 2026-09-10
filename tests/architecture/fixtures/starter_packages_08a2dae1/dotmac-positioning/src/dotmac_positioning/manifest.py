"""Installable tenant-plane declaration for dotmac-positioning."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_positioning.models import TENANT_TABLES

module = ModuleManifest(
    code="positioning",
    version="0.1.0a1",
    core=False,
    short_code="pos",
    migration_prefix="po",
    migration_branch="positioning",
    tables=TENANT_TABLES,
    platform_tables=(),
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
