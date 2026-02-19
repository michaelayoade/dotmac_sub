"""Service layer for legal document management."""

import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.models.legal import LegalDocument, LegalDocumentType
from app.schemas.legal import LegalDocumentCreate, LegalDocumentUpdate


UPLOAD_DIR = "uploads/legal"


class LegalDocumentService:
    """Service for managing legal documents."""

    def list(
        self,
        db: Session,
        document_type: Optional[LegalDocumentType] = None,
        is_published: Optional[bool] = None,
        is_current: Optional[bool] = None,
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

        # Ordering
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
        document_type: Optional[LegalDocumentType] = None,
        is_published: Optional[bool] = None,
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

    def get(self, db: Session, document_id: str) -> Optional[LegalDocument]:
        """Get a legal document by ID."""
        return db.get(LegalDocument, document_id)

    def get_by_slug(self, db: Session, slug: str) -> Optional[LegalDocument]:
        """Get a legal document by slug."""
        return db.query(LegalDocument).filter(LegalDocument.slug == slug).first()

    def get_current_by_type(
        self, db: Session, document_type: LegalDocumentType
    ) -> Optional[LegalDocument]:
        """Get the current published version of a document type."""
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
        """Create a new legal document."""
        document = LegalDocument(
            document_type=payload.document_type,
            title=payload.title,
            slug=payload.slug,
            version=payload.version,
            summary=payload.summary,
            content=payload.content,
            is_published=payload.is_published,
            effective_date=payload.effective_date,
        )

        if payload.is_published:
            document.published_at = datetime.now(timezone.utc)
            # Mark other documents of same type as not current
            self._set_as_current(db, document)

        db.add(document)
        db.commit()
        db.refresh(document)
        return document

    def update(
        self, db: Session, document_id: str, payload: LegalDocumentUpdate
    ) -> Optional[LegalDocument]:
        """Update a legal document."""
        document = self.get(db, document_id)
        if not document:
            return None

        update_data = payload.model_dump(exclude_unset=True)

        # Handle publishing
        if "is_published" in update_data and update_data["is_published"]:
            if not document.is_published:
                update_data["published_at"] = datetime.now(timezone.utc)

        for field, value in update_data.items():
            setattr(document, field, value)

        # If setting as current, update other documents
        if update_data.get("is_current"):
            self._set_as_current(db, document)

        db.commit()
        db.refresh(document)
        return document

    def delete(self, db: Session, document_id: str) -> bool:
        """Delete a legal document."""
        document = self.get(db, document_id)
        if not document:
            return False

        # Delete associated file if exists
        if document.file_path and os.path.exists(document.file_path):
            try:
                os.remove(document.file_path)
            except OSError:
                pass

        db.delete(document)
        db.commit()
        return True

    def upload_file(
        self,
        db: Session,
        document_id: str,
        file_content: bytes,
        file_name: str,
        mime_type: str,
    ) -> Optional[LegalDocument]:
        """Upload a file for a legal document."""
        document = self.get(db, document_id)
        if not document:
            return None

        # Ensure upload directory exists
        os.makedirs(UPLOAD_DIR, exist_ok=True)

        # Generate unique filename
        ext = os.path.splitext(file_name)[1]
        unique_name = f"{document_id}{ext}"
        file_path = os.path.join(UPLOAD_DIR, unique_name)

        # Delete old file if exists
        if document.file_path and os.path.exists(document.file_path):
            try:
                os.remove(document.file_path)
            except OSError:
                pass

        # Save new file
        with open(file_path, "wb") as f:
            f.write(file_content)

        # Update document
        document.file_path = file_path
        document.file_name = file_name
        document.file_size = len(file_content)
        document.mime_type = mime_type

        db.commit()
        db.refresh(document)
        return document

    def delete_file(self, db: Session, document_id: str) -> Optional[LegalDocument]:
        """Delete the file associated with a legal document."""
        document = self.get(db, document_id)
        if not document:
            return None

        if document.file_path and os.path.exists(document.file_path):
            try:
                os.remove(document.file_path)
            except OSError:
                pass

        document.file_path = None
        document.file_name = None
        document.file_size = None
        document.mime_type = None

        db.commit()
        db.refresh(document)
        return document

    def _set_as_current(self, db: Session, document: LegalDocument) -> None:
        """Set a document as the current version, unsetting others of same type."""
        db.query(LegalDocument).filter(
            and_(
                LegalDocument.document_type == document.document_type,
                LegalDocument.id != document.id,
            )
        ).update({"is_current": False})
        document.is_current = True


# Singleton instance
legal_documents = LegalDocumentService()
