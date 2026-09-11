"""Licensing's `ModuleManifest` — the fifteenth stateful module.

Must match `LICENSING_MIGRATION_OWNER` in the kernel ledger exactly or
`NamespaceRegistry.from_manifests` refuses the composition at boot:
`short_code="licensing"` → `mod_licensing`, `migration_prefix="li"` →
`li_0001_…`, `migration_branch="licensing"`, and `platform_tables` bounding
what the composed gate will accept the migration creating.

## Platform plane only, and here the reason is a security boundary

`tables=()` is a DECLARATION, not an oversight (ADR-0023 rejects inferring a
plane from a missing `tenant_id`). But this module's plane choice is stronger
than the usual "no tenant consumer exists": a tenant data plane installing
licence ISSUANCE would put the thing that decides what a deployment may do
inside the deployment it decides about.

The receiving half is already elsewhere and already correct —
`dotmac_kernel.licensing` verifies a signed envelope fully OFFLINE, and the
assembly's own `licensing` feature projects it into local grants. A data plane
learns what it may do from a document it can verify without asking anyone,
which is the entire point of a signed licence. Reading the issuer's tables
instead would replace an offline cryptographic check with a network dependency
and a trust relationship.

The import-linter contract forbidding this repository's assembly from importing
`dotmac_licensing` is what keeps that true rather than merely current.

## Three audit actions, split by WHO acted

`licence.issued`, `licence.transitioned`, `licence.acknowledged`. Not one code,
and not a code per verb.

The split is by actor, because that is the question an operator reading an audit
trail is actually answering: *did we do this, or did they?* Issuance and
transition are the issuer's acts; an acknowledgement is a REMOTE party's claim
that this module recorded after checking. Collapsing them would make that
distinction invisible without opening every detail blob — and it is the
distinction that matters when a licence's standing is disputed.

Contrast `dotmac-commercial-agreements`, which declares exactly one: every
transition there is the operator's own act, so there is nothing to distinguish.

## Two logical prerequisites, both written at REQUEST time

Neither is created by this module's own migrations, and an undeclared runtime
dependency is still a dependency — it just has no DDL to betray it. An adopter
that runs its own lineage and never ran the kernel's would pass every gate this
module has, migrate cleanly, and die on `UndefinedTable` at the first issuance.

- Every command delegates at-most-once to the kernel (hard rule 23, ADR-0014),
  writing `public.platform_idempotency_records`.
- Every command writes `public.platform_audit_events` through
  `write_platform_audit_event`, inside the same operation.

COMMON rather than `platform_requires`: this module has exactly one plane,
`supported_plane_sets` is unset, so the declared platform plane installs
atomically and there is no selection under which the requirement could lapse.
"""

from __future__ import annotations

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import IDEMPOTENCY_LEDGER_V1, PLATFORM_AUDIT_LOG_V1

module = ModuleManifest(
    code="licensing",
    version="0.1.0a1+dev",
    core=False,
    short_code="licensing",
    migration_prefix="li",
    migration_branch="licensing",
    tables=(),
    platform_tables=(
        "signing_keys",
        "licences",
        "licence_issuances",
        "licence_acknowledgements",
        "revocations",
        "revocation_lists",
    ),
    requires=(IDEMPOTENCY_LEDGER_V1.name, PLATFORM_AUDIT_LOG_V1.name),
    audit_actions=(
        "licence.issued",
        "licence.transitioned",
        "licence.acknowledged",
    ),
)

__all__ = ["module"]
