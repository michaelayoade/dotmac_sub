from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_service_orders.models import TENANT_TABLES

module = ModuleManifest(
    code="service_orders",
    version="0.1.0a1",
    core=False,
    short_code="serviceorders",
    migration_prefix="so",
    migration_branch="service_orders",
    tables=TENANT_TABLES,
    platform_tables=(),
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
