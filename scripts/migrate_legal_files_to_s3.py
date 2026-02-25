"""Migrate legacy legal document files from local disk to S3 metadata storage."""

from __future__ import annotations

from pathlib import Path

from app.db import SessionLocal
from app.models.legal import LegalDocument
from app.services.file_storage import file_uploads
from app.services.object_storage import ensure_storage_bucket


def main() -> None:
    ensure_storage_bucket()
    db = SessionLocal()
    migrated = 0
    skipped = 0
    try:
        documents = db.query(LegalDocument).all()
        for doc in documents:
            existing = file_uploads.get_active_entity_file(db, "legal_document", str(doc.id))
            if existing:
                skipped += 1
                continue
            if not doc.file_path:
                skipped += 1
                continue

            path = Path(doc.file_path)
            if not path.is_absolute():
                path = Path.cwd() / path
            resolved = path.resolve()
            if not resolved.exists() or not resolved.is_file():
                skipped += 1
                continue

            payload = resolved.read_bytes()
            uploaded = file_uploads.upload(
                db=db,
                domain="legal_documents",
                entity_type="legal_document",
                entity_id=str(doc.id),
                original_filename=doc.file_name or resolved.name,
                content_type=doc.mime_type,
                data=payload,
                organization_id=None,
                uploaded_by=None,
            )
            doc.file_path = uploaded.storage_key_or_relative_path
            doc.file_name = uploaded.original_filename
            doc.file_size = uploaded.file_size
            doc.mime_type = uploaded.content_type
            db.add(doc)
            db.commit()
            migrated += 1

        print(f"Migrated {migrated} legal files, skipped {skipped}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
