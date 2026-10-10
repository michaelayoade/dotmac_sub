"""Native field attachment metadata and private file access."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.field_attachment import FIELD_ATTACHMENT_KINDS, FieldAttachment
from app.models.stored_file import StoredFile
from app.models.work_order import WorkOrder
from app.schemas.field import FieldAttachmentRead
from app.services.domain_errors import DomainError
from app.services.events.owner_outputs import OwnerOutputEnvelope, stage_owner_output
from app.services.events.types import EventType
from app.services.field.execution_contracts import (
    CreateFieldAttachment,
    DeleteFieldAttachment,
    FieldAttachmentIdentity,
    FieldAttachmentQuery,
)
from app.services.field.work_order_access import (
    FieldAccessError,
    FieldActor,
    FieldWorkOrderScope,
    ResolveFieldActor,
    require_work_order,
    resolve_field_actor,
)
from app.services.file_storage import FileValidationError, file_uploads
from app.services.object_storage import ObjectNotFoundError, StreamResult
from app.services.owner_commands import (
    OwnerCommandDefinition,
    execute_owner_command,
    owner_command_active,
)


@dataclass(frozen=True, slots=True)
class StageExpenseReceiptAttachment:
    work_order_id: UUID
    work_order_public_id: str
    uploaded_by_person_id: UUID
    uploaded_by_system_user_id: UUID
    uploaded_by_technician_id: UUID | None
    file_name: str
    mime_type: str | None
    content: bytes
    client_ref: UUID


@dataclass(frozen=True, slots=True)
class ExpenseReceiptAttachmentOutcome:
    id: UUID


SUPPORTED_EXPENSE_RECEIPT_MIME_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "application/pdf",
    }
)
MAX_EXPENSE_RECEIPT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ResolvedExpenseReceiptAttachment:
    attachment_id: UUID
    work_order_id: UUID
    file_name: str
    mime_type: str
    size_bytes: int
    checksum_sha256: str
    content: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ExpenseReceiptFileIdentity:
    """Canonical receipt name and MIME derived from the stored bytes."""

    file_name: str
    mime_type: str


def resolve_expense_receipt_file_identity(
    *, file_name: str, content: bytes
) -> ExpenseReceiptFileIdentity:
    """Return an ERP-safe file identity from receipt magic bytes."""

    if content.startswith(b"\xff\xd8\xff"):
        extension, mime_type = ".jpg", "image/jpeg"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        extension, mime_type = ".png", "image/png"
    elif content.startswith((b"GIF87a", b"GIF89a")):
        extension, mime_type = ".gif", "image/gif"
    elif (
        len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    ):
        extension, mime_type = ".webp", "image/webp"
    elif content.startswith(b"%PDF"):
        extension, mime_type = ".pdf", "application/pdf"
    else:
        raise _expense_receipt_error("Receipt file type is unsupported")

    stem = Path(file_name).stem.strip() or "receipt"
    return ExpenseReceiptFileIdentity(
        file_name=f"{stem}{extension}",
        mime_type=mime_type,
    )


def resolve_expense_receipt_attachment(
    db: Session,
    *,
    work_order_id: UUID,
    attachment_id: UUID,
    allowed_owner_ids: frozenset[UUID],
) -> ResolvedExpenseReceiptAttachment:
    """Resolve and verify private receipt bytes for the expense owner/worker."""
    attachment = db.get(FieldAttachment, attachment_id)
    if (
        attachment is None
        or not attachment.is_active
        or attachment.kind != "document"
        or attachment.work_order_mirror_id != work_order_id
    ):
        raise _expense_receipt_error("Receipt attachment is unavailable")
    attachment_owner_ids = frozenset(
        value
        for value in (
            attachment.uploaded_by_system_user_id,
            attachment.uploaded_by_person_id,
            attachment.uploaded_by_technician_id,
        )
        if value is not None
    )
    if not attachment_owner_ids.intersection(allowed_owner_ids):
        raise _expense_receipt_error("Receipt attachment ownership is invalid")
    if (
        not attachment.file_name
        or Path(attachment.file_name).name != attachment.file_name
        or "\x00" in attachment.file_name
    ):
        raise _expense_receipt_error("Receipt filename is invalid")
    if attachment.mime_type not in SUPPORTED_EXPENSE_RECEIPT_MIME_TYPES:
        raise _expense_receipt_error("Receipt file type is unsupported")
    if not 0 < attachment.size_bytes <= MAX_EXPENSE_RECEIPT_BYTES:
        raise _expense_receipt_error("Receipt file size is invalid")
    stored = db.get(StoredFile, attachment.stored_file_id)
    if (
        stored is None
        or stored.is_deleted
        or stored.entity_type != "field_attachment"
        or stored.entity_id != attachment.work_order_mirror.public_id
        or stored.file_size != attachment.size_bytes
    ):
        raise _expense_receipt_error("Receipt storage evidence is invalid")
    try:
        stream = file_uploads.stream_file(stored)
        content = b"".join(stream.chunks)
    except ObjectNotFoundError as exc:
        raise _expense_receipt_error("Receipt content is unavailable") from exc
    if (
        len(content) != attachment.size_bytes
        or len(content) > MAX_EXPENSE_RECEIPT_BYTES
    ):
        raise _expense_receipt_error("Receipt checksum evidence is invalid")
    checksum = hashlib.sha256(content).hexdigest()
    if stored.checksum and stored.checksum.casefold() != checksum:
        raise _expense_receipt_error("Receipt checksum evidence is inconsistent")
    identity = resolve_expense_receipt_file_identity(
        file_name=attachment.file_name,
        content=content,
    )
    return ResolvedExpenseReceiptAttachment(
        attachment_id=attachment.id,
        work_order_id=attachment.work_order_mirror_id,
        file_name=identity.file_name,
        mime_type=identity.mime_type,
        size_bytes=len(content),
        checksum_sha256=checksum,
        content=content,
    )


def stage_expense_receipt_attachment(
    db: Session, command: StageExpenseReceiptAttachment
) -> ExpenseReceiptAttachmentOutcome:
    """Stage one receipt inside the expense owner's active transaction."""

    if not owner_command_active(db, owner="operations.expense_requests"):
        raise RuntimeError("Expense receipts require the expense request owner")
    work_order = db.get(WorkOrder, command.work_order_id)
    if (
        work_order is None
        or not work_order.is_active
        or work_order.public_id != command.work_order_public_id
    ):
        raise _expense_receipt_error("Work order not found")
    if not command.content:
        raise _expense_receipt_error("Receipt file is empty")
    identity = resolve_expense_receipt_file_identity(
        file_name=command.file_name or "receipt",
        content=command.content,
    )
    existing = (
        db.query(FieldAttachment)
        .filter(FieldAttachment.client_ref == command.client_ref)
        .filter(
            FieldAttachment.uploaded_by_system_user_id
            == command.uploaded_by_system_user_id
        )
        .one_or_none()
    )
    if existing is not None:
        if existing.work_order_mirror_id != work_order.id:
            raise _expense_receipt_error("Receipt identity conflict")
        return ExpenseReceiptAttachmentOutcome(id=existing.id)

    try:
        stored = file_uploads.stage_upload(
            db=db,
            domain="attachments",
            entity_type="field_attachment",
            entity_id=work_order.public_id,
            original_filename=identity.file_name,
            content_type=identity.mime_type,
            data=command.content,
            # StoredFile.uploaded_by is a legacy subscriber-only foreign key.
            # Staff provenance belongs on the FieldAttachment fields below.
            uploaded_by=None,
            owner_subscriber_id=None,
        )
    except FileValidationError as exc:
        raise _expense_receipt_error(str(exc)) from exc
    attachment = FieldAttachment(
        work_order_mirror_id=work_order.id,
        stored_file_id=stored.id,
        kind="document",
        file_name=stored.original_filename,
        mime_type=stored.content_type
        or command.mime_type
        or "application/octet-stream",
        size_bytes=stored.file_size,
        uploaded_by_technician_id=command.uploaded_by_technician_id,
        uploaded_by_person_id=command.uploaded_by_person_id,
        uploaded_by_system_user_id=command.uploaded_by_system_user_id,
        client_ref=command.client_ref,
    )
    db.add(attachment)
    db.flush()
    return ExpenseReceiptAttachmentOutcome(id=attachment.id)


