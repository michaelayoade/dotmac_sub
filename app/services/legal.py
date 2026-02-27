"""Service layer for legal document management."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import nh3
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.models.legal import LegalDocument, LegalDocumentType
from app.models.stored_file import StoredFile
from app.schemas.legal import LegalDocumentCreate, LegalDocumentUpdate
from app.services.file_storage import file_uploads
from app.services.object_storage import ObjectNotFoundError, StreamResult

logger = logging.getLogger(__name__)
UPLOAD_DIR = "uploads/legal"
ALLOWED_HTML_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "ul",
    "ol",
    "li",
    "strong",
    "em",
    "a",
    "blockquote",
    "br",
    "span",
    "div",
}
ALLOWED_HTML_ATTRIBUTES = {"a": ["href"]}


class LegalDocumentService:
    """Service for managing legal documents."""

    def list(
        self,
        db: Session,
        document_type: LegalDocumentType | None = None,
        is_published: bool | None = None,
        is_current: bool | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 100,
        offset: int = 0,
    ) -> list[LegalDocument]:
        """List legal documents with filters."""
        query = db.query(LegalDocument)

        if document_type:
            query = query.filter(LegalDocument.document_type == document_type)
        if is_published is not None:
            query = query.filter(LegalDocument.is_published == is_published)
        if is_current is not None:
            query = query.filter(LegalDocument.is_current == is_current)

        order_col = getattr(LegalDocument, order_by, LegalDocument.created_at)
        if order_dir == "desc":
            query = query.order_by(order_col.desc())
        else:
            query = query.order_by(order_col.asc())

        return query.offset(offset).limit(limit).all()

    def get_list_stats(
        self,
        db: Session,
        *,
        document_type: LegalDocumentType | None = None,
        is_published: bool | None = None,
    ) -> dict[str, int]:
        """Return total/published/draft counts for list page filters."""
        count_query = db.query(func.count(LegalDocument.id))
        if document_type is not None:
            count_query = count_query.filter(LegalDocument.document_type == document_type)
        if is_published is not None:
            count_query = count_query.filter(LegalDocument.is_published == is_published)
        total = count_query.scalar() or 0

        published_query = db.query(func.count(LegalDocument.id)).filter(
            LegalDocument.is_published.is_(True)
        )
        draft_query = db.query(func.count(LegalDocument.id)).filter(
            LegalDocument.is_published.is_(False)
        )
        if document_type is not None:
            published_query = published_query.filter(
                LegalDocument.document_type == document_type
            )
            draft_query = draft_query.filter(LegalDocument.document_type == document_type)

        return {
            "total": total,
            "published": published_query.scalar() or 0,
            "draft": draft_query.scalar() or 0,
        }

    def get(self, db: Session, document_id: str) -> LegalDocument | None:
        return db.get(LegalDocument, document_id)

    def get_by_slug(self, db: Session, slug: str) -> LegalDocument | None:
        return db.query(LegalDocument).filter(LegalDocument.slug == slug).first()

    def get_current_by_type(
        self, db: Session, document_type: LegalDocumentType
    ) -> LegalDocument | None:
        return (
            db.query(LegalDocument)
            .filter(
                and_(
                    LegalDocument.document_type == document_type,
                    LegalDocument.is_current == True,
                    LegalDocument.is_published == True,
                )
            )
            .first()
        )

    def create(self, db: Session, payload: LegalDocumentCreate) -> LegalDocument:
        clean_content = self._sanitize_content(payload.content)
        document = LegalDocument(
            document_type=payload.document_type,
            title=payload.title,
            slug=payload.slug,
            version=payload.version,
            summary=payload.summary,
            content=clean_content,
            is_published=payload.is_published,
            effective_date=payload.effective_date,
        )

        if payload.is_published:
            document.published_at = datetime.now(UTC)
            self._set_as_current(db, document)

        db.add(document)
        db.commit()
        db.refresh(document)
        return document

    def update(
        self, db: Session, document_id: str, payload: LegalDocumentUpdate
    ) -> LegalDocument | None:
        document = self.get(db, document_id)
        if not document:
            return None

        update_data = payload.model_dump(exclude_unset=True)
        if "content" in update_data and update_data["content"] is not None:
            update_data["content"] = self._sanitize_content(update_data["content"])
        if "is_published" in update_data and update_data["is_published"]:
            if not document.is_published:
                update_data["published_at"] = datetime.now(UTC)

        for field, value in update_data.items():
            setattr(document, field, value)

        if update_data.get("is_current"):
            self._set_as_current(db, document)

        db.commit()
        db.refresh(document)
        return document

    def delete(self, db: Session, document_id: str) -> bool:
        document = self.get(db, document_id)
        if not document:
            return False

        record = self.get_active_file_record(db, document_id)
        if record:
            file_uploads.soft_delete(db=db, file=record, hard_delete_object=True)
        elif document.file_path:
            self._delete_legacy_local_file(document.file_path)

        db.delete(document)
        db.commit()
        return True

    def get_active_file_record(self, db: Session, document_id: str) -> StoredFile | None:
        return file_uploads.get_active_entity_file(db, "legal_document", document_id)

    def upload_file(
        self,
        db: Session,
        document_id: str,
        file_content: bytes,
        file_name: str,
        mime_type: str | None,
        uploaded_by: str | None = None,
    ) -> LegalDocument | None:
        document = self.get(db, document_id)
        if not document:
            return None

        existing = self.get_active_file_record(db, document_id)
        if existing:
            file_uploads.soft_delete(db=db, file=existing, hard_delete_object=True)
        elif document.file_path:
            self._delete_legacy_local_file(document.file_path)

        uploaded = file_uploads.upload(
            db=db,
            domain="legal_documents",
            entity_type="legal_document",
            entity_id=document_id,
            original_filename=file_name,
            content_type=mime_type,
            data=file_content,
            organization_id=None,
            uploaded_by=uploaded_by,
        )

        document.file_path = uploaded.storage_key_or_relative_path
        document.file_name = uploaded.original_filename
        document.file_size = uploaded.file_size
        document.mime_type = uploaded.content_type
        db.commit()
        db.refresh(document)
        return document

    def delete_file(self, db: Session, document_id: str) -> LegalDocument | None:
        document = self.get(db, document_id)
        if not document:
            return None

        record = self.get_active_file_record(db, document_id)
        if record:
            file_uploads.soft_delete(db=db, file=record, hard_delete_object=True)
        elif document.file_path:
            self._delete_legacy_local_file(document.file_path)

        document.file_path = None
        document.file_name = None
        document.file_size = None
        document.mime_type = None

        db.commit()
        db.refresh(document)
        return document

    def stream_file(
        self,
        db: Session,
        document: LegalDocument,
        *,
        require_published: bool,
    ) -> tuple[StreamResult, str]:
        if require_published and not document.is_published:
            raise ObjectNotFoundError("Document is not published")

        record = self.get_active_file_record(db, str(document.id))
        if record:
            return file_uploads.stream_file(record), record.original_filename

        if document.file_path:
            legacy_stream = self._stream_legacy_file(
                document.file_path,
                content_type=document.mime_type,
                content_length=document.file_size,
            )
            return legacy_stream, document.file_name or "document"

        raise ObjectNotFoundError("Document file does not exist")

    def _stream_legacy_file(
        self,
        file_path: str,
        *,
        content_type: str | None,
        content_length: int | None,
    ) -> StreamResult:
        path = self._safe_legacy_path(file_path)
        if not path.exists():
            raise ObjectNotFoundError(str(path))

        def _chunks():
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk

        return StreamResult(
            chunks=_chunks(),
            content_type=content_type,
            content_length=content_length,
        )

    def _delete_legacy_local_file(self, file_path: str) -> None:
        try:
            path = self._safe_legacy_path(file_path)
        except ValueError:
            logger.warning("Skipping unsafe legacy path delete: %s", file_path)
            return
        if path.exists():
            path.unlink(missing_ok=True)

    def _safe_legacy_path(self, file_path: str) -> Path:
        path = Path(file_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        resolved = path.resolve()
        allowed_root = (Path.cwd() / "uploads").resolve()
        if allowed_root != resolved and allowed_root not in resolved.parents:
            raise ValueError("Legacy path is outside uploads directory")
        return resolved

    def _set_as_current(self, db: Session, document: LegalDocument) -> None:
        db.query(LegalDocument).filter(
            and_(
                LegalDocument.document_type == document.document_type,
                LegalDocument.id != document.id,
            )
        ).update({"is_current": False})
        document.is_current = True

    def _sanitize_content(self, content: str | None) -> str | None:
        if content is None:
            return None
        return nh3.clean(
            content,
            tags=ALLOWED_HTML_TAGS,
            attributes=ALLOWED_HTML_ATTRIBUTES,
        )


legal_documents = LegalDocumentService()
