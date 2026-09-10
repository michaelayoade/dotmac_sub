"""Installable declaration for tenant Remote Access."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_remote_access.models import TENANT_TABLES

module = ModuleManifest(
    code="remote_access",
    version="0.1.0a1",
    core=False,
    short_code="remoteaccess",
    migration_prefix="ra",
    migration_branch="remote_access",
    tables=TENANT_TABLES,
    platform_tables=(),
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
