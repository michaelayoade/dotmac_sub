from app.celery_app import celery_app
from app.db import SessionLocal
from app.services import nas as nas_service


@celery_app.task(name="app.tasks.nas.cleanup_nas_backups")
def cleanup_nas_backups() -> dict[str, int]:
    session = SessionLocal()
    try:
        return nas_service.NasConfigBackups.cleanup_retention(session)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
