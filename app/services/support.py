from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from app.models.domain_settings import SettingDomain
from app.models.notification import NotificationChannel, NotificationStatus
from app.models.provisioning import ServiceOrder
from app.models.support import (
    Ticket,
    TicketAssignee,
    TicketComment,
    TicketLink,
    TicketMerge,
    TicketSlaEvent,
    TicketStatus,
)
from app.schemas.notification import NotificationCreate
from app.schemas.provisioning import ServiceOrderCreate
from app.schemas.support import (
    TicketBulkUpdateRequest,
    TicketCommentCreate,
    TicketCommentUpdate,
    TicketCreate,
    TicketMergeRequest,
    TicketSlaEventCreate,
    TicketSlaEventUpdate,
    TicketUpdate,
)
from app.services import domain_settings as domain_settings_service
from app.services import notification as notification_service
from app.services import numbering as numbering_service
from app.services import provisioning as provisioning_service
from app.services.audit_helpers import log_audit_event
from app.services.common import apply_ordering, apply_pagination
from app.services.events import emit_event
from app.services.events.types import EventType

logger = logging.getLogger(__name__)

MENTION_EMAIL_RE = re.compile(r"@([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")

SUPPORT_NOTIFICATION_TOGGLE_KEY = "support_ticket_notifications_enabled"
SUPPORT_AUTO_ASSIGN_ENABLED_KEY = "support_ticket_auto_assign_enabled"
SUPPORT_REGION_ASSIGNMENT_RULES_KEY = "support_region_assignment_rules"
SUPPORT_SERVICE_TEAM_MEMBERS_KEY = "support_service_team_members"


def _now() -> datetime:
    return datetime.now(UTC)


def _coerce_uuid(value: str) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _ensure_not_merged_source(ticket: Ticket) -> None:
    if ticket.merged_into_ticket_id is not None or ticket.status == TicketStatus.merged:
        raise HTTPException(
            status_code=409, detail="Cannot modify a merged source ticket"
        )


def _merge_attachment_dicts(
    base: list[dict] | None, extra: list[dict] | None
) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in (base or []) + (extra or []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("storage_key") or item.get("url") or "")
        name = str(item.get("file_name") or "")
        dedupe_key = (key, name)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        merged.append(item)
    return merged


def _read_bool_setting(
    db: Session, domain: SettingDomain, key: str, default: bool
) -> bool:
    try:
        setting = (
            domain_settings_service.settings.get_by_key(db, key)
            if domain is None
            else None
        )
    except Exception:
        setting = None

    if setting is None:
        try:
            domain_client = getattr(domain_settings_service, f"{domain.value}_settings")
            setting = domain_client.get_by_key(db, key)
        except Exception:
            setting = None

    if not setting:
        return default

    raw = setting.value_json if setting.value_json is not None else setting.value_text
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _read_json_setting(db: Session, domain: SettingDomain, key: str) -> dict[str, Any]:
    try:
        domain_client = getattr(domain_settings_service, f"{domain.value}_settings")
        setting = domain_client.get_by_key(db, key)
    except Exception:
        return {}
    if isinstance(setting.value_json, dict):
        return dict(setting.value_json)
    return {}


