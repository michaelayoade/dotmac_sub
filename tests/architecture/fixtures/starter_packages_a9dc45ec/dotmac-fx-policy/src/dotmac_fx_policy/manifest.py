"""Manifest for effective FX observation and selection policy."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_fx_policy.models import TENANT_TABLES

module = ModuleManifest(
    code="fx_policy",
    version="0.1.0a1",
    core=False,
    short_code="fx_policy",
    migration_prefix="fx",
    migration_branch="fx_policy",
    tables=TENANT_TABLES,
    platform_tables=(),
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
