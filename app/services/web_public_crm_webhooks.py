"""Service helpers for public CRM webhooks."""

from sqlalchemy.orm import Session

from app.schemas.crm.inbox import EmailWebhookPayload, WhatsAppWebhookPayload
from app.services import crm as crm_service


def whatsapp_webhook(payload: WhatsAppWebhookPayload, db: Session):
    crm_service.inbox.receive_whatsapp_message(db, payload)
    return {"status": "ok"}


def email_webhook(payload: EmailWebhookPayload, db: Session):
    crm_service.inbox.receive_email_message(db, payload)
    return {"status": "ok"}
