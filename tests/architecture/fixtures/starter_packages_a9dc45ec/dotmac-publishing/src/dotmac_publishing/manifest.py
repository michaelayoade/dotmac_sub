"""Tenant-only module declaration for the publication lifecycle owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    OUTBOX_RELAY_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_publishing.models import TENANT_TABLES

module = ModuleManifest(
    code="publishing",
    version="0.1.0a1",
    core=False,
    short_code="publishing",
    migration_prefix="pb",
    migration_branch="publishing",
    tables=TENANT_TABLES,
    platform_tables=(),
    requires=(
        TENANT_SCOPE_CATALOG_V1.name,
        MODULE_DATABASE_ROLES_V1.name,
        IDEMPOTENCY_LEDGER_V1.name,
        OUTBOX_RELAY_V1.name,
    ),
)

__all__ = ["module"]
