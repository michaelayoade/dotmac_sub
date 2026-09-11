"""Installable module declaration for the document-series allocation owner.

`mod_numbering` / prefix `nu` / branch label `numbering` are allocated in
`dotmac_kernel.namespaces.MIGRATION_OWNER_LEDGER` (`NUMBERING_MIGRATION_OWNER`,
kernel `0.1.0a65`), and this manifest must match that row exactly or the module
cannot register at all.

Both plane tuples are populated, and neither is speculative. A tenant allocates
its own invoice, credit-note and order series; the control plane allocates
vendor-side series that no tenant may read. ADR-0023 wants both DECLARED rather
than one inferred from the absence of a tenant column — which matters here more
than usual, because the two planes hold structurally identical tables and a
reader cannot tell them apart from the columns alone.

Deliberately absent: any permission, audit-action or setting-domain
declaration. This module decides nothing a product would authorise — it hands
back the next value of a series the product configured, and who may ask for one
is the calling owner's decision. A numbering module that declared its own
permissions would be claiming an authority it does not have.
"""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.planes import ModulePlane
from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_numbering.models import PLATFORM_TABLES, TENANT_TABLES

module = ModuleManifest(
    code="numbering",
    version="0.1.0a2",
    core=False,
    short_code="numbering",
    migration_prefix="nu",
    migration_branch="numbering",
    tables=TENANT_TABLES,
    platform_tables=PLATFORM_TABLES,
    # Roles are common to both planes: the tenant plane needs FORCEd RLS
    # against the tenant app role, and the platform plane needs that same role
    # REVOKEd, so both halves of the isolation need the roles to exist.
    #
    # The tenant catalogue is a TENANT-plane prerequisite only. A control plane
    # that selects PLATFORM alone installs this module without a `tenants`
    # table, which is exactly the vendor-side case.
    #
    # The at-most-once ledger is a COMMON requirement, and it is the one
    # prerequisite here that nothing in this module's own DDL touches:
    # `allocate` delegates to `execute_once` / `execute_once_platform`, so the
    # tables are read at REQUEST time. Undeclared (0.1.0a1), an adopter that
    # has not run the kernel's own migrations passes every gate this module
    # has, migrates cleanly, and fails on its first allocation instead — the
    # `docs/inventories/numbering-erp-adoption-slice.md` finding. A runtime
    # dependency is still a dependency, and both planes call one of the pair.
    requires=(MODULE_DATABASE_ROLES_V1.name, IDEMPOTENCY_LEDGER_V1.name),
    tenant_requires=(TENANT_SCOPE_CATALOG_V1.name,),
    supported_plane_sets=(
        (ModulePlane.TENANT,),
        (ModulePlane.PLATFORM,),
        (ModulePlane.TENANT, ModulePlane.PLATFORM),
    ),
)

__all__ = ["module"]
