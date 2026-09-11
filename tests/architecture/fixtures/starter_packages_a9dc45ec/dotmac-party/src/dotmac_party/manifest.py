"""Stateful tenant-only manifest for the Party context owner."""

from __future__ import annotations

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    PARTY_PERSON_CATALOG_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_party.models import TENANT_TABLES

module = ModuleManifest(
    code="party",
    version="0.1.0a1",
    core=False,
    short_code="party",
    migration_prefix="pt",
    migration_branch="party",
    tables=TENANT_TABLES,
    requires=(
        TENANT_SCOPE_CATALOG_V1.name,
        MODULE_DATABASE_ROLES_V1.name,
        PARTY_PERSON_CATALOG_V1.name,
    ),
)

__all__ = ["module"]
