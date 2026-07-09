"""Native field notes for CRM-synced work-order mirrors."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.field_note import FieldWorkOrderNote
from app.models.work_order_mirror import WorkOrderMirror
from app.services.field.jobs import (
    _profile_from_principal,
    _scoped_query,
    _system_user,
    _technician_name,
)


def _serialize(note: FieldWorkOrderNote) -> dict:
    return {
        "id": note.id,
        "body": note.body,
        "is_internal": note.is_internal,
        "author_person_id": note.author_person_id,
        "author_name": note.author_name,
        "created_at": note.created_at,
        "attachments": note.attachments or [],
    }


class FieldNotes:
    @staticmethod
    def list_for_job(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
    ) -> list[dict]:
        row = _scoped_work_order(db, principal, crm_work_order_id)
        notes = (
            db.query(FieldWorkOrderNote)
            .filter(FieldWorkOrderNote.work_order_mirror_id == row.id)
            .order_by(FieldWorkOrderNote.created_at.asc())
            .all()
        )
        return [_serialize(note) for note in notes]

    @staticmethod
    def create(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
        *,
        body: str,
        is_internal: bool = True,
        attachment_ids: list[str] | None = None,
    ) -> dict:
        body = (body or "").strip()
        if not body:
            raise HTTPException(status_code=422, detail="Note body is required")
        if attachment_ids:
            raise HTTPException(
                status_code=422,
                detail="Field note attachments are not available in sub yet",
            )

        profile = _profile_from_principal(db, principal)
        row = (
            _scoped_query(db, profile)
            .filter(WorkOrderMirror.crm_work_order_id == crm_work_order_id)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")

        user = _system_user(db, profile)
        note = FieldWorkOrderNote(
            work_order_mirror_id=row.id,
            crm_work_order_id=row.crm_work_order_id,
            author_technician_id=profile.id,
            author_person_id=profile.person_id,
            author_system_user_id=profile.system_user_id,
            author_name=_technician_name(profile, user),
            body=body,
            is_internal=is_internal,
            attachments=[],
        )
        db.add(note)
        db.commit()
        db.refresh(note)
        return _serialize(note)


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


field_notes = FieldNotes()
