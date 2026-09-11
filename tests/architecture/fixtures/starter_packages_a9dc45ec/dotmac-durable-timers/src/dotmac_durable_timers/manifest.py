"""Selectable dual-plane manifest for durable timer mechanics."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.planes import ModulePlane
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    OUTBOX_RELAY_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_durable_timers.models import PLATFORM_TABLES, TENANT_TABLES

module = ModuleManifest(
    code="durable_timers",
    version="0.1.0a1",
    core=False,
    short_code="timers",
    migration_prefix="dt",
    migration_branch="durable_timers",
    tables=TENANT_TABLES,
    platform_tables=PLATFORM_TABLES,
    requires=(MODULE_DATABASE_ROLES_V1.name, OUTBOX_RELAY_V1.name),
    tenant_requires=(TENANT_SCOPE_CATALOG_V1.name,),
    supported_plane_sets=(
        (ModulePlane.TENANT,),
        (ModulePlane.PLATFORM,),
        (ModulePlane.TENANT, ModulePlane.PLATFORM),
    ),
)

__all__ = ["module"]
