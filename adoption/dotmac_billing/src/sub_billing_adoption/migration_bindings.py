"""Exact prerequisite and tenant-plane declarations for the shadow database."""

from __future__ import annotations

from dotmac_kernel.planes import ModulePlane, ModulePlaneSelection
from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    OUTBOX_RELAY_V1,
    TENANT_SCOPE_CATALOG_V1,
    PrerequisiteBinding,
)

SHADOW_PREREQUISITE_BINDINGS = (
    PrerequisiteBinding(
        prerequisite=MODULE_DATABASE_ROLES_V1.name,
        provider_revision="0001_initial_tenant_schema",
        provider_owner="kernel",
    ),
    PrerequisiteBinding(
        prerequisite=TENANT_SCOPE_CATALOG_V1.name,
        provider_revision="0001_initial_tenant_schema",
        provider_owner="kernel",
    ),
    PrerequisiteBinding(
        prerequisite=IDEMPOTENCY_LEDGER_V1.name,
        provider_revision="0018_idempotency_one_owner",
        provider_owner="kernel",
    ),
    PrerequisiteBinding(
        prerequisite=OUTBOX_RELAY_V1.name,
        provider_revision="0012_platform_outbox",
        provider_owner="kernel",
    ),
)

SHADOW_MODULE_PLANES = (
    ModulePlaneSelection(module="billing", planes=(ModulePlane.TENANT,)),
)

__all__ = ["SHADOW_MODULE_PLANES", "SHADOW_PREREQUISITE_BINDINGS"]
