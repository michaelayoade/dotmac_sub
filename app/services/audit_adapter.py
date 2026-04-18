"""Audit boundary for operational services."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.schemas.audit import AuditEventCreate


@dataclass(frozen=True)
class AuditRecord:
    action: str
    entity_type: str
    entity_id: str | None = None
    actor_type: AuditActorType = AuditActorType.system
    actor_id: str | None = None
    status_code: int | None = None
    is_success: bool = True
    ip_address: str | None = None
    user_agent: str | None = None
    request_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    occurred_at: datetime | None = None


class AuditAdapter:
    """Unified audit writer for operations."""

    def build_payload(self, record: AuditRecord) -> AuditEventCreate:
        return AuditEventCreate(
            actor_type=record.actor_type,
            actor_id=record.actor_id,
            action=record.action,
            entity_type=record.entity_type,
            entity_id=record.entity_id,
            status_code=record.status_code,
            is_success=record.is_success,
            ip_address=record.ip_address,
            user_agent=record.user_agent,
            request_id=record.request_id,
            metadata_=dict(record.metadata or {}),
            occurred_at=record.occurred_at,
        )

    def record(
        self,
        db: Session,
        record: AuditRecord,
        *,
        defer_until_commit: bool = False,
    ):
        from app.services import audit as audit_service

        return audit_service.audit_events.record(
            db,
            self.build_payload(record),
            defer_until_commit=defer_until_commit,
        )

    def list_events(self, db: Session, **filters):
        from app.services import audit as audit_service

        return audit_service.audit_events.list(db=db, **filters)


audit_adapter = AuditAdapter()


def record_audit_event(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    actor_type: AuditActorType = AuditActorType.system,
    actor_id: str | None = None,
    metadata: dict[str, object] | None = None,
    status_code: int | None = None,
    is_success: bool = True,
    request_id: str | None = None,
    defer_until_commit: bool = False,
):
    return audit_adapter.record(
        db,
        AuditRecord(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_type=actor_type,
            actor_id=actor_id,
            metadata=dict(metadata or {}),
            status_code=status_code,
            is_success=is_success,
            request_id=request_id,
        ),
        defer_until_commit=defer_until_commit,
    )
