"""Provisioning event log helpers."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    OntProvisioningEvent,
    OntProvisioningEventStatus,
    OntUnit,
)
from app.services.network.ont_provisioning.result import StepResult

_PROVISIONING_CORRELATION_KEY: ContextVar[str | None] = ContextVar(
    "provisioning_correlation_key",
    default=None,
)


@contextmanager
def provisioning_correlation(correlation_key: str | None) -> Iterator[None]:
    """Attach a correlation key to provisioning events in the current context."""
    token = _PROVISIONING_CORRELATION_KEY.set(correlation_key)
    try:
        yield
    finally:
        _PROVISIONING_CORRELATION_KEY.reset(token)


def current_provisioning_correlation_key() -> str | None:
    """Return the active provisioning event correlation key, if any."""
    return _PROVISIONING_CORRELATION_KEY.get()


def status_from_step_result(result: StepResult) -> OntProvisioningEventStatus:
    """Map a provisioning step result to the durable event status."""
    if result.skipped:
        return OntProvisioningEventStatus.skipped
    if result.waiting:
        return OntProvisioningEventStatus.waiting
    if result.success:
        return OntProvisioningEventStatus.succeeded
    return OntProvisioningEventStatus.failed


def record_ont_provisioning_event(
    db: Session,
    ont: OntUnit,
    step_name: str,
    result: StepResult,
    *,
    action: str = "step_completed",
    event_data: dict[str, Any] | None = None,
    compensation_applied: bool = False,
    correlation_key: str | None = None,
) -> OntProvisioningEvent:
    """Append one immutable provisioning event for an ONT step result."""
    data = dict(result.data or {})
    if event_data:
        data.update(event_data)
    effective_correlation_key = (
        correlation_key
        if correlation_key is not None
        else current_provisioning_correlation_key()
    )

    event = OntProvisioningEvent(
        ont_unit_id=ont.id,
        step_name=step_name,
        action=action,
        status=status_from_step_result(result),
        message=result.message,
        duration_ms=result.duration_ms,
        event_data=data or None,
        compensation_applied=compensation_applied,
        correlation_key=effective_correlation_key,
    )
    db.add(event)
    return event


def list_ont_provisioning_events(
    db: Session,
    ont_id: str | uuid.UUID,
    *,
    limit: int = 100,
) -> list[OntProvisioningEvent]:
    """Return recent provisioning events for an ONT, newest first."""
    ont_uuid = ont_id if isinstance(ont_id, uuid.UUID) else uuid.UUID(str(ont_id))
    stmt = (
        select(OntProvisioningEvent)
        .where(OntProvisioningEvent.ont_unit_id == ont_uuid)
        .order_by(OntProvisioningEvent.created_at.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))
