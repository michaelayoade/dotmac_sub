"""Installable manifest for Fiber Plant."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_fiber_plant.models import TENANT_TABLES

module = ModuleManifest(
    code="fiber_plant",
    version="0.1.0a1",
    core=False,
    short_code="fiber",
    migration_prefix="fp",
    migration_branch="fiber_plant",
    tables=TENANT_TABLES,
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)
__all__ = ["module"]
