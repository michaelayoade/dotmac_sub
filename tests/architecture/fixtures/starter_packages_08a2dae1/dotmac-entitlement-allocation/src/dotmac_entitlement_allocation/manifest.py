"""Entitlement allocation's `ModuleManifest` — the fourth stateful module.

Must match `ENTITLEMENT_ALLOCATION_MIGRATION_OWNER` in the kernel ledger exactly
or `NamespaceRegistry.from_manifests` refuses the composition at boot:
`short_code="ealloc"` → `mod_ealloc`, `migration_prefix="ea"` → `ea_0001_…`,
`migration_branch="entitlement_allocation"`, and `tables` bounding what the
composed gate will accept the migration creating.

## `core=False`, and vendor-assembly-only beyond that

Ruling C4 splits allocation from granting: the control plane ALLOCATES, the
product data plane is the only writer of its own `tenant_entitlement_grants`.
A data plane installing this module would be acquiring the wrong half. Being
non-core is the first guard; the import-linter contract forbidding the assembly
from importing it is the second.

## One audit action, declared because it HAS a consumer

`entitlement_allocation.staged` is written by `service.stage_allocation` inside
its idempotent operation, so the declaration is live rather than aspirational —
which is the whole test ADR-0008's registries apply: a declared code with no
consumer is dead vocabulary that reads as a working gate.

Capabilities and permissions stay undeclared for exactly the same reason
inverted: this release ships no routers, so there is nothing for them to gate.
`dotmac-ticketing` proved the point the loud way — CI rejected a declared
capability code no mounted route enforced. They land with the routers, in the
same change as the guards that reference them.
"""

from __future__ import annotations

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import IDEMPOTENCY_LEDGER_V1, PLATFORM_AUDIT_LOG_V1

module = ModuleManifest(
    code="entitlement_allocation",
    version="0.1.0a6+dev",
    core=False,
    short_code="ealloc",
    migration_prefix="ea",
    migration_branch="entitlement_allocation",
    tables=(),
    platform_tables=("allocations", "allocation_entries"),
    # ── Logical database prerequisites ──────────────────────────────────────
    # The TWO effects this module needs that its own migrations do not create.
    # `stage_allocation` delegates at-most-once to the kernel (hard rule 21,
    # ADR-0014), so `public.platform_idempotency_records` is written at REQUEST
    # time and nothing in `ea_0001` touches it. Undeclared — every release up
    # to and including `0.1.0a4` — an adopter that runs its own lineage and
    # never ran the kernel's passes every gate this module has, migrates
    # cleanly, and dies on `UndefinedTable` at the first staged activation. A
    # runtime dependency is still a dependency; it just has no DDL to betray
    # it. Same defect as `dotmac-numbering` 0.1.0a1 and `dotmac-integration`
    # 0.1.0a1..a3, found by the kernel persisted-runtime-dependency inventory
    # and named by kernel a66.
    #
    # COMMON, not `platform_requires`, and the reason is the same one
    # integration reached rather than the one numbering had. Numbering is
    # plane-SELECTABLE and both of its planes call one of the pair. This module
    # has exactly one plane: `tables` is empty and `supported_plane_sets` is
    # unset, so the declared platform plane is installed atomically and there
    # is no selection under which the requirement could lapse. A
    # plane-conditional list would be conditioning on something that cannot
    # vary — and `resolve_depends_on` cannot even resolve one here, because a
    # plane list needs `module=`, which reads `selected_module_planes`, which
    # no atomic module may have (`validate_module_plane_selections` refuses a
    # selection when only one plane set is supported). The spec is whole in any
    # case: one name, both ledgers, as `IDEMPOTENCY_LEDGER_V1.summary` states.
    #
    # `write_platform_audit_event` writes `public.platform_audit_events` from
    # inside the same operation. Kernel a68 names and verifies that second
    # effect. `ea_0002` and `ea_0003` verify the two provider effects in their
    # provider-lineage order rather than asking one consumer revision to depend
    # on both an ancestor and its descendant.
    requires=(IDEMPOTENCY_LEDGER_V1.name, PLATFORM_AUDIT_LOG_V1.name),
    audit_actions=("entitlement_allocation.staged",),
)

__all__ = ["module"]
