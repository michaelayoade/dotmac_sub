"""Installable declaration for tenant Compliance Reporting."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_compliance_reporting.models import TENANT_TABLES

module = ModuleManifest(
    code="compliance_reporting",
    version="0.1.0a1",
    core=False,
    short_code="compliance",
    migration_prefix="cr",
    migration_branch="compliance_reporting",
    tables=TENANT_TABLES,
    platform_tables=(),
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)
__all__ = ["module"]
