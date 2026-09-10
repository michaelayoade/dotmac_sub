"""Release catalogue's `ModuleManifest` — the third stateful module.

Four fields make it stateful, and they must match this module's row in
`dotmac_kernel.namespaces.MIGRATION_OWNER_LEDGER`
(`RELEASE_CATALOG_MIGRATION_OWNER`) exactly, or `NamespaceRegistry.from_manifests`
refuses the composition at boot:

- `short_code="rel"` → the derived, read-only schema `mod_rel`
- `migration_prefix="rl"` → revision ids `rl_0001_…`
- `migration_branch="release_catalog"` → how an `alembic_version` row is attributed
- `platform_tables=(...)` → the composed gate rejects a migration creating
  anything outside this declaration, in both directions

## Why the platform plane, declared and atomic

The DDL was always control-plane shaped — no `tenant_id`, no RLS, grants to
`platform_api`/`app_admin` and `REVOKE ALL` from `app_user`. Until ADR-0028 the
manifest still declared those tables under `tables=`, which is the tenant slot.
The declaration was simply wrong about what the migration builds, and ADR-0023
is explicit that the plane is DECLARED and never inferred — so a mismatch here
is a real defect, not a formality.

This module is ATOMIC, and says so by saying nothing. `supported_plane_sets`
is deliberately OMITTED rather than written as an explicit `()`: absence already
means atomic, and the generated catalogue renders it that way.

Omitting it is not only tidier — it is the honest compatibility floor. The
keyword is a constructor field that only exists from kernel `0.1.0a61`, so
writing it would force this module's floor up to `a61` for a value the default
already supplies. Omitted, the floor stays at `0.1.0a56`, the earliest published
kernel that has `platform_tables` at all — which is the real requirement.

A singleton `((ModulePlane.PLATFORM,),)` would not make the module selectable
either: the current implementation treats one combination as atomic and rejects
an assembly selection anyway. It would be ceremony without a choice.

There is no tenant consumer, and speculative selectability is the ADR-0006 § 5
speculative extraction wearing different clothes. If a real tenant consumer ever
appears, that is a capability EXPANSION needing product-first evidence, tenant
models and migrations, RLS canaries and a new release.

## Why `core=False`

Most deployments have no business holding a vendor's release catalogue. It is
installed in a vendor or OEM control-plane assembly and nowhere else — the fleet
parts are deliberately not something a product data plane can compose, and being
non-core is the first half of making that true. The second half is the
import-linter contract that fails the build if a data-plane assembly declares it.

## No capabilities, permissions or audit actions YET

This release ships no routers, and every one of those declarations exists to
gate or annotate a route. `dotmac-ticketing` learned this the loud way: CI
rejected a declared capability code that no mounted route enforced. A declared
code with no consumer is dead vocabulary that reads as a working gate — the
exact failure ADR-0008's registries exist to prevent.

`release_catalog.publish` / `.read` and the publish/attest audit actions land in
the release that ships the routers, with the guards that reference them in the
same change.
"""

from __future__ import annotations

from dotmac_kernel.modules import ModuleManifest

module = ModuleManifest(
    code="release_catalog",
    version="0.1.0a4+dev",
    core=False,
    # ── D1 database identity ────────────────────────────────────────────────
    short_code="rel",
    migration_prefix="rl",
    migration_branch="release_catalog",
    tables=(),
    platform_tables=("release_artifacts", "artifact_attestations"),
)

__all__ = ["module"]
