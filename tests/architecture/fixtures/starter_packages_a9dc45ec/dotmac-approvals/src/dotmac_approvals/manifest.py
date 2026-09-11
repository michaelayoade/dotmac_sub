"""Installable module declaration for the approval-state owner.

`mod_approvals` / prefix `ap` / branch label `approvals` are allocated in
`dotmac_kernel.namespaces.MIGRATION_OWNER_LEDGER` (`APPROVALS_MIGRATION_OWNER`,
kernel `0.1.0a59`), and this manifest must match that row exactly or the module
cannot register at all.

Both plane tuples are populated. That is the ADR-0023 case this module exists to
demonstrate honestly: approvals are a real tenant capability in ERP AND a real
control-plane capability in the vendor control plane, so both planes are
DECLARED rather than one being inferred from a missing column.
"""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.planes import ModulePlane
from dotmac_kernel.prerequisites import (
    MODULE_DATABASE_ROLES_V1,
    OUTBOX_RELAY_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_approvals.models import PLATFORM_TABLES, TENANT_TABLES

module = ModuleManifest(
    code="approvals",
    version="0.1.0a5+dev",
    core=False,
    short_code="approvals",
    migration_prefix="ap",
    migration_branch="approvals",
    tables=TENANT_TABLES,
    platform_tables=PLATFORM_TABLES,
    # Split by plane, but NEVER used as the selector. Vendor CP truthfully has
    # the kernel tenant catalogue in its composed database and still selects
    # only PLATFORM here; the assembly's ModulePlaneSelection owns that intent.
    # Roles are common, while only the tenant plane needs the tenant catalogue.
    #
    # Deliberately NOT an identity or RBAC prerequisite either way: role
    # membership arrives on the `Actor` value at the call site, so this module
    # installs beside a product whose RBAC the kernel has never seen.
    #
    # `outbox_relay.v1` (kernel a67) is the effect this module has always
    # needed and could never name: `dotmac_approvals.outbox` enqueues into
    # `public.outbox_events` / `public.platform_outbox_events` at REQUEST time
    # and `ap_0001` creates neither. COMMON because both planes enqueue — the
    # tenant plane through `enqueue_event`, the control plane through
    # `enqueue_platform_event` — so a PLATFORM-only install needs it too.
    requires=(MODULE_DATABASE_ROLES_V1.name, OUTBOX_RELAY_V1.name),
    tenant_requires=(TENANT_SCOPE_CATALOG_V1.name,),
    supported_plane_sets=(
        (ModulePlane.TENANT,),
        (ModulePlane.PLATFORM,),
        (ModulePlane.TENANT, ModulePlane.PLATFORM),
    ),
)

__all__ = ["module"]