def _expense_receipt_error(message: str) -> DomainError:
    from app.services.field.expense_requests import FieldExpenseRequestError

    return FieldExpenseRequestError(
        code="operations.expense_requests.invalid_request",
        message=message,
    )


def _download_path(attachment_id: UUID) -> str:
    return f"/api/v1/field/attachments/{attachment_id}/content"


def serialize_attachment(attachment: FieldAttachment) -> dict:
    return {
        "id": attachment.id,
        "work_order_id": attachment.work_order_mirror.public_id,
        "note_id": attachment.note_id,
        "kind": attachment.kind,
        "file_name": attachment.file_name,
        "mime_type": attachment.mime_type,
        "size_bytes": attachment.size_bytes,
        "latitude": attachment.latitude,
        "longitude": attachment.longitude,
        "captured_at": attachment.captured_at,
        "signer_name": attachment.signer_name,
        "uploaded_by_person_id": attachment.uploaded_by_person_id,
        "uploaded_by_system_user_id": attachment.uploaded_by_system_user_id,
        "client_ref": attachment.client_ref,
        "asset_type": attachment.asset_type,
        "asset_id": attachment.asset_id,
        "created_at": attachment.created_at,
        "download_path": _download_path(attachment.id),
    }


class FieldAttachments:
    @staticmethod
    def create(db: Session, command: CreateFieldAttachment) -> FieldAttachmentRead:
        def operation() -> FieldAttachmentRead:
            return _create_attachment(db, command)

        return execute_owner_command(
            db,
            definition=OwnerCommandDefinition(
                owner="operations.field_attachments",
                concern="native field attachment creation",
                name="create_field_attachment",
            ),
            context=command.context,
            operation=operation,
        )

    @staticmethod
    def list(
        db: Session, query: FieldAttachmentQuery
    ) -> tuple[FieldAttachmentRead, ...]:
        actor = resolve_field_actor(
            db, ResolveFieldActor(query.requester_system_user_id)
        )
        row = _resolve_work_order(db, actor, query.public_id, query.note_id)
        statement = db.query(FieldAttachment).filter(
            FieldAttachment.work_order_mirror_id == row.id,
            FieldAttachment.is_active.is_(True),
        )
        if query.note_id is not None:
            statement = statement.filter(FieldAttachment.note_id == query.note_id)
        if query.kind:
            statement = statement.filter(
                FieldAttachment.kind == query.kind.strip().lower()
            )
        return tuple(
            FieldAttachmentRead.model_validate(serialize_attachment(item))
            for item in statement.order_by(FieldAttachment.created_at.desc())
            .offset(query.offset)
            .limit(query.limit)
            .all()
        )

    @staticmethod
    def get(db: Session, query: FieldAttachmentIdentity) -> FieldAttachmentRead:
        return FieldAttachmentRead.model_validate(
            serialize_attachment(_get_attachment(db, query))
        )

    @staticmethod
    def get_content(
        db: Session, query: FieldAttachmentIdentity
    ) -> tuple[FieldAttachmentRead, StreamResult]:
        attachment = _get_attachment(db, query)
        stored = db.get(StoredFile, attachment.stored_file_id)
        if stored is None or stored.is_deleted:
            raise FieldAccessError(
                code="operations.field_attachments.not_found",
                message="Attachment content not found",
                retryable=False,
            )
        try:
            return FieldAttachmentRead.model_validate(
                serialize_attachment(attachment)
            ), file_uploads.stream_file(stored)
        except ObjectNotFoundError as exc:
            raise FieldAccessError(
                code="operations.field_attachments.not_found",
                message="Attachment content not found",
                retryable=False,
            ) from exc

    @staticmethod
    def delete(
        db: Session, command: DeleteFieldAttachment
    ) -> FieldAttachmentDeletionOutcome:
        def operation() -> FieldAttachmentDeletionOutcome:
            from app.models.system_user import SystemUser

            db.query(SystemUser).filter(
                SystemUser.id == command.requester_system_user_id
            ).with_for_update().one_or_none()
            actor = resolve_field_actor(
                db, ResolveFieldActor(command.requester_system_user_id)
            )
            attachment = db.get(FieldAttachment, command.attachment_id)
            if attachment is None:
                raise FieldAccessError(
                    code="operations.field_attachments.not_found",
                    message="Attachment not found",
                    retryable=False,
                )
            _resolve_work_order(
                db,
                actor,
                None,
                None,
                work_order_mirror_id=attachment.work_order_mirror_id,
                lock=True,
            )
            if not attachment.is_active:
                return FieldAttachmentDeletionOutcome(attachment.id, replayed=True)
            attachment.is_active = False
            stored = db.get(StoredFile, attachment.stored_file_id)
            if stored is not None and not stored.is_deleted:
                file_uploads.stage_soft_delete(db=db, file=stored)
            db.flush()
            stage_owner_output(
                db,
                OwnerOutputEnvelope(
                    event_type=EventType.field_attachment_deleted,
                    producer_owner="operations.field_attachments",
                    source_kind="field_attachment",
                    source_id=attachment.id,
                ),
                {
                    "attachment_id": str(attachment.id),
                    "work_order_id": str(attachment.work_order_mirror_id),
                    "system_user_id": str(actor.system_user_id),
                },
                context=command.context,
            )
            return FieldAttachmentDeletionOutcome(attachment.id, replayed=False)

        return execute_owner_command(
            db,
            definition=OwnerCommandDefinition(
                owner="operations.field_attachments",
                concern="native field attachment deletion",
                name="delete_field_attachment",
            ),
            context=command.context,
            operation=operation,
        )


