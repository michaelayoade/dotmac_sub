"""Tenant-only module declaration for payroll."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_payroll.models import TENANT_TABLES

module = ModuleManifest(
    code="payroll",
    version="0.1.0a1",
    core=False,
    short_code="payroll",
    migration_prefix="py",
    migration_branch="payroll",
    tables=TENANT_TABLES,
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
