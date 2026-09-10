"""Brand Profiles' `ModuleManifest` — the seventeenth stateful module.

Must match `BRAND_PROFILES_MIGRATION_OWNER` in the kernel ledger exactly:
`short_code="brand"` -> `mod_brand`, `migration_prefix="bp"` -> `bp_0001_...`,
`migration_branch="brand_profiles"`.

## Genuinely dual-plane, and this is the ADR-0023 case rather than an aspiration

Both plane tuples are populated, and a named assembly exists on each side TODAY:

- TENANT — Sub, whose 897-LOC production `BrandProfile` this is extracted from.
  An operator brands its own portals and a reseller or organization overrides
  within that tenant.
- PLATFORM — the vendor control plane, branding a deployment it ships. This is
  the OEM case, and the only one that needs HOST bindings: a profile has to be
  selectable before any tenant is resolved.

`supported_plane_sets` lists all three combinations because both products are
real and neither installs the other's plane. Sub selects TENANT; Vendor selects
PLATFORM; a future workspace could select both. ADR-0028: the assembly makes an
explicit `ModulePlaneSelection`, and a missing or unsupported one fails the
static composition rather than defaulting.

## ONE audit action, and only on the platform plane

The tenant plane deliberately declares none, because it writes none.
`dotmac_kernel.audit.write_audit_event` is a FROZEN facility — it touches
storage, has no prerequisite spec to declare, and its caller set is ratcheted so
the debt cannot grow unnoticed. A new caller is refused, and refusing it is
correct rather than merely enforced: a module that receives a `Session` does not
know WHO acted, and the tenant trail's actor derivation belongs to the adapter
that does.

A tenant-plane brand change is therefore audited by the assembly route that made
it. The module contributes `record_version`, which is what makes that audit
reconstructable.

Declaring `brand_profile.changed` with no writer would also fail ADR-0008's
every-declared-code-has-a-consumer rule — the vocabulary and the behaviour have
to agree, and here they do by both being absent.

## Prerequisites differ per plane, which is the point of declaring them per plane

- COMMON: the at-most-once ledger, written at REQUEST time by every upsert
  (hard rule 23, ADR-0014), and the database roles, because the tenant plane
  needs FORCEd RLS against the tenant app role and the platform plane needs that
  same role REVOKEd — both halves need the roles to exist.
- TENANT only: the tenant catalogue and `app_current_tenant_id()`, which every
  RLS policy calls. A control plane selecting PLATFORM alone installs this module
  without a `tenants` table at all, which is exactly the vendor-side case and the
  reason this is not a COMMON requirement.
- The tenant audit log is a tenant-plane effect and the platform audit log a
  platform-plane one, declared on their own planes for the same reason.
"""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.planes import ModulePlane
from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    PLATFORM_AUDIT_LOG_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_brand_profiles.models import PLATFORM_TABLES, TENANT_TABLES

module = ModuleManifest(
    code="brand_profiles",
    version="0.1.0a1",
    core=False,
    short_code="brand",
    migration_prefix="bp",
    migration_branch="brand_profiles",
    tables=TENANT_TABLES,
    platform_tables=PLATFORM_TABLES,
    requires=(MODULE_DATABASE_ROLES_V1.name, IDEMPOTENCY_LEDGER_V1.name),
    tenant_requires=(TENANT_SCOPE_CATALOG_V1.name,),
    platform_requires=(PLATFORM_AUDIT_LOG_V1.name,),
    supported_plane_sets=(
        (ModulePlane.TENANT,),
        (ModulePlane.PLATFORM,),
        (ModulePlane.TENANT, ModulePlane.PLATFORM),
    ),
    audit_actions=("platform_brand_profile.changed",),
)

__all__ = ["module"]
