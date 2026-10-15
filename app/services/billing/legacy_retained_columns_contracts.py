"""Closed retained money policy; observations are uncommitted component facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.services.billing.legacy_archive_fence_contracts import (
    CatalogDigest,
    DatabaseOid,
    FunctionOid,
    RelationOid,
    RoleOid,
    TransactionId,
    TriggerOid,
)


class RetainedColumnsRevision(StrEnum):
    V1 = "billing-private-retained-columns-v1"


class RetainedRelation(StrEnum):
    BILLING_ACCOUNTS = "billing_accounts"
    SUBSCRIBERS = "subscribers"


@dataclass(frozen=True)
class RetainedColumnsRequest:
    revision: RetainedColumnsRevision = RetainedColumnsRevision.V1

    def __post_init__(self) -> None:
        if self.revision is not RetainedColumnsRevision.V1:
            raise ValueError("Unsupported retained columns revision")


@dataclass(frozen=True)
class RetainedRelationObservation:
    relation: RetainedRelation
    oid: RelationOid
    function_oid: FunctionOid
    row_guard_oid: TriggerOid
    delete_guard_oid: TriggerOid
    truncate_guard_oid: TriggerOid


@dataclass(frozen=True)
class RetainedColumnsStagedObservation:
    """Transaction-local catalog facts, never admission or a receipt."""

    revision: RetainedColumnsRevision
    command_id: UUID
    database_oid: DatabaseOid
    backend_pid: int
    transaction_id: TransactionId
    owner_oid: RoleOid
    runtime_oid: RoleOid
    relations: tuple[RetainedRelationObservation, ...]
    catalog_digest: CatalogDigest
    replay_observed: bool


@dataclass(frozen=True)
class RetainedColumnsAuditPayload:
    revision: RetainedColumnsRevision
    table_count: int
    catalog_digest: CatalogDigest
    command_id: UUID

    def serialize(self) -> dict[str, object]:
        return {
            "revision": self.revision.value,
            "table_count": self.table_count,
            "catalog_digest": str(self.catalog_digest),
            "command_id": str(self.command_id),
        }
