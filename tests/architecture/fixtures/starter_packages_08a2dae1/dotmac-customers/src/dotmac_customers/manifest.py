"""Installable-module declaration for customer accounts."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_customers.models import TENANT_TABLES

module = ModuleManifest(
    code="customers",
    version="0.1.0a1",
    core=False,
    short_code="customers",
    migration_prefix="cu",
    migration_branch="customers",
    tables=TENANT_TABLES,
    platform_tables=(),
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
