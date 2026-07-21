"""Device projection reconcile Celery task.

Keeps the materialised ``device_projections`` table fresh by running the
``network.device_projection`` reconciler on a schedule. The reconciler is the
sole canonical writer; this task is a thin transport that hands it a session.
Wire ``reconcile_device_projections`` into Celery beat. See
app/services/device_projection_reconcile.py.
"""

import logging
from typing import Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy.exc import OperationalError

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

logger = logging.getLogger(__name__)


class _TaskRequest(Protocol):
    id: str | None


class _BoundTask(Protocol):
    request: _TaskRequest


@celery_app.task(
    bind=True,
    name="app.tasks.device_projection.reconcile_device_projections",
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
)
def reconcile_device_projections(self: _BoundTask) -> dict[str, int | str]:
    """Rebuild the unified device projection from the authoritative tables."""
    logger.info("Starting device projection reconcile task")
    from app.services import device_projection_reconcile as svc

    request_id = str(self.request.id or uuid4())
    command = svc.ReconcileDeviceProjectionsCommand(
        context=CommandContext.system(
            actor="celery:device_projection_reconcile",
            scope="network:global",
            reason="scheduled device projection freshness repair",
            command_id=uuid5(
                NAMESPACE_URL,
                f"dotmac:device-projection-reconcile:{request_id}",
            ),
            idempotency_key=f"celery:{request_id}",
        )
    )
    try:
        with db_session_adapter.owner_command_session() as db:
            result = svc.reconcile_device_projections(db, command)
    except DomainError as exc:
        logger.warning(
            "Device projection reconcile rejected: %s",
            exc.code,
            extra={
                "event": "device_projection_reconcile_rejected",
                "domain_error_code": exc.code,
                "command_id": str(command.context.command_id),
                "correlation_id": str(command.context.correlation_id),
            },
        )
        raise

    logger.info(
        "Device projection reconcile complete: inserted=%d updated=%d pruned=%d",
        result.inserted,
        result.updated,
        result.pruned,
        extra={
            "event": "device_projection_reconcile_completed",
            "command_id": str(result.command_id),
            "correlation_id": str(result.correlation_id),
        },
    )
    return {
        "inserted": result.inserted,
        "updated": result.updated,
        "pruned": result.pruned,
        "total": result.total,
        "reconciled_at": result.reconciled_at.isoformat(),
        "command_id": str(result.command_id),
        "correlation_id": str(result.correlation_id),
    }