def _parse_mentions(body: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in MENTION_EMAIL_RE.findall(body):
        email = match.strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        out.append(email)
    return out


class TicketComments:
    @staticmethod
    def get(db: Session, comment_id: str) -> TicketComment:
        comment = db.get(TicketComment, comment_id)
        if not comment:
            raise HTTPException(status_code=404, detail="Ticket comment not found")
        return comment

    @staticmethod
    def list(
        db: Session,
        ticket_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TicketComment]:
        query = (
            db.query(TicketComment)
            .filter(TicketComment.ticket_id == ticket_id)
            .order_by(TicketComment.created_at.asc())
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def create(
        db: Session,
        *,
        ticket: Ticket,
        payload: TicketCommentCreate,
        actor_id: str | None,
        request=None,
    ) -> TicketComment:
        _ensure_not_merged_source(ticket)
        comment = TicketComment(
            ticket_id=ticket.id,
            author_person_id=payload.author_person_id,
            body=payload.body.strip(),
            is_internal=payload.is_internal,
            attachments=[item.model_dump() for item in payload.attachments],
        )
        db.add(comment)
        db.flush()

        log_audit_event(
            db=db,
            request=request,
            action="comment_add",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor_id=actor_id,
            metadata={
                "comment_id": str(comment.id),
                "is_internal": payload.is_internal,
            },
        )
        return comment

    @staticmethod
    def update(
        db: Session,
        *,
        comment: TicketComment,
        payload: TicketCommentUpdate,
        actor_id: str | None,
        request=None,
    ) -> TicketComment:
        ticket = db.get(Ticket, comment.ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        _ensure_not_merged_source(ticket)

        data = payload.model_dump(exclude_unset=True)
        if "body" in data:
            comment.body = str(data["body"]).strip()
        if "is_internal" in data:
            comment.is_internal = bool(data["is_internal"])
        if "attachments" in data and data["attachments"] is not None:
            comment.attachments = [item.model_dump() for item in data["attachments"]]

        log_audit_event(
            db=db,
            request=request,
            action="comment_edit",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor_id=actor_id,
            metadata={"comment_id": str(comment.id)},
        )
        db.commit()
        db.refresh(comment)
        return comment

    @staticmethod
    def delete(
        db: Session, *, comment: TicketComment, actor_id: str | None, request=None
    ) -> None:
        ticket = db.get(Ticket, comment.ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        _ensure_not_merged_source(ticket)
        db.delete(comment)
        log_audit_event(
            db=db,
            request=request,
            action="comment_delete",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor_id=actor_id,
            metadata={"comment_id": str(comment.id)},
        )
        db.commit()


class TicketSlaEvents:
    @staticmethod
    def get(db: Session, event_id: str) -> TicketSlaEvent:
        event = db.get(TicketSlaEvent, event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Ticket SLA event not found")
        return event

    @staticmethod
    def list(
        db: Session, ticket_id: str, limit: int = 100, offset: int = 0
    ) -> list[TicketSlaEvent]:
        query = (
            db.query(TicketSlaEvent)
            .filter(TicketSlaEvent.ticket_id == ticket_id)
            .order_by(TicketSlaEvent.created_at.desc())
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def create(db: Session, payload: TicketSlaEventCreate) -> TicketSlaEvent:
        ticket = db.get(Ticket, payload.ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        _ensure_not_merged_source(ticket)
        event = TicketSlaEvent(
            ticket_id=payload.ticket_id,
            event_type=payload.event_type,
            expected_at=payload.expected_at,
            actual_at=payload.actual_at,
            metadata_=payload.metadata_,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        return event

    @staticmethod
    def update(
        db: Session, event: TicketSlaEvent, payload: TicketSlaEventUpdate
    ) -> TicketSlaEvent:
        ticket = db.get(Ticket, event.ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        _ensure_not_merged_source(ticket)
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(event, key, value)
        db.commit()
        db.refresh(event)
        return event

    @staticmethod
    def delete(db: Session, event: TicketSlaEvent) -> None:
        ticket = db.get(Ticket, event.ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        _ensure_not_merged_source(ticket)
        db.delete(event)
        db.commit()


class Tickets:
    @staticmethod
    def list_response(
        db: Session,
        search: str | None = None,
        status: str | None = None,
        ticket_type: str | None = None,
        assigned_to_person_id: str | None = None,
        project_manager_person_id: str | None = None,
        site_coordinator_person_id: str | None = None,
        subscriber_id: str | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        items = Tickets.list(
            db,
            search=search,
            status=status,
            ticket_type=ticket_type,
            assigned_to_person_id=assigned_to_person_id,
            project_manager_person_id=project_manager_person_id,
            site_coordinator_person_id=site_coordinator_person_id,
            subscriber_id=subscriber_id,
            order_by=order_by,
            order_dir=order_dir,
            limit=limit,
            offset=offset,
        )
        return {"items": items, "count": len(items), "limit": limit, "offset": offset}

    @staticmethod
    def _resolve_ticket_number(db: Session) -> str | None:
        return numbering_service.generate_number(
            db=db,
            domain=SettingDomain.workflow,
            sequence_key="support_ticket",
            enabled_key="support_ticket_numbering_enabled",
            prefix_key="support_ticket_number_prefix",
            padding_key="support_ticket_number_padding",
            start_key="support_ticket_number_start",
        )

    @staticmethod
    def _assert_ticket_exists(db: Session, ticket_id: UUID) -> Ticket:
        ticket = db.get(Ticket, ticket_id)
        if not ticket or not ticket.is_active:
            raise HTTPException(status_code=404, detail="Ticket not found")
        return ticket

    @staticmethod
    def _apply_status_timestamp_rules(
        ticket: Ticket, explicit_data: dict[str, Any]
    ) -> None:
        if ticket.status == TicketStatus.resolved:
            if explicit_data.get("resolved_at") is None and ticket.resolved_at is None:
                ticket.resolved_at = _now()
        if ticket.status == TicketStatus.closed:
            if explicit_data.get("closed_at") is None and ticket.closed_at is None:
                ticket.closed_at = _now()

    @staticmethod
    def _replace_assignees(db: Session, ticket: Ticket, person_ids: list[UUID]) -> None:
        deduped: list[UUID] = []
        seen: set[str] = set()
        for person_id in person_ids:
            key = str(person_id)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(person_id)

        db.query(TicketAssignee).filter(TicketAssignee.ticket_id == ticket.id).delete()
        for person_id in deduped:
            db.add(TicketAssignee(ticket_id=ticket.id, person_id=person_id))

        # Backward compatibility legacy single assignee.
        if deduped and not ticket.assigned_to_person_id:
            ticket.assigned_to_person_id = deduped[0]

    @staticmethod
    def _auto_assignment_enabled(db: Session) -> bool:
        return _read_bool_setting(
            db, SettingDomain.workflow, SUPPORT_AUTO_ASSIGN_ENABLED_KEY, True
        )

    @staticmethod
    def _notifications_enabled(db: Session) -> bool:
        return _read_bool_setting(
            db, SettingDomain.notification, SUPPORT_NOTIFICATION_TOGGLE_KEY, False
        )

    @staticmethod
    def _apply_region_auto_assignment(ticket: Ticket, db: Session) -> dict[str, Any]:
        rules = _read_json_setting(
            db, SettingDomain.workflow, SUPPORT_REGION_ASSIGNMENT_RULES_KEY
        )
        if not ticket.region:
            return {"matched": False, "reason": "region_missing"}
        region_rule = rules.get(ticket.region) if isinstance(rules, dict) else None
        if not isinstance(region_rule, dict):
            return {"matched": False, "reason": "no_rule"}

        changed: dict[str, Any] = {}
        if not ticket.ticket_manager_person_id and region_rule.get(
            "ticket_manager_person_id"
        ):
            ticket.ticket_manager_person_id = _coerce_uuid(
                region_rule.get("ticket_manager_person_id")
            )
            changed["ticket_manager_person_id"] = str(ticket.ticket_manager_person_id)
        if not ticket.site_coordinator_person_id and region_rule.get(
            "site_coordinator_person_id"
        ):
            ticket.site_coordinator_person_id = _coerce_uuid(
                region_rule.get("site_coordinator_person_id")
            )
            changed["site_coordinator_person_id"] = str(
                ticket.site_coordinator_person_id
            )
        if not ticket.technician_person_id and region_rule.get("technician_person_id"):
            ticket.technician_person_id = _coerce_uuid(
                region_rule.get("technician_person_id")
            )
            changed["technician_person_id"] = str(ticket.technician_person_id)
        if not ticket.service_team_id and region_rule.get("service_team_id"):
            ticket.service_team_id = _coerce_uuid(region_rule.get("service_team_id"))
            changed["service_team_id"] = str(ticket.service_team_id)

        assignee_ids = (
            region_rule.get("assignee_person_ids")
            if isinstance(region_rule.get("assignee_person_ids"), list)
            else []
        )
        if assignee_ids:
            resolved = [
                uid
                for uid in (_coerce_uuid(v) for v in assignee_ids)
                if uid is not None
            ]
            if resolved:
                existing = {
                    str(row.person_id)
                    for row in db.query(TicketAssignee)
                    .filter(TicketAssignee.ticket_id == ticket.id)
                    .all()
                }
                for person_id in resolved:
                    if str(person_id) in existing:
                        continue
                    db.add(TicketAssignee(ticket_id=ticket.id, person_id=person_id))
                changed["assignee_person_ids"] = [str(uid) for uid in resolved]

        return {"matched": True, "changes": changed}

    @staticmethod
    def _ensure_field_visit_work_order(db: Session, ticket: Ticket) -> None:
        tags = {
            str(tag).strip().lower() for tag in (ticket.tags or []) if str(tag).strip()
        }
        if "field_visit" not in tags:
            return
        if not ticket.subscriber_id:
            return

        metadata = dict(ticket.metadata_ or {})
        existing_order_id = metadata.get("work_order_id")
        if existing_order_id:
            order = db.get(ServiceOrder, existing_order_id)
            if order:
                return

        order = provisioning_service.service_orders.create(
            db,
            ServiceOrderCreate(
                account_id=ticket.subscriber_id,
                notes=f"Auto-created from support ticket {ticket.number or ticket.id}",
            ),
        )
        metadata["work_order_id"] = str(order.id)
        ticket.metadata_ = metadata

    @staticmethod
    def _queue_notifications_for_assignments(
        db: Session, ticket: Ticket, actor_id: str | None
    ) -> None:
        if not Tickets._notifications_enabled(db):
            return

        recipients: set[str] = set()
        for candidate in [
            ticket.technician_person_id,
            ticket.ticket_manager_person_id,
            ticket.site_coordinator_person_id,
            ticket.assigned_to_person_id,
        ]:
            if candidate:
                recipients.add(str(candidate))

        assignee_rows = (
            db.query(TicketAssignee).filter(TicketAssignee.ticket_id == ticket.id).all()
        )
        recipients.update(str(row.person_id) for row in assignee_rows)

        if ticket.service_team_id:
            team_map = _read_json_setting(
                db, SettingDomain.workflow, SUPPORT_SERVICE_TEAM_MEMBERS_KEY
            )
            team_members = (
                team_map.get(str(ticket.service_team_id))
                if isinstance(team_map, dict)
                else None
            )
            if isinstance(team_members, list):
                for member in team_members:
                    member_uuid = _coerce_uuid(str(member))
                    if member_uuid:
                        recipients.add(str(member_uuid))

        if actor_id:
            recipients.discard(str(actor_id))

        subject = f"Ticket assigned: {ticket.number or str(ticket.id)[:8]}"
        body = f"Ticket {ticket.number or ticket.id} assignment updated."
        for recipient in recipients:
            notification_service.notifications.create(
                db,
                NotificationCreate(
                    channel=NotificationChannel.push,
                    recipient=recipient,
                    subject=subject,
                    body=body,
                    status=NotificationStatus.delivered,
                    sent_at=_now(),
                ),
            )

        # Email queue for service-team assignments.
        if ticket.service_team_id:
            for recipient in recipients:
                notification_service.notifications.create(
                    db,
                    NotificationCreate(
                        channel=NotificationChannel.email,
                        recipient=recipient,
                        subject=subject,
                        body=body,
                        status=NotificationStatus.queued,
                    ),
                )

    @staticmethod
    def _queue_mention_notifications(
        db: Session, ticket: Ticket, body: str, actor_id: str | None
    ) -> None:
        if not Tickets._notifications_enabled(db):
            return

        mentioned_emails = _parse_mentions(body)
        if not mentioned_emails:
            return

        from app.services import subscriber as subscriber_service

        recipients = subscriber_service.subscribers.list_active_by_emails(
            db, mentioned_emails
        )
        subject = f"Mentioned in ticket {ticket.number or str(ticket.id)[:8]}"
        for subscriber in recipients:
            if actor_id and str(subscriber.id) == str(actor_id):
                continue
            notification_service.notifications.create(
                db,
                NotificationCreate(
                    channel=NotificationChannel.push,
                    recipient=str(subscriber.id),
                    subject=subject,
                    body=body,
                    status=NotificationStatus.delivered,
                    sent_at=_now(),
                ),
            )

    @staticmethod
    def _emit_ticket_event(
        db: Session, event_name: str, ticket: Ticket, actor_id: str | None = None
    ) -> None:
        payload = {
            "name": event_name,
            "ticket_id": str(ticket.id),
            "ticket_number": ticket.number,
            "status": ticket.status.value,
            "priority": ticket.priority.value,
            "channel": ticket.channel.value,
            "customer_account_id": str(ticket.customer_account_id)
            if ticket.customer_account_id
            else None,
            "subscriber_id": str(ticket.subscriber_id)
            if ticket.subscriber_id
            else None,
            "actor_id": actor_id,
        }
        emit_event(
            db,
            EventType.custom,
            payload,
            actor=actor_id,
            subscriber_id=ticket.subscriber_id,
            account_id=ticket.customer_account_id or ticket.subscriber_id,
        )

    @staticmethod
    def create(
        db: Session, payload: TicketCreate, actor_id: str | None = None, request=None
    ) -> Ticket:
        data = payload.model_dump()
        data["status"] = data.get("status") or TicketStatus.open

        ticket = Ticket(
            **{
                k: v
                for k, v in data.items()
                if k not in {"assignee_person_ids", "related_outage_ticket_id"}
            }
        )
        ticket.number = Tickets._resolve_ticket_number(db)

        db.add(ticket)
        db.flush()

        assignee_ids = payload.assignee_person_ids or []
        if assignee_ids:
            Tickets._replace_assignees(db, ticket, assignee_ids)

        if payload.related_outage_ticket_id:
            link = TicketLink(
                from_ticket_id=ticket.id,
                to_ticket_id=payload.related_outage_ticket_id,
                link_type="related_outage",
                created_by_person_id=_coerce_uuid(actor_id) if actor_id else None,
            )
            db.add(link)

        if Tickets._auto_assignment_enabled(db):
            Tickets._apply_region_auto_assignment(ticket, db)

        Tickets._apply_status_timestamp_rules(ticket, data)
        Tickets._ensure_field_visit_work_order(db, ticket)

        Tickets._queue_notifications_for_assignments(db, ticket, actor_id)

        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor_id=actor_id,
            metadata={"number": ticket.number},
        )
        Tickets._emit_ticket_event(db, "ticket.created", ticket, actor_id)

        db.commit()
        db.refresh(ticket)
        return ticket

    @staticmethod
    def get(db: Session, ticket_id: str) -> Ticket:
        ticket = db.get(Ticket, ticket_id)
        if not ticket or not ticket.is_active:
            raise HTTPException(status_code=404, detail="Ticket not found")
        return ticket

    @staticmethod
    def get_by_lookup(db: Session, ticket_lookup: str) -> Ticket:
        lookup = ticket_lookup.strip()
        ticket_uuid = _coerce_uuid(lookup)
        query = (
            db.query(Ticket)
            .options(selectinload(Ticket.assignees))
            .filter(Ticket.is_active.is_(True))
        )
        if ticket_uuid:
            query = query.filter(or_(Ticket.id == ticket_uuid, Ticket.number == lookup))
        else:
            query = query.filter(Ticket.number == lookup)
        ticket = query.first()
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        return ticket

    @staticmethod
    def list(
        db: Session,
        search: str | None = None,
        status: str | None = None,
        ticket_type: str | None = None,
        assigned_to_person_id: str | None = None,
        project_manager_person_id: str | None = None,
        site_coordinator_person_id: str | None = None,
        subscriber_id: str | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> list[Ticket]:
        query = (
            db.query(Ticket)
            .options(selectinload(Ticket.assignees))
            .filter(Ticket.is_active.is_(True))
        )
        if search:
            like = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    Ticket.number.ilike(like),
                    Ticket.title.ilike(like),
                    Ticket.description.ilike(like),
                )
            )
        if status:
            try:
                query = query.filter(Ticket.status == TicketStatus(status))
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail="Invalid ticket status"
                ) from exc
        if ticket_type:
            query = query.filter(Ticket.ticket_type == ticket_type)
        if assigned_to_person_id:
            query = query.filter(
                or_(
                    Ticket.assigned_to_person_id == assigned_to_person_id,
                    Ticket.id.in_(
                        db.query(TicketAssignee.ticket_id).filter(
                            TicketAssignee.person_id == assigned_to_person_id
                        )
                    ),
                )
            )
        if project_manager_person_id:
            query = query.filter(
                Ticket.ticket_manager_person_id == project_manager_person_id
            )
        if site_coordinator_person_id:
            query = query.filter(
                Ticket.site_coordinator_person_id == site_coordinator_person_id
            )
        if subscriber_id:
            query = query.filter(
                or_(
                    Ticket.subscriber_id == subscriber_id,
                    Ticket.customer_account_id == subscriber_id,
                )
            )

        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": Ticket.created_at,
                "updated_at": Ticket.updated_at,
                "due_at": Ticket.due_at,
                "priority": Ticket.priority,
                "status": Ticket.status,
                "number": Ticket.number,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(
        db: Session,
        ticket_id: str,
        payload: TicketUpdate,
        actor_id: str | None = None,
        request=None,
    ) -> Ticket:
        ticket = Tickets.get(db, ticket_id)
        _ensure_not_merged_source(ticket)

        before = {
            "status": ticket.status.value,
            "priority": ticket.priority.value,
            "assigned_to_person_id": str(ticket.assigned_to_person_id)
            if ticket.assigned_to_person_id
            else None,
            "ticket_manager_person_id": str(ticket.ticket_manager_person_id)
            if ticket.ticket_manager_person_id
            else None,
            "site_coordinator_person_id": str(ticket.site_coordinator_person_id)
            if ticket.site_coordinator_person_id
            else None,
            "service_team_id": str(ticket.service_team_id)
            if ticket.service_team_id
            else None,
        }

        data = payload.model_dump(exclude_unset=True)
        assignee_person_ids = data.pop("assignee_person_ids", None)

        if "status" in data and data["status"] is None:
            data.pop("status")
        if "priority" in data and data["priority"] is None:
            data.pop("priority")

        for key, value in data.items():
            setattr(ticket, key, value)

        Tickets._apply_status_timestamp_rules(ticket, data)

        if assignee_person_ids is not None:
            Tickets._replace_assignees(db, ticket, assignee_person_ids)

        if Tickets._auto_assignment_enabled(db):
            Tickets._apply_region_auto_assignment(ticket, db)

        Tickets._ensure_field_visit_work_order(db, ticket)
        Tickets._queue_notifications_for_assignments(db, ticket, actor_id)

        after = {
            "status": ticket.status.value,
            "priority": ticket.priority.value,
            "assigned_to_person_id": str(ticket.assigned_to_person_id)
            if ticket.assigned_to_person_id
            else None,
            "ticket_manager_person_id": str(ticket.ticket_manager_person_id)
            if ticket.ticket_manager_person_id
            else None,
            "site_coordinator_person_id": str(ticket.site_coordinator_person_id)
            if ticket.site_coordinator_person_id
            else None,
            "service_team_id": str(ticket.service_team_id)
            if ticket.service_team_id
            else None,
        }

        changes = {
            field: {"from": before[field], "to": after[field]}
            for field in before.keys()
            if before[field] != after[field]
        }
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor_id=actor_id,
            metadata={"changes": changes} if changes else None,
        )

        if before["status"] != after["status"]:
            log_audit_event(
                db=db,
                request=request,
                action="status_change",
                entity_type="support_ticket",
                entity_id=str(ticket.id),
                actor_id=actor_id,
                metadata={"from": before["status"], "to": after["status"]},
            )
        if before["priority"] != after["priority"]:
            log_audit_event(
                db=db,
                request=request,
                action="priority_change",
                entity_type="support_ticket",
                entity_id=str(ticket.id),
                actor_id=actor_id,
                metadata={"from": before["priority"], "to": after["priority"]},
            )

        if changes and any(
            key in changes
            for key in [
                "assigned_to_person_id",
                "ticket_manager_person_id",
                "site_coordinator_person_id",
                "service_team_id",
            ]
        ):
            Tickets._emit_ticket_event(db, "ticket.assigned", ticket, actor_id)

        db.commit()
        db.refresh(ticket)
        return ticket

    @staticmethod
    def soft_delete(
        db: Session, ticket_id: str, actor_id: str | None = None, request=None
    ) -> None:
        ticket = Tickets.get(db, ticket_id)
        _ensure_not_merged_source(ticket)
        ticket.is_active = False
        log_audit_event(
            db=db,
            request=request,
            action="delete",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor_id=actor_id,
            metadata={"soft_deleted": True},
        )
        db.commit()

    @staticmethod
    def bulk_update(
        db: Session,
        payload: TicketBulkUpdateRequest,
        actor_id: str | None = None,
        request=None,
    ) -> list[Ticket]:
        updated: list[Ticket] = []
        for item in payload.items:
            ticket = Tickets.get(db, str(item.ticket_id))
            _ensure_not_merged_source(ticket)
            if item.status is not None:
                ticket.status = item.status
            if item.priority is not None:
                ticket.priority = item.priority
            if item.assigned_to_person_id is not None:
                ticket.assigned_to_person_id = item.assigned_to_person_id
            Tickets._apply_status_timestamp_rules(
                ticket, item.model_dump(exclude_unset=True)
            )
            updated.append(ticket)

        log_audit_event(
            db=db,
            request=request,
            action="bulk_update",
            entity_type="support_ticket",
            entity_id=None,
            actor_id=actor_id,
            metadata={"count": len(updated)},
        )
        db.commit()
        return updated

    @staticmethod
    def manual_auto_assign(
        db: Session, ticket_id: str, actor_id: str | None = None, request=None
    ) -> dict[str, Any]:
        ticket = Tickets.get(db, ticket_id)
        _ensure_not_merged_source(ticket)
        result = Tickets._apply_region_auto_assignment(ticket, db)
        log_audit_event(
            db=db,
            request=request,
            action="auto_assignment",
            entity_type="support_ticket",
            entity_id=str(ticket.id),
            actor_id=actor_id,
            metadata={"result": result},
        )
        Tickets._queue_notifications_for_assignments(db, ticket, actor_id)
        db.commit()
        return result

    @staticmethod
    def create_comment(
        db: Session,
        ticket_id: str,
        payload: TicketCommentCreate,
        actor_id: str | None = None,
        request=None,
    ) -> TicketComment:
        ticket = Tickets.get(db, ticket_id)
        comment = ticket_comments.create(
            db, ticket=ticket, payload=payload, actor_id=actor_id, request=request
        )
        Tickets._queue_mention_notifications(db, ticket, payload.body, actor_id)
        db.commit()
        db.refresh(comment)
        return comment

    @staticmethod
    def bulk_create_comments(
        db: Session,
        ticket_id: str,
        payloads: list[TicketCommentCreate],
        actor_id: str | None = None,
        request=None,
    ) -> list[TicketComment]:
        ticket = Tickets.get(db, ticket_id)
        comments: list[TicketComment] = []
        for payload in payloads:
            comment = ticket_comments.create(
                db, ticket=ticket, payload=payload, actor_id=actor_id, request=request
            )
            Tickets._queue_mention_notifications(db, ticket, payload.body, actor_id)
            comments.append(comment)
        db.commit()
        for comment in comments:
            db.refresh(comment)
        return comments

    @staticmethod
    def link_ticket(
        db: Session,
        *,
        from_ticket_id: str,
        to_ticket_id: str,
        link_type: str,
        actor_id: str | None,
        request=None,
    ) -> TicketLink:
        source = Tickets.get(db, from_ticket_id)
        target = Tickets.get(db, to_ticket_id)
        _ensure_not_merged_source(source)
        _ensure_not_merged_source(target)

        existing = (
            db.query(TicketLink)
            .filter(TicketLink.from_ticket_id == source.id)
            .filter(TicketLink.to_ticket_id == target.id)
            .filter(TicketLink.link_type == link_type)
            .first()
        )
        if existing:
            return existing

        link = TicketLink(
            from_ticket_id=source.id,
            to_ticket_id=target.id,
            link_type=link_type,
            created_by_person_id=_coerce_uuid(actor_id) if actor_id else None,
        )
        db.add(link)
        log_audit_event(
            db=db,
            request=request,
            action="link",
            entity_type="support_ticket",
            entity_id=str(source.id),
            actor_id=actor_id,
            metadata={"to_ticket_id": str(target.id), "link_type": link_type},
        )
        db.commit()
        db.refresh(link)
        return link

    @staticmethod
    def list_links(
        db: Session,
        ticket_id: str,
        *,
        limit: int = 100,
    ) -> list[TicketLink]:
        ticket = Tickets.get(db, ticket_id)
        return (
            db.query(TicketLink)
            .filter(
                or_(
                    TicketLink.from_ticket_id == ticket.id,
                    TicketLink.to_ticket_id == ticket.id,
                )
            )
            .order_by(TicketLink.created_at.desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def merge(
        db: Session,
        source_ticket_id: str,
        payload: TicketMergeRequest,
        actor_id: str | None = None,
        request=None,
    ) -> Ticket:
        source = Tickets.get(db, source_ticket_id)
        target = Tickets.get(db, str(payload.target_ticket_id))
        if source.id == target.id:
            raise HTTPException(
                status_code=400, detail="Cannot merge a ticket into itself"
            )
        _ensure_not_merged_source(source)
        _ensure_not_merged_source(target)

        # Merge comments and attachments.
        db.query(TicketComment).filter(TicketComment.ticket_id == source.id).update(
            {TicketComment.ticket_id: target.id}, synchronize_session=False
        )
        target.attachments = _merge_attachment_dicts(
            target.attachments, source.attachments
        )

        # Merge assignees.
        target_assignee_ids = {
            str(row.person_id)
            for row in db.query(TicketAssignee)
            .filter(TicketAssignee.ticket_id == target.id)
            .all()
        }
        source_assignees = (
            db.query(TicketAssignee).filter(TicketAssignee.ticket_id == source.id).all()
        )
        for row in source_assignees:
            if str(row.person_id) in target_assignee_ids:
                continue
            db.add(TicketAssignee(ticket_id=target.id, person_id=row.person_id))

        # Move/dedupe links.
        links = (
            db.query(TicketLink)
            .filter(
                or_(
                    TicketLink.from_ticket_id == source.id,
                    TicketLink.to_ticket_id == source.id,
                )
            )
            .all()
        )
        for link in links:
            from_id = (
                target.id if link.from_ticket_id == source.id else link.from_ticket_id
            )
            to_id = target.id if link.to_ticket_id == source.id else link.to_ticket_id
            if from_id == to_id:
                db.delete(link)
                continue
            duplicate = (
                db.query(TicketLink)
                .filter(TicketLink.id != link.id)
                .filter(TicketLink.from_ticket_id == from_id)
                .filter(TicketLink.to_ticket_id == to_id)
                .filter(TicketLink.link_type == link.link_type)
                .first()
            )
            if duplicate:
                db.delete(link)
            else:
                link.from_ticket_id = from_id
                link.to_ticket_id = to_id

        db.query(TicketAssignee).filter(TicketAssignee.ticket_id == source.id).delete()

        source.status = TicketStatus.merged
        source.merged_into_ticket_id = target.id

        merge_row = TicketMerge(
            source_ticket_id=source.id,
            target_ticket_id=target.id,
            reason=payload.reason,
            merged_by_person_id=_coerce_uuid(actor_id) if actor_id else None,
        )
        db.add(merge_row)

        source_comment_text = (
            f"System: merged into ticket {target.number or target.id}."
        )
        target_comment_text = (
            f"System: merged ticket {source.number or source.id} into this ticket."
        )

        db.add(
            TicketComment(
                ticket_id=source.id,
                author_person_id=_coerce_uuid(actor_id) if actor_id else None,
                body=source_comment_text,
                is_internal=True,
                attachments=[],
            )
        )
        db.add(
            TicketComment(
                ticket_id=target.id,
                author_person_id=_coerce_uuid(actor_id) if actor_id else None,
                body=target_comment_text,
                is_internal=True,
                attachments=[],
            )
        )

        log_audit_event(
            db=db,
            request=request,
            action="merge",
            entity_type="support_ticket",
            entity_id=str(source.id),
            actor_id=actor_id,
            metadata={"target_ticket_id": str(target.id), "reason": payload.reason},
        )

        db.commit()
        db.refresh(target)
        return target


# ---------------------------------------------------------------------------
# Ticket list-page helpers
# ---------------------------------------------------------------------------


def list_people(db: Session, *, limit: int = 500) -> list[dict[str, str]]:
    """Return active subscribers formatted for ticket people selectors."""
    from app.models.subscriber import Subscriber

    rows = (
        db.query(Subscriber)
        .filter(Subscriber.is_active.is_(True))
        .order_by(Subscriber.first_name.asc(), Subscriber.last_name.asc())
        .limit(limit)
        .all()
    )
    people: list[dict[str, str]] = []
    for row in rows:
        full_name = " ".join(filter(None, [row.first_name, row.last_name])).strip()
        label = row.display_name or full_name or row.email or str(row.id)
        people.append(
            {
                "id": str(row.id),
                "label": label,
                "email": row.email or "",
                "phone": row.phone or "",
                "organization": row.company_name
                if getattr(row, "is_business", False)
                else "",
                "address": ", ".join(
                    [p for p in [row.address_line1, row.city, row.region] if p]
                ),
                "subscriber_number": row.subscriber_number or "",
                "account_number": row.account_number or "",
                "account_status": (row.status.value if row.status else "") or "",
                "plan": "",
                "service_address": ", ".join(
                    [p for p in [row.address_line1, row.city, row.region] if p]
                ),
            }
        )
    return people


def status_totals(db: Session) -> dict[str, int]:
    """Return ticket counts grouped by status."""
    from sqlalchemy import func

    statuses = [
        TicketStatus.new,
        TicketStatus.open,
        TicketStatus.pending,
        TicketStatus.on_hold,
        TicketStatus.resolved,
        TicketStatus.closed,
    ]
    counts = {item.value: 0 for item in statuses}
    rows = (
        db.query(Ticket.status, func.count(Ticket.id))
        .filter(Ticket.is_active.is_(True))
        .group_by(Ticket.status)
        .all()
    )
    for status_value, count in rows:
        key = (
            status_value.value if hasattr(status_value, "value") else str(status_value)
        )
        if key in counts:
            counts[key] = int(count)
    return counts


def ticket_types(db: Session) -> list[str]:
    """Return distinct ticket types with defaults."""
    rows = (
        db.query(Ticket.ticket_type)
        .filter(
            Ticket.is_active.is_(True),
            Ticket.ticket_type.isnot(None),
            Ticket.ticket_type != "",
        )
        .distinct()
        .order_by(Ticket.ticket_type.asc())
        .limit(200)
        .all()
    )
    discovered = [str(item[0]) for item in rows if item and item[0]]
    defaults = ["incident", "request", "change", "maintenance", "outage"]
    return sorted(set(discovered + defaults))


def regions(db: Session) -> list[str]:
    """Return distinct ticket regions with defaults."""
    rows = (
        db.query(Ticket.region)
        .filter(
            Ticket.is_active.is_(True), Ticket.region.isnot(None), Ticket.region != ""
        )
        .distinct()
        .order_by(Ticket.region.asc())
        .limit(200)
        .all()
    )
    discovered = [str(item[0]) for item in rows if item and item[0]]
    defaults = ["north", "south", "east", "west", "central"]
    return sorted(set(discovered + defaults))


tickets = Tickets()
ticket_comments = TicketComments()
ticket_sla_events = TicketSlaEvents()
