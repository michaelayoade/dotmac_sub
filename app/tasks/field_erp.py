import time

from app.celery_app import celery_app
from app.logging import get_logger
from app.metrics import observe_job
from app.models.domain_settings import SettingDomain
from app.services import settings_spec
from app.services.db_session_adapter import db_session_adapter

SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.field_erp.sync_field_erp_outbox")
def sync_field_erp_outbox(limit: int = 50):
    start = time.monotonic()
    status = "success"
    logger = get_logger(__name__)
    session = SessionLocal()
    sync_service = None
    try:
        enabled = bool(
            settings_spec.resolve_value(
                session,
                SettingDomain.integration,
                "dotmac_erp_sync_enabled",
            )
        )
        if not enabled:
            status = "skipped"
            return {"processed": 0, "synced": 0, "failed": 0, "canceled": 0}

        from app.services.dotmac_erp import dotmac_erp_field_outbox_sync

        sync_service = dotmac_erp_field_outbox_sync(session)
        result = sync_service.process_pending(limit=limit)
        return {
            "processed": result.processed,
            "synced": result.synced,
            "failed": result.failed,
            "canceled": result.canceled,
        }
    except Exception:
        status = "error"
        session.rollback()
        logger.exception("field_erp_outbox_sync_failed")
        raise
    finally:
        if sync_service is not None:
            sync_service.close()
        session.close()
        observe_job("field_erp_outbox_sync", status, time.monotonic() - start)
