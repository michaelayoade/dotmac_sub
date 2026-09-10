"""Tenant-only module declaration for campaign progression."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    OUTBOX_RELAY_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_campaigns.models import TABLES

module = ModuleManifest(
    code="campaigns",
    version="0.1.0a1",
    core=False,
    short_code="campaigns",
    migration_prefix="ca",
    migration_branch="campaigns",
    tables=TABLES,
    requires=(
        TENANT_SCOPE_CATALOG_V1.name,
        MODULE_DATABASE_ROLES_V1.name,
        IDEMPOTENCY_LEDGER_V1.name,
        OUTBOX_RELAY_V1.name,
    ),
)

__all__ = ["module"]
