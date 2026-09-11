"""Installable declaration for the platform-only health owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import MODULE_DATABASE_ROLES_V1

from dotmac_platform_health.models import PLATFORM_TABLES

module = ModuleManifest(
    code="platform_health",
    version="0.2.0a1",
    core=False,
    short_code="health",
    migration_prefix="ph",
    migration_branch="platform_health",
    tables=(),
    platform_tables=PLATFORM_TABLES,
    requires=(MODULE_DATABASE_ROLES_V1.name,),
)

__all__ = ["module"]
