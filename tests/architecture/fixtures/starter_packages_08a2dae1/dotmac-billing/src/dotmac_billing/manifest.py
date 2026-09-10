"""Installable declaration for the dual-plane operational-receivables owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.planes import ModulePlane
from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    OUTBOX_RELAY_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_billing.models import PLATFORM_TABLES, TENANT_TABLES

module = ModuleManifest(
    code="billing",
    version="0.1.0a1",
    core=False,
    short_code="billing",
    migration_prefix="bi",
    migration_branch="billing",
    tables=TENANT_TABLES,
    platform_tables=PLATFORM_TABLES,
    requires=(
        MODULE_DATABASE_ROLES_V1.name,
        IDEMPOTENCY_LEDGER_V1.name,
        OUTBOX_RELAY_V1.name,
    ),
    outbox_event_types=(
        "billing.accounting.fact.v1",
        "billing.document.artifact.recorded.v1",
        "billing.document.artifact.repaired.v1",
        "billing.invoice.document.fact.v1",
        "billing.obligation.accepted.v1",
        "billing.receivable.exposure.v1",
        "billing.receivable.position.v1",
        "billing.settlement.accepted.v1",
    ),
    tenant_requires=(TENANT_SCOPE_CATALOG_V1.name,),
    supported_plane_sets=(
        (ModulePlane.TENANT,),
        (ModulePlane.PLATFORM,),
        (ModulePlane.TENANT, ModulePlane.PLATFORM),
    ),
)

__all__ = ["module"]
