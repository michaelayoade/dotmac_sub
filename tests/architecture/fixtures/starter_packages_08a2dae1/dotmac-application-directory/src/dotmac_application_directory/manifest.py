"""The directory's `ModuleManifest` — the third stateful module.

Four fields make it stateful, and each must match this module's row in
`dotmac_kernel.namespaces.MIGRATION_OWNER_LEDGER`
(`APPLICATION_DIRECTORY_MIGRATION_OWNER`) exactly, or
`NamespaceRegistry.from_manifests` refuses the composition at boot:

- `short_code="appdir"` → the derived, read-only schema `mod_appdir`
- `migration_prefix="ad"` → revision ids `ad_0001_…`
- `migration_branch="application_directory"` → how an `alembic_version` row is
  attributed
- `tables=(...)` → the composed gate rejects a migration creating anything
  outside this declaration, in both directions

## Why `core=False`

Most deployments have no connected-application portfolio. A target application
— Sub, ERP, this starter — is on the receiving end of the model and needs
nothing from this module; only a Workspace does. Being non-core is what makes
that a real answer rather than a formality.

## No routers, and therefore no declarations

This release ships the contract, the vocabulary, the table and the service. It
ships no `api_routers` and no `web_routers`, so it declares no capabilities, no
permissions and no audit actions.

That is a deliberate omission, on the precedent `dotmac-ticketing` set: every
one of those declarations exists to gate or annotate a ROUTE, and CI enforces it
(`test_every_declared_capability_is_enforced_somewhere`). A declared code with
no consumer is dead vocabulary that reads like a working gate — the failure mode
ADR-0008's registries exist to prevent.

The surface belongs to the Workspace assembly in any case. ADR-0021 records that
the portal is the assembly's UI facet rather than a domain module, so the
launcher screens live in `dotmac_workspace` and this module stays a domain.
"""

from __future__ import annotations

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

module = ModuleManifest(
    code="application_directory",
    version="0.1.0a3",
    core=False,
    # ── D1 database identity ────────────────────────────────────────────────
    short_code="appdir",
    migration_prefix="ad",
    migration_branch="application_directory",
    tables=("application_bindings",),
    # Needs a tenant catalogue for its foreign keys and roles to grant to —
    # never the kernel's identity/RBAC/audit estate. The assembly binds these
    # effects to the revisions that supply them (ADR-0006 D1 amendment).
    requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name),
)

__all__ = ["module"]
