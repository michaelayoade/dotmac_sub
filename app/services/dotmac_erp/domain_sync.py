"""Watermarked Sub -> ERP operational context feed.

ERP needs project/ticket/work-order context for expense and finance reporting,
but must no longer pull it from CRM. This feed is idempotent at ERP and advances
each local cursor only when the complete bulk request succeeds without errors.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.erp_domain_sync import ErpDomainSyncCursor
from app.models.project import Project
from app.models.support import Ticket
from app.models.work_order_mirror import WorkOrderMirror
from app.services.dotmac_erp.client import DotMacERPClient, build_erp_client

_DOMAINS = ("projects", "tickets", "work_orders")


def _cursor(db: Session, domain: str) -> ErpDomainSyncCursor:
    row = db.get(ErpDomainSyncCursor, domain)
    if row is None:
        row = ErpDomainSyncCursor(domain=domain)
        db.add(row)
        db.flush()
    return row


def _after_cursor(query, model, cursor: ErpDomainSyncCursor):
    if cursor.watermark_at is None:
        return query
    return query.filter(
        or_(
            model.updated_at > cursor.watermark_at,
            and_(
                model.updated_at == cursor.watermark_at,
                model.id > cursor.watermark_id,
            ),
        )
    )


def _projects(db: Session, cursor: ErpDomainSyncCursor, limit: int):
    return (
        _after_cursor(db.query(Project), Project, cursor)
        .order_by(Project.updated_at.asc(), Project.id.asc())
        .limit(limit)
        .all()
    )


def _tickets(db: Session, cursor: ErpDomainSyncCursor, limit: int):
    return (
        _after_cursor(db.query(Ticket), Ticket, cursor)
        .order_by(Ticket.updated_at.asc(), Ticket.id.asc())
        .limit(limit)
        .all()
    )


def _work_orders(db: Session, cursor: ErpDomainSyncCursor, limit: int):
    return (
        _after_cursor(db.query(WorkOrderMirror), WorkOrderMirror, cursor)
        .order_by(WorkOrderMirror.updated_at.asc(), WorkOrderMirror.id.asc())
        .limit(limit)
        .all()
    )


def _project_payload(row: Project) -> dict:
    subscriber = row.subscriber
    customer_name = None
    if subscriber:
        customer_name = (
            f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
        )
    return {
        "source_id": str(row.id),
        "name": row.name,
        "code": row.code,
        "project_type": row.project_type,
        "status": row.status,
        "priority": row.priority,
        "region": row.region,
        "description": row.description,
        "start_at": row.start_at.isoformat() if row.start_at else None,
        "due_at": row.due_at.isoformat() if row.due_at else None,
        "customer_name": customer_name or None,
        "customer_crm_id": str(row.subscriber_id) if row.subscriber_id else None,
        "metadata": {"source_system": "dotmac_sub", **(row.metadata_ or {})},
        "service_team_name": row.service_team.name if row.service_team else None,
    }


def _ticket_payload(row: Ticket) -> dict:
    channel = getattr(row.channel, "value", row.channel)
    return {
        "source_id": str(row.id),
        "subject": row.title,
        "ticket_number": row.number,
        "ticket_type": row.ticket_type,
        "status": row.status,
        "priority": row.priority,
        "description": row.description,
        "customer_crm_id": str(row.subscriber_id) if row.subscriber_id else None,
        "metadata": {
            "source_system": "dotmac_sub",
            "channel": str(channel) if channel else None,
            **(row.metadata_ or {}),
        },
        "comments": [],
        "activity_log": [],
    }


def _work_order_payload(row: WorkOrderMirror) -> dict:
    metadata = dict(row.metadata_ or {})
    metadata.update(
        {
            "source_system": "dotmac_sub",
            "address": row.address,
            "subscriber_id": str(row.subscriber_id),
        }
    )
    return {
        "source_id": str(row.id),
        "title": row.title,
        "work_type": row.work_type,
        "status": row.status,
        "priority": row.priority,
        "project_crm_id": row.crm_project_id,
        "ticket_crm_id": row.crm_ticket_id,
        "scheduled_start": (
            row.scheduled_start.isoformat() if row.scheduled_start else None
        ),
        "scheduled_end": row.scheduled_end.isoformat() if row.scheduled_end else None,
        "metadata": metadata,
    }


def _advance(cursor: ErpDomainSyncCursor, rows: list) -> None:
    if not rows:
        return
    last = rows[-1]
    cursor.watermark_at = last.updated_at
    cursor.watermark_id = last.id
    cursor.updated_at = datetime.now(UTC)


def sync_operational_domains(
    db: Session,
    *,
    client: DotMacERPClient | None = None,
    batch_size: int = 100,
) -> dict:
    limit = max(1, min(int(batch_size or 100), 500))
    cursors = {domain: _cursor(db, domain) for domain in _DOMAINS}
    projects = _projects(db, cursors["projects"], limit)
    tickets = _tickets(db, cursors["tickets"], limit)
    work_orders = _work_orders(db, cursors["work_orders"], limit)
    if not projects and not tickets and not work_orders:
        db.commit()
        return {"projects": 0, "tickets": 0, "work_orders": 0, "errors": []}

    payload = {
        "projects": [_project_payload(row) for row in projects],
        "tickets": [_ticket_payload(row) for row in tickets],
        "work_orders": [_work_order_payload(row) for row in work_orders],
    }
    owned_client = client or build_erp_client(db)
    created_client = client is None
    try:
        response = owned_client.sync_operational_domains(payload)
    finally:
        if created_client:
            owned_client.close()
    errors = response.get("errors") or []
    if errors:
        db.rollback()
        return {
            "projects": 0,
            "tickets": 0,
            "work_orders": 0,
            "errors": errors,
        }
    _advance(cursors["projects"], projects)
    _advance(cursors["tickets"], tickets)
    _advance(cursors["work_orders"], work_orders)
    db.commit()
    return {
        "projects": len(projects),
        "tickets": len(tickets),
        "work_orders": len(work_orders),
        "errors": [],
    }
