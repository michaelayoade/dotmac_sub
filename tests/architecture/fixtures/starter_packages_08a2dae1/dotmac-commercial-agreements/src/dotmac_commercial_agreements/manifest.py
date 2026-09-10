"""Commercial Agreements' `ModuleManifest` — the fourteenth stateful module.

Must match `COMMERCIAL_AGREEMENTS_MIGRATION_OWNER` in the kernel ledger exactly
or `NamespaceRegistry.from_manifests` refuses the composition at boot:
`short_code="agreements"` → `mod_agreements`, `migration_prefix="cg"` →
`cg_0001_…`, `migration_branch="commercial_agreements"`, and `tables` /
`platform_tables` bounding what the composed gate will accept the migration
creating.

## Platform plane only, declared and not inferred

`tables=()` is a DECLARATION that this module has no tenant plane, not an
oversight. ADR-0023 rejects inferring a plane from a missing `tenant_id`, and
ADR-0057 § 7 derives the plane from a consumer that exists today: the vendor
control plane owns vendor↔operator agreements, and no tenant data plane holds
one. Sub sells ISP service to subscribers, which is a different subject with a
different owner (`dotmac-subscriptions`, ruling A2(a)).

A tenant plane declared "for later" is a plane whose isolation nobody tests.

## `core=False`, and vendor-assembly-only beyond that

A data plane installing this module would be acquiring the wrong half of the
commercial relationship. Being non-core is the first guard; the import-linter
contract forbidding this repository's assembly from importing it is the second.

## One audit action, declared because it HAS a consumer

`commercial_agreement.transitioned` is written by every transition inside its
idempotent operation, so the declaration is live rather than aspirational —
which is the test ADR-0008's registries apply: a declared code with no consumer
is dead vocabulary that reads as a working gate.

It is deliberately ONE code and not ten. The action is "a commercial agreement
transitioned"; which transition is a detail in the record. Declaring a code per
verb would put the lifecycle in two places — the manifest and the status enum —
and leave them free to drift, which is the duplicate-vocabulary defect ADR-0008
exists to prevent rather than an application of it.

Capabilities and permissions stay undeclared for the inverted reason: this
release ships no routers, so there is nothing for them to gate.
`dotmac-ticketing` proved the point the loud way — CI rejected a declared
capability code no mounted route enforced. They land with the routers, in the
same change as the guards that reference them.

## Two logical prerequisites, both written at REQUEST time

Neither is created by this module's own migrations, and an undeclared runtime
dependency is still a dependency — it just has no DDL to betray it. An adopter
that runs its own lineage and never ran the kernel's would pass every gate this
module has, migrate cleanly, and die on `UndefinedTable` at the first
transition. Same defect as `dotmac-numbering` 0.1.0a1 and `dotmac-integration`
0.1.0a1..a3.

- Every command delegates at-most-once to the kernel (hard rule 23, ADR-0014),
  writing `public.platform_idempotency_records`.
- Every transition writes `public.platform_audit_events` through
  `write_platform_audit_event`, inside the same operation.

COMMON rather than `platform_requires`, for the reason
`dotmac-entitlement-allocation` records: this module has exactly one plane,
`supported_plane_sets` is unset, so the declared platform plane installs
atomically and there is no selection under which the requirement could lapse. A
plane-conditional list would condition on something that cannot vary.
"""

from __future__ import annotations

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import IDEMPOTENCY_LEDGER_V1, PLATFORM_AUDIT_LOG_V1

module = ModuleManifest(
    code="commercial_agreements",
    version="0.1.0a2+dev",
    core=False,
    short_code="agreements",
    migration_prefix="cg",
    migration_branch="commercial_agreements",
    tables=(),
    platform_tables=("agreements", "agreement_lines", "agreement_events"),
    requires=(IDEMPOTENCY_LEDGER_V1.name, PLATFORM_AUDIT_LOG_V1.name),
    audit_actions=("commercial_agreement.transitioned",),
)

__all__ = ["module"]
