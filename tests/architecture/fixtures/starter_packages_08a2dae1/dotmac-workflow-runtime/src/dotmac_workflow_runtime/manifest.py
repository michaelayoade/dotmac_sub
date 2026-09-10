"""Installable declaration for the tenant Workflow Runtime owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_workflow_runtime.models import TENANT_TABLES

module = ModuleManifest(
    code="workflow_runtime",
    version="0.1.0a1",
    core=False,
    short_code="workflow",
    migration_prefix="wr",
    migration_branch="workflow_runtime",
    tables=TENANT_TABLES,
    platform_tables=(),
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
