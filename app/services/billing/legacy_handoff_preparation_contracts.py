"""Uncommitted component observations; no seal, receipt or admission authority."""

from dataclasses import dataclass
from typing import NewType

from app.services.billing.legacy_archive_fence_contracts import (
    ArchiveFenceRequest,
    ArchiveFenceStagedObservation,
    RelationOid,
)
from app.services.billing.legacy_retained_columns_contracts import (
    RetainedColumnsRequest,
    RetainedColumnsStagedObservation,
)
from app.services.billing.opening_export_contracts import (
    LockedBillingOpeningPopulation,
    PreviewBillingOpeningExportQuery,
)

HandoffLockRelation = NewType("HandoffLockRelation", str)


@dataclass(frozen=True)
class PrepareLockedLegacyHandoffRequest:
    source: PreviewBillingOpeningExportQuery
    archive: ArchiveFenceRequest = ArchiveFenceRequest()
    retained: RetainedColumnsRequest = RetainedColumnsRequest()


@dataclass(frozen=True)
class HandoffLockedRelationObservation:
    relation: HandoffLockRelation
    oid: RelationOid


@dataclass(frozen=True)
class LockedLegacyHandoffStagedObservation:
    """Actual locked v3 census and the two staged narrow fence components.

    This excludes a complete economic watermark, helper/view/FK closure, PSP
    cash qualification, image/drain evidence, production roles and final seal.
    The caller retains all locks and sole transaction completion responsibility.
    """

    relations: tuple[HandoffLockedRelationObservation, ...]
    capture: LockedBillingOpeningPopulation
    archive: ArchiveFenceStagedObservation
    retained: RetainedColumnsStagedObservation
