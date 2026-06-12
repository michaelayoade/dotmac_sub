"""VAS wallet scheduled jobs."""

import logging
import time

from app.celery_app import celery_app
from app.metrics import observe_job
from app.services import vas_wallet as vas_wallet_service
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.vas.run_wallet_auto_deduct")
def run_wallet_auto_deduct():
    """Pay due/overdue DotMac invoices from wallets that opted in.

    No-ops when vas.enabled is off; the wallet is the consent boundary —
    only wallets with auto_pay_bill_enabled are swept.
    """
    start = time.monotonic()
    status = "success"
    session = SessionLocal()
    try:
        stats = vas_wallet_service.run_auto_deduct_sweep(session)
        logger.info("vas_auto_deduct_sweep %s", stats)
        return stats
    except Exception:
        status = "failure"
        raise
    finally:
        session.close()
        observe_job("vas_auto_deduct", status, time.monotonic() - start)


@celery_app.task(name="app.tasks.vas.sync_vas_catalog")
def sync_vas_catalog():
    """Refresh the VTPass service catalog (services land disabled)."""
    start = time.monotonic()
    status = "success"
    session = SessionLocal()
    try:
        from app.services import vas_purchases

        stats = vas_purchases.sync_catalog(session)
        logger.info("vas_catalog_sync %s", stats)
        return stats
    except Exception:
        status = "failure"
        raise
    finally:
        session.close()
        observe_job("vas_catalog_sync", status, time.monotonic() - start)


@celery_app.task(name="app.tasks.vas.run_vas_requery")
def run_vas_requery():
    """Resolve submitted purchases via the requery endpoint (source of truth)."""
    start = time.monotonic()
    status = "success"
    session = SessionLocal()
    try:
        from app.services import vas_purchases

        stats = vas_purchases.run_requery_sweep(session)
        logger.info("vas_requery_sweep %s", stats)
        return stats
    except Exception:
        status = "failure"
        raise
    finally:
        session.close()
        observe_job("vas_requery", status, time.monotonic() - start)
