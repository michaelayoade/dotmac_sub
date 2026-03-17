"""
Provisioning Log Service.

Manages CRUD operations for NAS provisioning log entries.
Extracted from the monolithic nas.py service.
"""
import logging
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import (
    ProvisioningAction,
    ProvisioningLog,
    ProvisioningLogStatus,
)
from app.schemas.catalog import ProvisioningLogCreate
from app.services.common import apply_pagination, coerce_uuid
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)


class ProvisioningLogs(ListResponseMixin):
    """Service class for provisioning log operations."""

    @staticmethod
    def create(db: Session, payload: ProvisioningLogCreate) -> ProvisioningLog:
        """Create a new provisioning log entry."""
        data = payload.model_dump(exclude_unset=True)
        log = ProvisioningLog(**data)
        db.add(log)
        db.commit()
        db.refresh(log)
        return log

    @staticmethod
    def get(db: Session, log_id: str | UUID) -> ProvisioningLog:
        """Get a provisioning log by ID."""
        log_id = coerce_uuid(log_id)
        log = cast(ProvisioningLog | None, db.get(ProvisioningLog, log_id))
        if not log:
            raise HTTPException(status_code=404, detail="Provisioning log not found")
        return log

    @staticmethod
    def list(
        db: Session,
        *,
        limit: int = 50,
        offset: int = 0,
        nas_device_id: UUID | None = None,
        subscriber_id: UUID | None = None,
        action: ProvisioningAction | None = None,
        status: ProvisioningLogStatus | None = None,
    ) -> list[ProvisioningLog]:
        """List provisioning logs with filtering."""
        query = select(ProvisioningLog).order_by(ProvisioningLog.created_at.desc())

        if nas_device_id:
            query = query.where(ProvisioningLog.nas_device_id == nas_device_id)
        if subscriber_id:
            query = query.where(ProvisioningLog.subscriber_id == subscriber_id)
        if action:
            query = query.where(ProvisioningLog.action == action)
        if status:
            query = query.where(ProvisioningLog.status == status)

        query = apply_pagination(query, limit, offset)
        return list(db.execute(query).scalars().all())

    @staticmethod
    def count(
        db: Session,
        *,
        nas_device_id: UUID | None = None,
        subscriber_id: UUID | None = None,
        action: ProvisioningAction | None = None,
        status: ProvisioningLogStatus | None = None,
    ) -> int:
        """Count provisioning logs with filtering (same filters as list)."""
        query = select(func.count(ProvisioningLog.id))

        if nas_device_id:
            query = query.where(ProvisioningLog.nas_device_id == nas_device_id)
        if subscriber_id:
            query = query.where(ProvisioningLog.subscriber_id == subscriber_id)
        if action:
            query = query.where(ProvisioningLog.action == action)
        if status:
            query = query.where(ProvisioningLog.status == status)

        return db.execute(query).scalar() or 0

    @staticmethod
    def update_status(
        db: Session,
        log_id: UUID,
        status: ProvisioningLogStatus,
        response: str | None = None,
        error: str | None = None,
        execution_time_ms: int | None = None,
    ) -> ProvisioningLog:
        """Update the status of a provisioning log."""
        log = ProvisioningLogs.get(db, log_id)
        log.status = status
        if response:
            log.response_received = response
        if error:
            log.error_message = error
        if execution_time_ms:
            log.execution_time_ms = execution_time_ms
        db.commit()
        db.refresh(log)
        return log
