"""Tenant-only module declaration for first-party web analytics."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_web_analytics.models import TENANT_TABLES

module = ModuleManifest(
    code="web_analytics",
    version="0.1.0a1",
    core=False,
    short_code="webanalytics",
    migration_prefix="wa",
    migration_branch="web_analytics",
    tables=TENANT_TABLES,
    platform_tables=(),
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