@dataclass(frozen=True, slots=True)
class FieldAttachmentDeletionOutcome:
    attachment_id: UUID
    replayed: bool


def _get_attachment(db: Session, query: FieldAttachmentIdentity) -> FieldAttachment:
    attachment = db.get(FieldAttachment, query.attachment_id)
    if attachment is None or not attachment.is_active:
        raise FieldAccessError(
            code="operations.field_attachments.not_found",
            message="Attachment not found",
            retryable=False,
        )
    actor = resolve_field_actor(db, ResolveFieldActor(query.requester_system_user_id))
    _resolve_work_order(
        db, actor, None, None, work_order_mirror_id=attachment.work_order_mirror_id
    )
    return attachment


def _create_attachment(
    db: Session, command: CreateFieldAttachment
) -> FieldAttachmentRead:
    from app.models.system_user import SystemUser

    db.query(SystemUser).filter(
        SystemUser.id == command.requester_system_user_id
    ).with_for_update().one_or_none()
    kind, file_name, mime_type, content = (
        command.kind,
        command.file_name,
        command.mime_type,
        command.content,
    )
    client_ref, crm_work_order_id, note_id = (
        command.client_ref,
        command.public_id,
        command.note_id,
    )
    latitude, longitude, captured_at, signer_name = (
        command.latitude,
        command.longitude,
        command.captured_at,
        command.signer_name,
    )
    asset_type, asset_id = command.asset_type, command.asset_id
    normalized_kind = kind.strip().lower()
    if normalized_kind not in FIELD_ATTACHMENT_KINDS:
        raise FieldAccessError(
            code="operations.field_work_order_access.invalid_request",
            message=f"Unsupported attachment kind: {kind}",
        )
    if not content:
        raise FieldAccessError(
            code="operations.field_work_order_access.invalid_request",
            message="Empty file",
        )
    if not crm_work_order_id and note_id is None:
        raise FieldAccessError(
            code="operations.field_work_order_access.invalid_request",
            message="Attachment must reference a job or note",
        )
    if (asset_type and asset_id is None) or (asset_id and not asset_type):
        raise FieldAccessError(
            code="operations.field_work_order_access.invalid_request",
            message="asset_type and asset_id must be provided together",
        )

    profile = resolve_field_actor(
        db, ResolveFieldActor(command.requester_system_user_id)
    )
    row = _resolve_work_order(db, profile, crm_work_order_id, note_id, lock=True)
    if client_ref:
        existing = (
            db.query(FieldAttachment)
            .filter(FieldAttachment.client_ref == client_ref)
            .one_or_none()
        )
        if existing is not None:
            stored = db.get(StoredFile, existing.stored_file_id)
            safe_name, final_mime = file_uploads.validate(
                config=file_uploads.get_domain_config("attachments"),
                filename=file_name or "upload",
                content_type=mime_type,
                data=content,
            )
            if (
                existing.work_order_mirror_id != row.id
                or existing.uploaded_by_system_user_id != profile.system_user_id
                or existing.uploaded_by_vendor_user_id != profile.vendor_user_id
                or not existing.is_active
                or stored is None
                or stored.checksum != hashlib.sha256(content).hexdigest()
                or existing.kind != normalized_kind
                or existing.note_id != note_id
                or existing.file_name != safe_name
                or existing.mime_type != final_mime
                or existing.latitude != latitude
                or existing.longitude != longitude
                or existing.signer_name != signer_name
                or existing.asset_type != asset_type
                or existing.asset_id != asset_id
                or (
                    existing.captured_at.replace(tzinfo=UTC)
                    if existing.captured_at and existing.captured_at.tzinfo is None
                    else existing.captured_at
                )
                != (
                    captured_at.replace(tzinfo=UTC)
                    if captured_at and captured_at.tzinfo is None
                    else captured_at
                )
            ):
                raise FieldAccessError(
                    code="operations.field_attachments.idempotency_conflict",
                    message="Attachment identity was reused with different details",
                    retryable=False,
                )
            return FieldAttachmentRead.model_validate(serialize_attachment(existing))

    try:
        stored = file_uploads.stage_upload(
            db=db,
            domain="attachments",
            entity_type="field_attachment",
            entity_id=row.public_id,
            original_filename=file_name or "upload",
            content_type=mime_type,
            data=content,
            uploaded_by=None,
            owner_subscriber_id=None,
        )
    except FileValidationError as exc:
        raise FieldAccessError(
            code="operations.field_work_order_access.invalid_request", message=str(exc)
        ) from exc

    attachment = FieldAttachment(
        work_order_mirror_id=row.id,
        note_id=note_id,
        stored_file_id=stored.id,
        kind=normalized_kind,
        file_name=stored.original_filename,
        mime_type=stored.content_type or mime_type or "application/octet-stream",
        size_bytes=stored.file_size,
        latitude=latitude,
        longitude=longitude,
        captured_at=captured_at,
        signer_name=signer_name,
        uploaded_by_technician_id=profile.technician_id,
        uploaded_by_vendor_user_id=profile.vendor_user_id,
        uploaded_by_person_id=profile.person_id,
        uploaded_by_system_user_id=profile.system_user_id,
        client_ref=client_ref,
        asset_type=asset_type,
        asset_id=asset_id,
    )
    db.add(attachment)
    db.flush()
    stage_owner_output(
        db,
        OwnerOutputEnvelope(
            event_type=EventType.field_attachment_created,
            producer_owner="operations.field_attachments",
            source_kind="field_attachment",
            source_id=attachment.id,
        ),
        {
            "attachment_id": str(attachment.id),
            "work_order_id": str(row.id),
            "work_order_public_id": row.public_id,
            "system_user_id": str(profile.system_user_id),
            "kind": attachment.kind,
        },
        context=command.context,
    )
    return FieldAttachmentRead.model_validate(serialize_attachment(attachment))


