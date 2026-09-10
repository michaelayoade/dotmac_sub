"""Installable-module declaration for the employment-directory owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    PARTY_PERSON_CATALOG_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_people.models import TENANT_TABLES

module = ModuleManifest(
    code="people",
    version="0.1.0a2",
    core=False,
    short_code="people",
    migration_prefix="pe",
    migration_branch="people",
    tables=TENANT_TABLES,
    platform_tables=(),
    requires=(
        TENANT_SCOPE_CATALOG_V1.name,
        MODULE_DATABASE_ROLES_V1.name,
        PARTY_PERSON_CATALOG_V1.name,
    ),
)

__all__ = ["module"]
