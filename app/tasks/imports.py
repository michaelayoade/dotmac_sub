"""Celery tasks for background data imports."""

from __future__ import annotations

from base64 import b64decode
from datetime import UTC, datetime
from typing import Any

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.domain_settings import SettingDomain
from app.services import email as email_service
from app.services import settings_spec
from app.services import web_system_import_wizard as import_wizard_service


def _job_progress_percent(update: dict[str, Any]) -> int:
    phase = str(update.get("phase") or "")
    if phase == "completed":
        return 100
    total_valid = int(update.get("total_valid_rows") or 0)
    if total_valid <= 0:
        return 10 if phase == "validated" else 0
    processed = int(update.get("processed_valid_rows") or 0)
    return max(0, min(99, int((processed / total_valid) * 100)))


@celery_app.task(name="app.tasks.imports.run_import_job")
def run_import_job(
    *,
    job_id: str,
    module: str,
    data_format: str,
    raw_text: str,
    source_name: str,
    dry_run: bool,
    column_mapping: dict[str, str] | None = None,
    csv_delimiter: str = ",",
    file_bytes_b64: str | None = None,
    notify_email: str | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    session = SessionLocal()
    try:
        import_wizard_service.upsert_job(
            session,
            {
                "job_id": job_id,
                "module": module,
                "module_label": import_wizard_service.ENTITY_CONFIG.get(module, {}).get("label", module),
                "source_name": source_name,
                "status": "running",
                "queued_at": started_at,
                "started_at": started_at,
                "progress_percent": 0,
                "result": None,
                "error": None,
            },
        )
    finally:
        session.close()

    tracker = {"last_pct": -1}
    file_bytes = b64decode(file_bytes_b64) if file_bytes_b64 else None

    def _progress(update: dict[str, Any]) -> None:
        pct = _job_progress_percent(update)
        phase = str(update.get("phase") or "")
        processed_valid = int(update.get("processed_valid_rows") or 0)
        if phase not in {"validated", "completed"} and processed_valid % 100 != 0:
            return
        if phase != "completed" and pct <= tracker["last_pct"]:
            return
        tracker["last_pct"] = pct
        progress_session = SessionLocal()
        try:
            import_wizard_service.upsert_job(
                progress_session,
                {
                    "job_id": job_id,
                    "status": "running",
                    "progress_percent": pct,
                    "progress": {
                        "phase": phase,
                        "processed_valid_rows": processed_valid,
                        "total_valid_rows": int(update.get("total_valid_rows") or 0),
                        "imported_rows": int(update.get("imported_rows") or 0),
                        "failed_rows": int(update.get("failed_rows") or 0),
                    },
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )
        finally:
            progress_session.close()

    session = SessionLocal()
    try:
        result = import_wizard_service.execute_import(
            session,
            module=module,
            data_format=data_format,
            raw_text=raw_text,
            source_name=source_name,
            dry_run=dry_run,
            column_mapping=column_mapping,
            csv_delimiter=csv_delimiter,
            file_bytes=file_bytes,
            progress_callback=_progress,
        )
        completed_at = datetime.now(UTC).isoformat()
        import_wizard_service.upsert_job(
            session,
            {
                "job_id": job_id,
                "status": "completed",
                "progress_percent": 100,
                "completed_at": completed_at,
                "result": result,
                "error": None,
            },
        )

        recipient = (notify_email or "").strip()
        if not recipient:
            fallback = settings_spec.resolve_value(
                session,
                SettingDomain.notification,
                "alert_notifications_default_recipient",
            )
            recipient = str(fallback or "").strip()
        if recipient:
            subject = f"Import Completed: {result.get('module_label', module)}"
            body = (
                f"<p>Import job <strong>{job_id}</strong> completed.</p>"
                f"<p>Status: {result.get('status')}</p>"
                f"<p>Imported: {result.get('imported_rows')} / {result.get('total_rows')}</p>"
            )
            email_service.send_email(
                db=session,
                to_email=recipient,
                subject=subject,
                body_html=body,
                body_text=None,
                track=True,
                activity="notification_queue",
            )
        return {"job_id": job_id, "status": "completed", "result": result}
    except Exception as exc:
        session.rollback()
        failed_session = SessionLocal()
        try:
            import_wizard_service.upsert_job(
                failed_session,
                {
                    "job_id": job_id,
                    "status": "failed",
                    "progress_percent": tracker["last_pct"] if tracker["last_pct"] > 0 else 0,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "error": str(exc),
                },
            )
            recipient = (notify_email or "").strip()
            if recipient:
                email_service.send_email(
                    db=failed_session,
                    to_email=recipient,
                    subject=f"Import Failed: {module}",
                    body_html=f"<p>Import job <strong>{job_id}</strong> failed.</p><p>{exc!s}</p>",
                    body_text=None,
                    track=True,
                    activity="notification_queue",
                )
        finally:
            failed_session.close()
        raise
    finally:
        session.close()
    file_bytes = b64decode(file_bytes_b64) if file_bytes_b64 else None
