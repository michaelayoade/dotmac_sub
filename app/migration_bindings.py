"""Sub's answers to the database effects composed modules require.

Sub is an independent application and keeps its own migration lineage.  Kernel
revision ``0001`` cannot be composed here: it would create or rewrite Sub-owned
identity, RBAC and audit tables.  Migration ``544`` therefore supplies only the
four provider-neutral effects the timer and campaigns modules consume.

The bindings are claims, not aliases.  Each requiring module re-verifies the
live PostgreSQL catalogue before creating any of its own objects.
"""

from __future__ import annotations

from typing import Final

from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    OUTBOX_RELAY_V1,
    TENANT_SCOPE_CATALOG_V1,
    PrerequisiteBinding,
)

FOUNDATION_REVISION: Final[str] = "544_campaign_module_foundation"

ASSEMBLY_PREREQUISITE_BINDINGS: Final[tuple[PrerequisiteBinding, ...]] = tuple(
    PrerequisiteBinding(
        prerequisite=prerequisite.name,
        provider_revision=FOUNDATION_REVISION,
        provider_owner="assembly",
    )
    for prerequisite in (
        TENANT_SCOPE_CATALOG_V1,
        MODULE_DATABASE_ROLES_V1,
        IDEMPOTENCY_LEDGER_V1,
        OUTBOX_RELAY_V1,
    )
)

BINDINGS_REFERENCE: Final[str] = (
    "app.migration_bindings:ASSEMBLY_PREREQUISITE_BINDINGS"
)

__all__ = [
    "ASSEMBLY_PREREQUISITE_BINDINGS",
    "BINDINGS_REFERENCE",
    "FOUNDATION_REVISION",
]
