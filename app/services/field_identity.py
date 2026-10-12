"""Single owner for field identity submission, manual review and disclosure."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.field_identity import (
    FieldIdentityReceipt,
    FieldIdentityRevision,
    IdentityState,
)
from app.models.stored_file import StoredFile
from app.models.system_user import SystemUser
from app.services.audit_adapter import AuditActor, AuditRecord, audit_adapter
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.field_identity_contracts import (
    REVIEW_PERMISSION,
    WRITE_SCOPE,
    CustomerIdentityCard,
    IdentityAccess,
    IdentityDetail,
    IdentityOutcome,
    ReviewIdentity,
    SubmitIdentity,
)
from app.services.file_storage import PreparedFileUpload, file_uploads
from app.services.object_storage import StreamResult
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.permission_matching import permission_keys_allow

OWNER = "operations.field_identity"
CONCERN = "restricted field identity review lifecycle"
MAX_IDENTITY_FILE_BYTES = 5 * 1024 * 1024
MAX_PORTRAIT_PIXELS = 20_000_000
MAX_PORTRAIT_DIMENSION = 8192
ERROR_SUFFIXES = (
    "denied",
    "not_found",
    "stale_revision",
    "invalid_request",
    "idempotency_conflict",
)


class FieldIdentityError(DomainError):
    """Stable identity refusal."""


def _error(suffix: str) -> FieldIdentityError:
    return FieldIdentityError(
        code=f"{OWNER}.{suffix}", message=suffix.replace("_", " ")
    )


def _access(
    db: Session, access: IdentityAccess, subject_id: UUID, *, review: bool = False
) -> SystemUser:
    actor = db.get(SystemUser, access.system_user_id)
    subject = db.get(SystemUser, subject_id)
    if actor is None or not actor.is_active or subject is None or not subject.is_active:
        raise _error("denied")
    reviewer = permission_keys_allow(access.permission_keys, REVIEW_PERMISSION)
    if (review and (not reviewer or access.system_user_id == subject_id)) or (
        not review and access.system_user_id != subject_id and not reviewer
    ):
        raise _error("denied")
    return subject


def _latest(db: Session, subject_id: UUID) -> FieldIdentityRevision | None:
    return db.scalar(
        select(FieldIdentityRevision)
        .where(FieldIdentityRevision.subject_id == subject_id)
        .order_by(FieldIdentityRevision.revision.desc())
        .limit(1)
    )


def _outcome(row: FieldIdentityRevision) -> IdentityOutcome:
    return IdentityOutcome(
        row.subject_id, row.revision, row.version, IdentityState(row.state)
    )


def _context(context: CommandContext, access: IdentityAccess) -> None:
    if (
        context.scope != WRITE_SCOPE
        or context.actor != f"user:{access.system_user_id}"
        or not context.reason.strip()
        or not context.idempotency_key
        or len(context.idempotency_key) > 120
    ):
        raise _error("invalid_request")


def _fingerprint(values: tuple[str | int, ...]) -> str:
    return hashlib.sha256(
        json.dumps(values, separators=(",", ":")).encode()
    ).hexdigest()


def _replay(
    db: Session, subject_id: UUID, context: CommandContext, fingerprint: str
) -> IdentityOutcome | None:
    receipt = db.scalar(
        select(FieldIdentityReceipt).where(
            FieldIdentityReceipt.subject_id == subject_id,
            FieldIdentityReceipt.idempotency_key == context.idempotency_key,
        )
    )
    if receipt is None:
        return None
    if receipt.fingerprint != fingerprint:
        raise _error("idempotency_conflict")
    return IdentityOutcome(
        subject_id, receipt.revision, receipt.version, IdentityState(receipt.state)
    )


def _finish(
    db: Session, row: FieldIdentityRevision, context: CommandContext, fingerprint: str
) -> IdentityOutcome:
    db.add(
        FieldIdentityReceipt(
            subject_id=row.subject_id,
            idempotency_key=context.idempotency_key,
            fingerprint=fingerprint,
            revision=row.revision,
            version=row.version,
            state=row.state,
        )
    )
    details = {
        "subject_id": str(row.subject_id),
        "revision": row.revision,
        "version": row.version,
        "state": row.state,
        "command_id": str(context.command_id),
        "correlation_id": str(context.correlation_id),
        "reason": context.reason,
    }
    audit_adapter.stage(
        db,
        AuditRecord(
            action="field_identity.changed",
            entity_type="field_identity",
            entity_id=str(row.subject_id),
            actor=AuditActor.user(str(context.actor.removeprefix("user:"))),
            details=details,
            metadata=details,
        ),
    )
    emit_event(
        db,
        EventType.field_identity_changed,
        {
            "schema_version": 1,
            "subject_id": str(row.subject_id),
            "revision": row.revision,
            "version": row.version,
            "state": row.state,
            "command_id": str(context.command_id),
        },
        actor=context.actor,
    )
    db.flush()
    return _outcome(row)


def _prepared(upload: PreparedFileUpload, subject_id: UUID, *, portrait: bool) -> None:
    expected = "field_identity_portrait" if portrait else "field_identity_evidence"
    if (
        upload.entity_type != expected
        or upload.entity_id != str(subject_id)
        or upload.owner_subscriber_id is not None
        or upload.uploaded_by is not None
        or not upload.storage_key.startswith("attachments/")
        or not upload.checksum
    ):
        raise _error("invalid_request")
    allowed = (
        {"image/jpeg", "image/png", "image/webp"}
        if portrait
        else {"image/jpeg", "image/png", "image/webp", "application/pdf"}
    )
    if (
        upload.content_type not in allowed
        or upload.file_size <= 0
        or upload.file_size > 5 * 1024 * 1024
    ):
        raise _error("invalid_request")


def submit(db: Session, *, command: SubmitIdentity) -> IdentityOutcome:
    def operation() -> IdentityOutcome:
        _context(command.context, command.access)
        db.scalar(
            select(SystemUser)
            .where(SystemUser.id == command.subject_id)
            .with_for_update()
        )
        _access(db, command.access, command.subject_id)
        _prepared(command.portrait, command.subject_id, portrait=True)
        _prepared(command.evidence, command.subject_id, portrait=False)
        if (
            not command.display_name.strip()
            or len(command.display_name) > 120
            or not command.company_name.strip()
            or len(command.company_name) > 120
        ):
            raise _error("invalid_request")
        fingerprint = _fingerprint(
            (
                "submit",
                str(command.access.system_user_id),
                command.expected_revision,
                command.display_name,
                command.company_name,
                command.portrait.storage_key,
                command.evidence.storage_key,
            )
        )
        replay = _replay(db, command.subject_id, command.context, fingerprint)
        if replay:
            return replay
        previous = _latest(db, command.subject_id)
        if (previous.revision if previous else 0) != command.expected_revision:
            raise _error("stale_revision")
        portrait = file_uploads.stage_prepared_upload(db=db, prepared=command.portrait)
        evidence = file_uploads.stage_prepared_upload(db=db, prepared=command.evidence)
        row = FieldIdentityRevision(
            subject_id=command.subject_id,
            revision=command.expected_revision + 1,
            version=1,
            state=IdentityState.pending.value,
            display_name=command.display_name.strip(),
            company_name=command.company_name.strip(),
            portrait_file_id=portrait.id,
            evidence_file_id=evidence.id,
        )
        db.add(row)
        return _finish(db, row, command.context, fingerprint)

    return execute_owner_command(
        db,
        definition=OwnerCommandDefinition(OWNER, CONCERN, "submit"),
        context=command.context,
        operation=operation,
    )


def review(db: Session, *, command: ReviewIdentity) -> IdentityOutcome:
    def operation() -> IdentityOutcome:
        _context(command.context, command.access)
        db.scalar(
            select(SystemUser)
            .where(SystemUser.id == command.subject_id)
            .with_for_update()
        )
        _access(db, command.access, command.subject_id, review=True)
        fingerprint = _fingerprint(
            (
                "review",
                str(command.access.system_user_id),
                command.revision,
                command.expected_version,
                command.decision.value,
                command.context.reason,
            )
        )
        replay = _replay(db, command.subject_id, command.context, fingerprint)
        if replay:
            return replay
        row = _latest(db, command.subject_id)
        if row is None:
            raise _error("not_found")
        if row.revision != command.revision or row.version != command.expected_version:
            raise _error("stale_revision")
        allowed = (
            {IdentityState.reviewed, IdentityState.rejected}
            if row.state == IdentityState.pending
            else {IdentityState.revoked}
            if row.state == IdentityState.reviewed
            else set()
        )
        if command.decision not in allowed:
            raise _error("invalid_request")
        row.state = command.decision.value
        row.version += 1
        row.reviewer_id = command.access.system_user_id
        row.reviewed_at = datetime.now(UTC)
        row.review_reason = command.context.reason
        return _finish(db, row, command.context, fingerprint)

    return execute_owner_command(
        db,
        definition=OwnerCommandDefinition(OWNER, CONCERN, "review"),
        context=command.context,
        operation=operation,
    )


def detail(db: Session, *, access: IdentityAccess, subject_id: UUID) -> IdentityDetail:
    _access(db, access, subject_id)
    row = _latest(db, subject_id)
    if row is None:
        raise _error("not_found")
    return IdentityDetail(
        _outcome(row),
        row.display_name,
        row.company_name,
        row.portrait_file_id,
        row.evidence_file_id,
        row.review_reason,
    )


def private_file(
    db: Session, *, access: IdentityAccess, subject_id: UUID, file_id: UUID
) -> StoredFile:
    info = detail(db, access=access, subject_id=subject_id)
    if file_id not in (info.portrait_file_id, info.evidence_file_id):
        raise _error("denied")
    row = db.get(StoredFile, file_id)
    if (
        row is None
        or row.is_deleted
        or row.entity_id != str(subject_id)
        or row.entity_type not in {"field_identity_portrait", "field_identity_evidence"}
    ):
        raise _error("not_found")
    return row


def approved_customer_card(
    db: Session, *, subject_id: UUID
) -> CustomerIdentityCard | None:
    """Internal projection input; caller must recheck current customer assignment."""
    subject = db.get(SystemUser, subject_id)
    row = _latest(db, subject_id)
    if (
        subject is None
        or not subject.is_active
        or row is None
        or row.state != IdentityState.reviewed
        or row.reviewed_at is None
    ):
        return None
    portrait = db.get(StoredFile, row.portrait_file_id)
    if (
        portrait is None
        or portrait.is_deleted
        or portrait.entity_type != "field_identity_portrait"
        or portrait.entity_id != str(subject_id)
    ):
        return None
    return CustomerIdentityCard(
        subject_id,
        row.revision,
        row.display_name,
        row.company_name,
        True,
        IdentityState.reviewed,
        row.reviewed_at,
        row.portrait_file_id,
    )


def approved_portrait(db: Session, *, subject_id: UUID, revision: int) -> StoredFile:
    """Internal assignment-gated adapter input; never exposes verification evidence."""
    card = approved_customer_card(db, subject_id=subject_id)
    if card is None or card.revision != revision:
        raise _error("denied")
    row = db.get(StoredFile, card.portrait_file_id)
    if row is None:
        raise _error("not_found")
    return row


@dataclass(frozen=True, slots=True)
class NormalizedIdentityPortrait:
    data: bytes
    content_type: str = "image/png"
    filename: str = "portrait.png"


def normalize_identity_portrait(
    *, data: bytes, content_type: str | None
) -> NormalizedIdentityPortrait:
    """Decode oriented pixels into a fresh PNG; never copy source metadata."""
    from io import BytesIO

    from PIL import Image, ImageOps, UnidentifiedImageError

    formats = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
    if not data or len(data) > MAX_IDENTITY_FILE_BYTES or content_type not in formats:
        raise _error("invalid_request")
    try:
        with Image.open(BytesIO(data)) as source:
            if (
                source.format != formats[content_type]
                or source.width * source.height > MAX_PORTRAIT_PIXELS
                or max(source.size) > MAX_PORTRAIT_DIMENSION
                or getattr(source, "n_frames", 1) != 1
            ):
                raise _error("invalid_request")
            source.verify()
        with Image.open(BytesIO(data)) as source:
            source.load()
            oriented = ImageOps.exif_transpose(source)
            mode = (
                "RGBA"
                if "A" in oriented.getbands() or "transparency" in oriented.info
                else "RGB"
            )
            # A fresh raster has no EXIF, GPS, XMP, ICC, comments, thumbnails,
            # PNG text chunks, or container/trailing payload from the source.
            clean = Image.new(mode, oriented.size)
            clean.paste(oriented.convert(mode))
            output = BytesIO()
            clean.save(output, format="PNG")
            normalized = output.getvalue()
        if len(normalized) > MAX_IDENTITY_FILE_BYTES:
            raise _error("invalid_request")
        return NormalizedIdentityPortrait(normalized)
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise _error("invalid_request") from exc


def approved_portrait_stream(
    db: Session, *, subject_id: UUID, revision: int
) -> StreamResult:
    """Assignment-gated callers receive sanitized pixels, including legacy files."""
    file = approved_portrait(db, subject_id=subject_id, revision=revision)
    stream = file_uploads.stream_file(file)
    data = bytearray()
    try:
        if (
            stream.content_length is not None
            and stream.content_length > MAX_IDENTITY_FILE_BYTES
        ):
            raise _error("invalid_request")
        for chunk in stream.chunks:
            if len(data) + len(chunk) > MAX_IDENTITY_FILE_BYTES:
                raise _error("invalid_request")
            data.extend(chunk)
    finally:
        close = getattr(stream.chunks, "close", None)
        if callable(close):
            close()
    portrait = normalize_identity_portrait(
        data=bytes(data), content_type=file.content_type
    )
    return StreamResult(
        chunks=iter((portrait.data,)),
        content_type=portrait.content_type,
        content_length=len(portrait.data),
    )


def authorize_upload(db: Session, *, access: IdentityAccess, subject_id: UUID) -> None:
    """Admission before uploading private bytes; command repeats it under lock."""
    _access(db, access, subject_id)


def prepare_identity_upload(
    *,
    subject_id: UUID,
    portrait: bool,
    filename: str,
    content_type: str | None,
    data: bytes,
) -> PreparedFileUpload:
    """Validate restricted bytes before sending them to private object storage."""
    from io import BytesIO

    from PIL import Image, UnidentifiedImageError

    allowed = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
    if not data or len(data) > 5 * 1024 * 1024:
        raise _error("invalid_request")
    if portrait:
        normalized = normalize_identity_portrait(data=data, content_type=content_type)
        data, content_type, filename = (
            normalized.data,
            normalized.content_type,
            normalized.filename,
        )
    elif content_type is not None and content_type in allowed:
        try:
            with Image.open(BytesIO(data)) as image:
                if (
                    image.format != allowed[content_type]
                    or image.width * image.height > 20_000_000
                ):
                    raise _error("invalid_request")
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise _error("invalid_request") from exc
    elif portrait or content_type != "application/pdf" or not data.startswith(b"%PDF-"):
        raise _error("invalid_request")
    return file_uploads.prepare_upload(
        domain="attachments",
        entity_type="field_identity_portrait"
        if portrait
        else "field_identity_evidence",
        entity_id=str(subject_id),
        original_filename=filename,
        content_type=content_type,
        data=data,
        uploaded_by=None,
    )
