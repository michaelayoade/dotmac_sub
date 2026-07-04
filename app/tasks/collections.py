import logging

from app.celery_app import celery_app
from app.schemas.collections import BillingEnforcementRunRequest
from app.services import collections as collections_service
from app.services.billing_settings import billing_enabled
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.collections.run_billing_enforcement")
def run_billing_enforcement() -> dict[str, int | str]:
    logger.info("Starting unified billing enforcement run")
    session = SessionLocal()
    try:
        if not billing_enabled(session):
            logger.info(
                "billing enforcement skipped: local billing disabled (billing_enabled)"
            )
            return {"skipped": "billing_disabled"}
        result = collections_service.billing_enforcement_reconciler.run(
            session, BillingEnforcementRunRequest()
        )
        summary: dict[str, int | str] = {
            "accounts_scanned": int(result.accounts_scanned),
            "cases_created": int(result.cases_created),
            "actions_created": int(result.actions_created),
            "skipped": int(result.skipped),
            "credit_accounts_scanned": int(result.credit_accounts_scanned),
            "credit_accounts_settled": int(result.credit_accounts_settled),
            "credit_invoices_touched": int(result.credit_invoices_touched),
            "credit_settlement_errors": int(result.credit_settlement_errors),
            "credit_applied": str(result.credit_applied),
        }
        logger.info(
            "Billing enforcement run completed: accounts_scanned=%d cases_created=%d "
            "actions_created=%d skipped=%d",
            summary["accounts_scanned"],
            summary["cases_created"],
            summary["actions_created"],
            summary["skipped"],
        )
        session.commit()
        return summary
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.collections.run_bundle_reconcile")
def run_bundle_reconcile() -> dict[str, int]:
    """Converge any divergent bundle members to their anchor's state."""
    from app.services import bundles

    session = SessionLocal()
    try:
        result = bundles.reconcile_bundle_states(session)
        session.commit()
        logger.info(
            "Bundle reconcile completed: bundles_scanned=%d members_converged=%d",
            result["bundles_scanned"],
            result["members_converged"],
        )
        return {
            "bundles_scanned": int(result["bundles_scanned"]),
            "members_converged": int(result["members_converged"]),
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.collections.run_dunning")
def run_dunning() -> dict[str, int | str]:
    """Backward-compatible task alias for the unified billing enforcer."""
    return run_billing_enforcement()


@celery_app.task(name="app.tasks.collections.prepaid_balance_sweep")
def prepaid_balance_sweep() -> dict[str, int | str]:
    """Balance/expiry-based prepaid enforcement (arm timers, warn, suspend).

    Gated OFF by default behind the ``collections.prepaid_balance_enforcement``
    control; the service returns ``{"skipped": "disabled"}`` when the control is
    off, so the beat entry firing is harmless until an operator opts in.
    """
    from app.services.collections.prepaid_balance_sweep import (
        run_prepaid_balance_sweep,
    )

    logger.info("Starting prepaid balance sweep")
    session = SessionLocal()
    try:
        result = run_prepaid_balance_sweep(session)
        logger.info("prepaid_balance_sweep completed: %s", result)
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
