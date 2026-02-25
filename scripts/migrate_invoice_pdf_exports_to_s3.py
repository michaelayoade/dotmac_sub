"""Migrate legacy local invoice PDF exports to S3-backed stored_files records."""

from __future__ import annotations

from pathlib import Path

from app.db import SessionLocal
from app.models.billing import InvoicePdfExport
from app.services.file_storage import file_uploads
from app.services.object_storage import ensure_storage_bucket


def _resolve_local_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def main() -> None:
    ensure_storage_bucket()
    db = SessionLocal()
    migrated = 0
    skipped = 0
    missing = 0
    try:
        exports = db.query(InvoicePdfExport).all()
        for export in exports:
            if not export.file_path:
                skipped += 1
                continue

            existing = file_uploads.get_active_entity_file(
                db, "invoice_pdf_export", str(export.id)
            )
            if existing:
                skipped += 1
                continue

            local_path = _resolve_local_path(export.file_path)
            local_like = export.file_path.startswith("uploads/") or export.file_path.startswith(
                "/app/uploads/"
            )
            if not local_like:
                skipped += 1
                continue
            if not local_path.exists() or not local_path.is_file():
                missing += 1
                continue

            payload = local_path.read_bytes()
            invoice = export.invoice
            org_id = getattr(getattr(invoice, "account", None), "organization_id", None)
            uploaded = file_uploads.upload(
                db=db,
                domain="generated_docs",
                entity_type="invoice_pdf_export",
                entity_id=str(export.id),
                original_filename=f"invoice-{invoice.invoice_number or invoice.id}.pdf"
                if invoice is not None
                else f"invoice-export-{export.id}.pdf",
                content_type="application/pdf",
                data=payload,
                uploaded_by=str(export.requested_by_id) if export.requested_by_id else None,
                organization_id=org_id,
            )
            export.file_path = uploaded.storage_key_or_relative_path
            export.file_size_bytes = uploaded.file_size
            db.add(export)
            db.commit()
            migrated += 1

        print(
            f"Migrated {migrated} invoice exports, skipped {skipped}, missing_local_files {missing}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
