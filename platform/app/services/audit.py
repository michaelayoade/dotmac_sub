from fastapi import HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent, AuditActorType
from app.schemas.audit import AuditEventCreate


def _apply_ordering(query, order_by, order_dir, allowed_columns):
    if order_by not in allowed_columns:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid order_by. Allowed: {', '.join(sorted(allowed_columns))}",
        )
    column = allowed_columns[order_by]
    if order_dir == "desc":
        return query.order_by(column.desc())
    return query.order_by(column.asc())


def _apply_pagination(query, limit, offset):
    return query.limit(limit).offset(offset)


class AuditEvents:
    @staticmethod
    def parse_actor_type(value: str | None) -> AuditActorType | None:
        if value is None:
            return None
        try:
            return AuditActorType(value)
        except ValueError as exc:
            allowed = ", ".join(sorted(a.value for a in AuditActorType))
            raise HTTPException(
                status_code=400,
                detail=f"Invalid actor_type. Allowed: {allowed}",
            ) from exc

    @staticmethod
    def create(db: Session, payload: AuditEventCreate):
        data = payload.model_dump()
        if payload.occurred_at is None:
            data.pop("occurred_at", None)
        event = AuditEvent(**data)
        db.add(event)
        db.commit()
        db.refresh(event)
        return event

    @staticmethod
    def get(db: Session, event_id: str):
        event = db.get(AuditEvent, event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Audit event not found")
        return event

    @staticmethod
    def list(
        db: Session,
        actor_id: str | None,
        actor_type: AuditActorType | None,
        action: str | None,
        entity_type: str | None,
        request_id: str | None,
        is_success: bool | None,
        status_code: int | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(AuditEvent)
        if actor_id:
            query = query.filter(AuditEvent.actor_id == actor_id)
        if actor_type:
            query = query.filter(AuditEvent.actor_type == actor_type)
        if action:
            query = query.filter(AuditEvent.action == action)
        if entity_type:
            query = query.filter(AuditEvent.entity_type == entity_type)
        if request_id:
            query = query.filter(AuditEvent.request_id == request_id)
        if is_success is not None:
            query = query.filter(AuditEvent.is_success == is_success)
        if status_code is not None:
            query = query.filter(AuditEvent.status_code == status_code)
        if is_active is None:
            query = query.filter(AuditEvent.is_active.is_(True))
        else:
            query = query.filter(AuditEvent.is_active == is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "occurred_at": AuditEvent.occurred_at,
                "action": AuditEvent.action,
                "entity_type": AuditEvent.entity_type,
                "status_code": AuditEvent.status_code,
            },
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def log_request(db: Session, request: Request, response: Response):
        actor_type = request.headers.get("x-actor-type", AuditActorType.system.value)
        actor_id = request.headers.get("x-actor-id")
        request_id = request.headers.get("x-request-id")
        entity_id = request.headers.get("x-entity-id")
        ip_address = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        try:
            resolved_actor_type = AuditActorType(actor_type)
        except ValueError:
            resolved_actor_type = AuditActorType.system
        payload = AuditEventCreate(
            actor_type=resolved_actor_type,
            actor_id=actor_id,
            action=request.method,
            entity_type=request.url.path,
            entity_id=entity_id,
            status_code=response.status_code,
            is_success=response.status_code < 400,
            ip_address=ip_address,
            user_agent=user_agent,
            request_id=request_id,
            metadata={
                "path": request.url.path,
                "query": dict(request.query_params),
            },
        )
        event = AuditEvent(**payload.model_dump())
        db.add(event)
        db.commit()

    @staticmethod
    def delete(db: Session, event_id: str):
        event = db.get(AuditEvent, event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Audit event not found")
        event.is_active = False
        db.commit()


audit_events = AuditEvents()
