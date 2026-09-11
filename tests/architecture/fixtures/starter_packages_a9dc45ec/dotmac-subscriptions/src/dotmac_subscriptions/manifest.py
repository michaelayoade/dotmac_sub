"""Selectable dual-plane manifest for the recurring-commercial owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.planes import ModulePlane
from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_subscriptions.models import PLATFORM_TABLES, TENANT_TABLES

module = ModuleManifest(
    code="subscriptions",
    version="0.1.0a3",
    core=False,
    short_code="subscriptions",
    migration_prefix="su",
    migration_branch="subscriptions",
    tables=TENANT_TABLES,
    platform_tables=PLATFORM_TABLES,
    requires=(MODULE_DATABASE_ROLES_V1.name, IDEMPOTENCY_LEDGER_V1.name),
    tenant_requires=(TENANT_SCOPE_CATALOG_V1.name,),
    supported_plane_sets=(
        (ModulePlane.TENANT,),
        (ModulePlane.PLATFORM,),
        (ModulePlane.TENANT, ModulePlane.PLATFORM),
    ),
)

__all__ = ["module"]
