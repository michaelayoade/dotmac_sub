"""Installable manifest for the tenant-only fulfillment saga owner."""

from dotmac_kernel.modules import ModuleManifest
from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    TENANT_SCOPE_CATALOG_V1,
)

from dotmac_fulfillment.models import TENANT_TABLES

module = ModuleManifest(
    code="fulfillment",
    version="0.1.0a1",
    core=False,
    dependencies=("durable_timers",),
    short_code="fulfillment",
    migration_prefix="fu",
    migration_branch="fulfillment",
    tables=TENANT_TABLES,
    requires=(
        TENANT_SCOPE_CATALOG_V1.name,
        MODULE_DATABASE_ROLES_V1.name,
        IDEMPOTENCY_LEDGER_V1.name,
    ),
    outbox_event_types=("fulfillment.reobserve_due.v1",),
    audit_actions=(
        "fulfillment.repair.attempt_redriven",
        "fulfillment.repair.compensation_requested",
        "fulfillment.repair.outcome_terminalized",
    ),
)

__all__ = ["module"]
