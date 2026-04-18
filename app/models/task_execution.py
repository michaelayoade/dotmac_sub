"""Task execution tracking for Celery task idempotency.

This module provides the TaskExecution model which tracks task executions
to prevent duplicate processing of the same logical operation.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TaskExecutionStatus(enum.Enum):
    """Status of a task execution."""

    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class TaskExecution(Base):
    """Tracks task executions for idempotency.

    When a task decorated with @idempotent_task runs, it first checks for
    an existing TaskExecution with the same idempotency_key. If found:
    - If running: skip execution (task already in progress)
    - If succeeded: return cached result (already completed successfully)
    - If failed: may retry depending on configuration

    This prevents issues like:
    - Double-charging payments due to retry
    - Duplicate provisioning of services
    - Race conditions in concurrent task execution
    """

    __tablename__ = "task_executions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Unique key identifying this logical operation
    # Format typically: "{task_name}:{entity_id}" or "{task_name}:{composite_key}"
    idempotency_key: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False
    )

    # The Celery task name for debugging/monitoring
    task_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Current execution status
    status: Mapped[TaskExecutionStatus] = mapped_column(
        Enum(TaskExecutionStatus), nullable=False, index=True
    )

    # The Celery task ID for correlation
    celery_task_id: Mapped[str | None] = mapped_column(String(255))

    # Result data (for succeeded tasks) or error info (for failed tasks)
    result: Mapped[dict | None] = mapped_column(JSONB)

    # Error message if failed
    error_message: Mapped[str | None] = mapped_column(Text)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Index for finding stale running tasks (older than threshold)
        Index("ix_task_executions_status_created", "status", "created_at"),
    )
