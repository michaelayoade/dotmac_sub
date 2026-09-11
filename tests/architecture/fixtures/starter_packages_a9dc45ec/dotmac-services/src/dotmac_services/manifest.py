"""Manifest for the service-instance lifecycle owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_services.models import TENANT_TABLES

module = ModuleManifest(
    code="services",
    version="0.1.0a1",
    core=False,
    short_code="services",
    migration_prefix="se",
    migration_branch="services",
    tables=TENANT_TABLES,
    platform_tables=(),
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
