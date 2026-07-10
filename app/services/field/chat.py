"""Job-scoped technician chat over the native field chat store.

CRM's field chat rides its inbox/conversation engine (Phase 4); sub has no
such stack, so messages persist in ``field_job_chat_messages`` keyed directly
to the work-order mirror. Technicians can only read/send for jobs in their
assignment scope, and sending is limited to active job states.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.field_chat import FieldJobChatMessage
from app.models.subscriber import Subscriber
from app.models.work_order_mirror import WorkOrderMirror
from app.services.field.jobs import (
    OPEN_STATUSES,
    _profile_from_principal,
    _scoped_query,
    _subscriber_name,
    _system_user,
    _technician_name,
)

MAX_CHAT_MESSAGES = 200


def _serialize(message: FieldJobChatMessage) -> dict:
    return {
        "id": message.id,
        "body": message.body,
        "direction": message.direction,
        "author_name": message.author_name,
        "created_at": message.created_at,
        "read_at": message.read_at,
    }


def _scoped_work_order(
    db: Session,
    principal: dict[str, Any],
    crm_work_order_id: str,
) -> WorkOrderMirror:
    profile = _profile_from_principal(db, principal)
    row = (
        _scoped_query(db, profile)
        .filter(WorkOrderMirror.crm_work_order_id == crm_work_order_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return row


def _can_send(row: WorkOrderMirror) -> bool:
    return bool(row.subscriber_id) and row.status in OPEN_STATUSES


def _customer_name(db: Session, row: WorkOrderMirror) -> str | None:
    subscriber = db.get(Subscriber, row.subscriber_id)
    if subscriber is None:
        return None
    return _subscriber_name(subscriber)


class FieldJobChat:
    @staticmethod
    def get_thread(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
        *,
        limit: int = 50,
    ) -> dict:
        row = _scoped_work_order(db, principal, crm_work_order_id)
        safe_limit = max(1, min(int(limit or 50), MAX_CHAT_MESSAGES))
        messages = (
            db.query(FieldJobChatMessage)
            .filter(FieldJobChatMessage.work_order_mirror_id == row.id)
            .order_by(FieldJobChatMessage.created_at.desc())
            .limit(safe_limit)
            .all()
        )
        messages.reverse()
        return {
            "available": bool(row.subscriber_id),
            "can_send": _can_send(row),
            "conversation_id": str(row.id),
            "customer_name": _customer_name(db, row),
            "messages": [_serialize(message) for message in messages],
        }

    @staticmethod
    def send_message(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
        *,
        body: str,
    ) -> dict:
        text = (body or "").strip()
        if not text:
            raise HTTPException(status_code=422, detail="Message body is required")
        profile = _profile_from_principal(db, principal)
        row = (
            _scoped_query(db, profile)
            .filter(WorkOrderMirror.crm_work_order_id == crm_work_order_id)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if not _can_send(row):
            raise HTTPException(
                status_code=409, detail="Field chat is not active for this job"
            )
        user = _system_user(db, profile)
        message = FieldJobChatMessage(
            work_order_mirror_id=row.id,
            crm_work_order_id=row.crm_work_order_id,
            direction="staff",
            body=text,
            author_technician_id=profile.id,
            author_person_id=profile.person_id,
            author_system_user_id=profile.system_user_id,
            author_name=_technician_name(profile, user),
        )
        db.add(message)
        db.commit()
        db.refresh(message)
        return _serialize(message)


field_job_chat = FieldJobChat()
