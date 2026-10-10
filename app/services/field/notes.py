"""Native field notes for imported work-order mirrors."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.field_note import FieldWorkOrderNote
from app.schemas.field import FieldNoteRead
from app.services.field.execution_contracts import FieldJobQuery
from app.services.field.work_order_access import (
    FieldWorkOrderScope,
    ResolveFieldActor,
    require_work_order,
    resolve_field_actor,
)


def _serialize(note: FieldWorkOrderNote) -> dict:
    attachments = [
        _serialize_attachment(attachment)
        for attachment in getattr(note, "attachments_", []) or []
        if attachment.is_active
    ]
    return {
        "id": note.id,
        "client_ref": note.client_ref,
        "body": note.body,
        "is_internal": note.is_internal,
        "author_person_id": note.author_person_id,
        "author_name": note.author_name,
        "created_at": note.created_at,
        "attachments": attachments,
    }


class FieldNotes:
    @staticmethod
    def list_for_job(db: Session, query: FieldJobQuery) -> tuple[FieldNoteRead, ...]:
        actor = resolve_field_actor(
            db, ResolveFieldActor(query.requester_system_user_id)
        )
        row = require_work_order(db, FieldWorkOrderScope(actor, query.public_id))
        notes = (
            db.query(FieldWorkOrderNote)
            .filter(FieldWorkOrderNote.work_order_mirror_id == row.id)
            .order_by(FieldWorkOrderNote.created_at.asc())
            .all()
        )
        return tuple(FieldNoteRead.model_validate(_serialize(note)) for note in notes)


field_notes = FieldNotes()


def _serialize_attachment(attachment) -> dict:
    from app.services.field.attachments import serialize_attachment

    return serialize_attachment(attachment)
