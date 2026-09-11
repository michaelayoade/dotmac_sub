"""Tenant-only module declaration for controlled documents."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_documents.models import TABLES

module = ModuleManifest(
    code="documents",
    version="0.1.0a1",
    core=False,
    short_code="documents",
    migration_prefix="do",
    migration_branch="documents",
    tables=TABLES,
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
