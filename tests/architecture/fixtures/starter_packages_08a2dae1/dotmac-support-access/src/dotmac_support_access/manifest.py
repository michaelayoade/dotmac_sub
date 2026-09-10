"""Installable declaration for the platform-only support-access owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import MODULE_DATABASE_ROLES_V1

from dotmac_support_access.models import PLATFORM_TABLES

module = ModuleManifest(
    code="support_access",
    version="0.1.0a1",
    core=False,
    short_code="supportaccess",
    migration_prefix="sup",
    migration_branch="support_access",
    tables=(),
    platform_tables=PLATFORM_TABLES,
    requires=(MODULE_DATABASE_ROLES_V1.name,),
)

__all__ = ["module"]
