"""Installable tenant-plane media-observations module declaration."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_media_observations.models import TENANT_TABLES

module = ModuleManifest(
    code="media_observations",
    version="0.1.0a1",
    core=False,
    short_code="mediaobs",
    migration_prefix="mo",
    migration_branch="media_observations",
    tables=TENANT_TABLES,
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
