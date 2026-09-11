"""Ticketing's `ModuleManifest` — the second stateful module.

Four fields make it stateful, and they must match this module's row in
`dotmac_kernel.namespaces.MIGRATION_OWNER_LEDGER` (`TICKETING_MIGRATION_OWNER`)
exactly, or `NamespaceRegistry.from_manifests` refuses the composition at boot:

- `short_code="tkt"` → the derived, read-only schema `mod_tkt`
- `migration_prefix="tk"` → revision ids `tk_0001_…`
- `migration_branch="ticketing"` → how an `alembic_version` row is attributed
- `tables=(...)` + `platform_tables=(...)` → the composed gate rejects a
  migration creating anything outside these declarations, in both directions

## Two planes (ADR-0023)

This module is dual-plane: the same lifecycle serves a product data plane and a
control plane, so it declares its tables in two sets held to opposite isolation
contracts. `tables` is tenant-scoped (`tenant_id NOT NULL`, FORCEd RLS);
`platform_tables` is control-plane (no tenant column, no RLS, schema `USAGE` +
row DML for `platform_api`, REVOKEd from `app_user`). The classification is
declared here rather than inferred from the absence of a `tenant_id`, because a
tenant table that merely FORGOT its column would otherwise reclassify itself as
platform and lose its isolation silently.

**`tables` lists only this module's own tables.** Product link tables generated
by `dotmac_ticketing.linking`'s per-plane helpers are NOT declared here and must
not be: they live in the product's schema and lineage, and a module claiming
ownership of a table it does not create would make the live-catalog gate wrong
in the direction that matters.

## Why `core=False`

A product may genuinely not want tickets — the vendor control plane's licensing
surface is a plausible deployment with none. Being non-core means "is this
tenant entitled to it" is a real question with a real answer rather than a
formality, and it is what a `ticketing.use` capability will gate once there is a
router to gate. See the comment in the manifest body for why that declaration is
deliberately absent from this release.
"""

from __future__ import annotations

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.planes import ModulePlane
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

module = ModuleManifest(
    code="ticketing",
    version="0.1.0a4",
    core=False,
    # ── D1 database identity ────────────────────────────────────────────────
    short_code="tkt",
    migration_prefix="tk",
    migration_branch="ticketing",
    tables=("tickets", "ticket_comments"),
    platform_tables=("platform_tickets", "platform_ticket_comments"),
    # ── No capabilities, permissions or audit actions YET ───────────────────
    # This release ships no routers, and every one of those declarations exists
    # to gate or annotate a route. CI proved the point rather than leaving it to
    # judgement: `test_every_declared_capability_is_enforced_somewhere` failed
    # this manifest with "capability code(s) declared but enforced by no mounted
    # route: ['ticketing.use'] — wire a require_capability guard, or drop the
    # declaration until there is something to gate."
    #
    # Dropping it is the honest half of that instruction. A declared code with
    # no consumer is dead vocabulary that reads as a working gate, which is the
    # failure mode ADR-0008's registries exist to prevent, and it would have
    # shipped as a permanent allowlist entry if the contract were weaker.
    #
    # `ticketing.use`, the read/work/administer permission split, and the five
    # audit actions all land in the release that ships the routers — with the
    # guards that reference them in the same change.
    # Split by PLANE: roles to create anything, a tenant catalogue only for the
    # tenant plane. Never the kernel's identity/RBAC/audit estate either way;
    # the assembly binds these effects to the revisions that supply them
    # (ADR-0006 D1 amendment).
    requires=(MODULE_DATABASE_ROLES_V1.name,),
    tenant_requires=(TENANT_SCOPE_CATALOG_V1.name,),
    # ADR-0028. Under ADR-0027 this module inferred its planes from which
    # prerequisites happened to be bound, which reads provider availability as
    # if it were intent. The vendor control plane is where those two part
    # company: it composes a truthful tenant catalogue and still wants only
    # platform tickets, so nothing is missing and inference has nothing to see.
    # The assembly now SELECTS, and every combination below is one this lineage
    # is actually built and tested to produce.
    supported_plane_sets=(
        (ModulePlane.TENANT,),
        (ModulePlane.PLATFORM,),
        (ModulePlane.TENANT, ModulePlane.PLATFORM),
    ),
)

__all__ = ["module"]
