"""Installable manifest for the tenant-only IPAM owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_ipam.models import TENANT_TABLES

module = ModuleManifest(
    code="ipam",
    version="0.1.0a1",
    core=False,
    short_code="ipam",
    migration_prefix="ip",
    migration_branch="ipam",
    tables=TENANT_TABLES,
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
