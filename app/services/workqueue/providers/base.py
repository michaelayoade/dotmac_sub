"""The provider contract.

A provider owns exactly one item source. It receives a resolved scope and must
return only items that scope permits; the aggregator does not re-filter, it only
ranks. Adding a source means writing a provider and registering it — the
aggregator never changes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.workqueue.scope import WorkqueueScope
from app.services.workqueue.scoring_config import WorkqueueScoringConfig
from app.services.workqueue.types import ItemKind, WorkqueueItem


@runtime_checkable
class WorkqueueProvider(Protocol):
    kind: ItemKind

    def fetch(
        self,
        db: Session,
        *,
        scope: WorkqueueScope,
        config: WorkqueueScoringConfig,
        snoozed_ids: set[UUID],
        now: datetime,
        limit: int,
    ) -> list[WorkqueueItem]: ...
