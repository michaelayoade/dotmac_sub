"""Installable module declaration for the import-run ledger."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_imports.models import TENANT_TABLES

module = ModuleManifest(
    code="imports",
    version="0.1.0a2",
    core=False,
    short_code="imports",
    migration_prefix="im",
    migration_branch="imports",
    tables=TENANT_TABLES,
    # Needs a tenant catalogue for its foreign keys and roles to grant to —
    # never the kernel's identity/RBAC/audit estate. The assembly binds these
    # effects to the revisions that supply them (ADR-0006 D1 amendment).
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
