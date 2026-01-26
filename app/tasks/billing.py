from app.celery_app import celery_app
from app.db import SessionLocal
from app.services import billing_automation as billing_automation_service


@celery_app.task(name="app.tasks.billing.run_invoice_cycle")
def run_invoice_cycle():
    session = SessionLocal()
    try:
        billing_automation_service.run_invoice_cycle(session)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
