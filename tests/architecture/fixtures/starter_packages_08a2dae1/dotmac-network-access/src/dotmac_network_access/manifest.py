"""Installable manifest for Network Access."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_network_access.models import TENANT_TABLES

module = ModuleManifest(
    code="network_access",
    version="0.1.0a1",
    core=False,
    short_code="netaccess",
    migration_prefix="nac",
    migration_branch="network_access",
    tables=TENANT_TABLES,
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)
__all__ = ["module"]
