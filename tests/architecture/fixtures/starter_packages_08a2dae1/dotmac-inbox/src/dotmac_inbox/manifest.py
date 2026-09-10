"""Installable manifest for the tenant-only conversation owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_inbox.models import TENANT_TABLES

module = ModuleManifest(
    code="inbox",
    version="0.1.0a2+dev",
    core=False,
    short_code="inbox",
    migration_prefix="ib",
    migration_branch="inbox",
    tables=TENANT_TABLES,
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