def _resolve_work_order(
    db: Session,
    profile: FieldActor,
    crm_work_order_id: str | None,
    note_id: UUID | None,
    work_order_mirror_id: UUID | None = None,
    *,
    lock: bool = False,
) -> WorkOrder:
    from app.models.field_note import FieldWorkOrderNote

    query = db.query(WorkOrder)
    if work_order_mirror_id is not None:
        query = query.filter(WorkOrder.id == work_order_mirror_id)
    elif note_id is not None:
        note = db.get(FieldWorkOrderNote, note_id)
        if note is None:
            raise FieldAccessError(
                code="operations.field_work_order_access.not_found",
                message="Note not found",
            )
        query = query.filter(WorkOrder.id == note.work_order_mirror_id)
    elif crm_work_order_id:
        query = query.filter(WorkOrder.public_id == crm_work_order_id)
    else:
        raise FieldAccessError(
            code="operations.field_work_order_access.invalid_request",
            message="crm_work_order_id is required",
        )

    row = query.one_or_none()
    if row is None:
        raise FieldAccessError(
            code="operations.field_work_order_access.not_found", message="Job not found"
        )
    return require_work_order(
        db, FieldWorkOrderScope(profile, row.public_id, lock=lock)
    )


def parse_captured_at(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FieldAccessError(
            code="operations.field_work_order_access.invalid_request",
            message="Invalid captured_at timestamp",
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


field_attachments = FieldAttachments()
