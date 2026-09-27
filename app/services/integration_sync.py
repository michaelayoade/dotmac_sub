"""Capability-only integration sync orchestration."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.integration import IntegrationJob


class SyncAdapterError(RuntimeError):
    """Raised when a sync job lacks an enabled typed capability."""


# No capability currently registers a handler here (the CRM ticket-pull
# capability that used to be the sole entry was retired — see
# docs/runbooks/CRM_TICKET_CAPABILITY_CUTOVER.md). The dispatcher stays as
# the extension point for the next capability that needs one.
_SYNC_CAPABILITY_HANDLERS: dict[str, Any] = {}


def run_sync_job(
    db: Session, job: IntegrationJob, run_id: UUID
) -> dict[str, Any] | None:
    if job.capability_binding is None:
        raise SyncAdapterError("integration job has no capability binding")
    handler = _SYNC_CAPABILITY_HANDLERS.get(job.capability_binding.capability_id)
    if handler is None:
        raise SyncAdapterError(
            "No sync handler registered for capability "
            f"{job.capability_binding.capability_id}"
        )
    return handler(db, job, run_id)
