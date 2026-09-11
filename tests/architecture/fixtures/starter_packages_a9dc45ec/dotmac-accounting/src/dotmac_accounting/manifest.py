"""Installable declaration for the tenant accounting owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_accounting.models import TABLES

module = ModuleManifest(
    code="accounting",
    version="0.1.0a1",
    core=False,
    short_code="accounting",
    migration_prefix="ac",
    migration_branch="accounting",
    tables=TABLES,
    requires=(
        TENANT_SCOPE_CATALOG_V1.name,
        MODULE_DATABASE_ROLES_V1.name,
        IDEMPOTENCY_LEDGER_V1.name,
    ),
)

__all__ = ["module"]
