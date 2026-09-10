"""Installable declaration for the dual-plane Collections owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.planes import ModulePlane
from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    OUTBOX_RELAY_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_collections.models import PLATFORM_TABLES, TENANT_TABLES

module = ModuleManifest(
    code="collections",
    version="0.1.0a1",
    core=False,
    short_code="coll",
    migration_prefix="cl",
    migration_branch="collections",
    tables=TENANT_TABLES,
    platform_tables=PLATFORM_TABLES,
    requires=(
        MODULE_DATABASE_ROLES_V1.name,
        IDEMPOTENCY_LEDGER_V1.name,
        OUTBOX_RELAY_V1.name,
    ),
    outbox_event_types=(
        "collections.case.step_due.v1",
        "collections.action.requested.v1",
        "collections.notice.requested.v1",
    ),
    tenant_requires=(TENANT_SCOPE_CATALOG_V1.name,),
    supported_plane_sets=(
        (ModulePlane.TENANT,),
        (ModulePlane.PLATFORM,),
        (ModulePlane.TENANT, ModulePlane.PLATFORM),
    ),
)

__all__ = ["module"]
