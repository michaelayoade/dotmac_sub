import logging

from app.celery_app import celery_app
from app.db import SessionLocal
from app.services import nas as nas_service

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.nas.cleanup_nas_backups")
def cleanup_nas_backups() -> dict[str, int]:
    logger.info("Starting cleanup_nas_backups")
    session = SessionLocal()
    try:
        result = nas_service.NasConfigBackups.cleanup_retention(session)
        logger.info("Completed cleanup_nas_backups: %s", result)
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
