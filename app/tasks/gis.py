import logging
import time

from app.celery_app import celery_app
from app.db import SessionLocal
from app.metrics import observe_job
from app.models.domain_settings import SettingDomain
from app.services import gis_sync as gis_sync_service
from app.services.scheduler_config import _effective_bool

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.gis.sync_gis_sources")
def sync_gis_sources():
    start = time.monotonic()
    status = "success"
    session = SessionLocal()
    try:
        sync_pops = _effective_bool(
            session,
            SettingDomain.gis,
            "sync_pop_sites",
            "GIS_SYNC_POP_SITES",
            True,
        )
        sync_addresses = _effective_bool(
            session,
            SettingDomain.gis,
            "sync_addresses",
            "GIS_SYNC_ADDRESSES",
            True,
        )
        deactivate_missing = _effective_bool(
            session,
            SettingDomain.gis,
            "sync_deactivate_missing",
            "GIS_SYNC_DEACTIVATE_MISSING",
            False,
        )
        logger.info(
            "GIS sync start pops=%s addresses=%s deactivate_missing=%s",
            sync_pops,
            sync_addresses,
            deactivate_missing,
        )
        if sync_pops:
            result = gis_sync_service.geo_sync.sync_pop_sites(
                session, deactivate_missing=deactivate_missing
            )
            logger.info(
                "GIS sync pop sites created=%s updated=%s skipped=%s",
                result.created,
                result.updated,
                result.skipped,
            )
        if sync_addresses:
            result = gis_sync_service.geo_sync.sync_addresses(
                session, deactivate_missing=deactivate_missing
            )
            logger.info(
                "GIS sync addresses created=%s updated=%s skipped=%s",
                result.created,
                result.updated,
                result.skipped,
            )
    except Exception:
        status = "error"
        session.rollback()
        logger.exception("GIS sync failed.")
        raise
    finally:
        session.close()
        duration = time.monotonic() - start
        observe_job("gis_sync", status, duration)
