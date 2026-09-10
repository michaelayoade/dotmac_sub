"""Manifest for normalized usage facts."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_usage.models import TENANT_TABLES

module = ModuleManifest(
    code="usage",
    version="0.1.0a1",
    core=False,
    short_code="usage",
    migration_prefix="us",
    migration_branch="usage",
    tables=TENANT_TABLES,
    platform_tables=(),
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
