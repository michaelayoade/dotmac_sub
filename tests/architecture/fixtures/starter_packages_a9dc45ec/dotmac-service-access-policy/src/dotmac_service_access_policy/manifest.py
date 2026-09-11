"""Manifest for desired service-access policy."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_service_access_policy.models import TENANT_TABLES

module = ModuleManifest(
    code="service_access_policy",
    version="0.1.0a1",
    core=False,
    short_code="serviceaccess",
    migration_prefix="sap",
    migration_branch="service_access_policy",
    tables=TENANT_TABLES,
    platform_tables=(),
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
